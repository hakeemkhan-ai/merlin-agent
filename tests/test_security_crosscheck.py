"""Independent regressions across durable lifecycle and sandbox policy boundaries."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from sqlalchemy import update

from merlin.browser._remote import BrowserSession
from merlin.contracts import (
    Checkpoint,
    ModelReply,
    TaskSpec,
    TaskState,
    ToolCall,
    ToolResult,
    Workspace,
    now,
)
from merlin.persistence import SQLTaskStore
from merlin.persistence.models import TaskRow
from merlin.sandbox._remote import operate
from merlin.worker import Worker


class ParentLifecycleCrosscheck(unittest.TestCase):
    def test_terminal_root_stops_queued_and_running_children(self) -> None:
        for terminal_state in (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED):
            with self.subTest(state=terminal_state):
                store = SQLTaskStore("sqlite:///:memory:")
                store.create_schema()
                try:
                    parent = store.create_task(TaskSpec(prompt="delegate", repository="owner/repo"))
                    parent_lease = store.claim("parent")
                    assert parent_lease is not None
                    children = [
                        store.create_task(
                            TaskSpec(
                                prompt=f"child {index}",
                                repository="owner/repo",
                                parent_id=parent.id,
                            ),
                            parent_lease=parent_lease,
                        )
                        for index in range(2)
                    ]
                    child_lease = store.claim("child")
                    assert child_lease is not None
                    self.assertEqual(child_lease.task_id, children[0].id)
                    store.update_task(parent_lease, state=terminal_state)
                    running = store.get_task(children[0].id)
                    queued = store.get_task(children[1].id)
                    self.assertTrue(running.cancel_requested)
                    self.assertTrue(queued.cancel_requested)
                    self.assertEqual(queued.state, TaskState.CANCELLED)
                    self.assertIsNone(store.claim("extra"))
                finally:
                    store.close()


@unittest.skipUnless(shutil.which("rg"), "Sandbox search requires ripgrep")
class SearchPolicyCrosscheck(unittest.TestCase):
    def test_recursive_glob_search_excludes_secrets_but_keeps_env_examples(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {
                "source.py": "visible_marker = True\n",
                ".env.example": "example_marker=allowed\n",
                "credentials": "private_marker=never-return\n",
                ".env": "private_marker=never-return\n",
                ".env.production": "private_marker=never-return\n",
                ".aws/credentials": "private_marker=never-return\n",
                "nested/id_rsa": "private_marker=never-return\n",
                "nested/.ssh/config": "private_marker=never-return\n",
            }
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            result = operate(
                {
                    "root": str(root),
                    "operation": "search",
                    "arguments": {"query": "marker", "path": ".", "glob": "*"},
                }
            )
            self.assertIn("visible_marker", result["output"])
            self.assertIn("example_marker", result["output"])
            self.assertNotIn("private_marker", result["output"])


class BrowserOriginCrosscheck(unittest.TestCase):
    @staticmethod
    def session(page_url: str) -> BrowserSession:
        session = BrowserSession.__new__(BrowserSession)
        session.domains = ["docs.example.com"]
        session.entries = []
        session.page = Mock(url=page_url)
        return session

    @staticmethod
    def request_route(session: BrowserSession, target: str, method: str) -> Mock:
        frame = SimpleNamespace(url=session.page.url, parent_frame=None, page=session.page)
        request = SimpleNamespace(
            url=target,
            method=method,
            frame=frame,
            is_navigation_request=lambda: False,
            redirected_from=None,
        )
        return Mock(request=request)

    def test_external_page_cannot_post_to_sandbox_application(self) -> None:
        session = self.session("https://docs.example.com/untrusted-page")
        route = self.request_route(session, "http://localhost:3000/delete", "POST")
        session.route(route)
        route.abort.assert_called_once()
        route.continue_.assert_not_called()

    def test_external_page_cannot_read_sandbox_application(self) -> None:
        session = self.session("https://docs.example.com/untrusted-page")
        route = self.request_route(session, "http://127.0.0.1:3000/private", "GET")
        session.route(route)
        route.abort.assert_called_once()
        route.continue_.assert_not_called()

    def test_local_application_can_still_post_to_its_backend(self) -> None:
        session = self.session("http://localhost:3000/app")
        route = self.request_route(session, "http://127.0.0.1:8000/save", "POST")
        session.route(route)
        route.continue_.assert_called_once()
        route.abort.assert_not_called()

    def test_public_redirect_cannot_return_to_a_previously_local_page(self) -> None:
        session = self.session("http://localhost:3000/app")
        session.navigation_target = "https://docs.example.com/redirect"
        route = self.request_route(session, "http://127.0.0.1:8000/private", "GET")
        route.request.is_navigation_request = lambda: True
        route.request.redirected_from = SimpleNamespace(
            url="https://docs.example.com/redirect", redirected_from=None
        )
        session.route(route)
        route.abort.assert_called_once()
        route.continue_.assert_not_called()


class SandboxTakeoverCrosscheck(unittest.IsolatedAsyncioTestCase):
    async def test_reclaimed_generation_restores_into_an_independent_sandbox(self) -> None:
        store = SQLTaskStore("sqlite:///:memory:")
        store.create_schema()
        try:
            task = store.create_task(TaskSpec(prompt="continue", repository="owner/repo"))
            abandoned = store.claim("old-worker")
            assert abandoned is not None
            checkpoint = Checkpoint(task_id=task.id, artifact="last.tar", sha256="0" * 64)
            store.save_checkpoint(abandoned, checkpoint)
            store.update_task(abandoned, sandbox_id="old-worker-sandbox")
            with store.engine.begin() as connection:
                connection.execute(
                    update(TaskRow)
                    .where(TaskRow.id == task.id)
                    .values(lease_until=now() - timedelta(seconds=1))
                )
            fresh = Workspace(id="replacement-sandbox")
            sandbox = SimpleNamespace(
                create=AsyncMock(return_value=fresh),
                connect=AsyncMock(return_value=Workspace(id="old-worker-sandbox")),
                restore=AsyncMock(),
                checkpoint=AsyncMock(
                    return_value=Checkpoint(task_id=task.id, artifact="new.tar", sha256="1" * 64)
                ),
                close=AsyncMock(),
            )
            call = ToolCall(name="list_files")
            model = SimpleNamespace(
                generate=AsyncMock(
                    side_effect=[ModelReply(calls=[call]), ModelReply(text="Recovered")]
                )
            )
            executor = SimpleNamespace(
                definitions=Mock(return_value=[]),
                execute=AsyncMock(return_value=ToolResult(call_id=call.id)),
            )
            worker = Worker(store, model, sandbox, executor, worker_id="replacement")
            self.assertTrue(await worker.run_once())
            self.assertEqual(store.get_task(task.id).state, TaskState.SUCCEEDED)
            sandbox.connect.assert_not_awaited()
            sandbox.restore.assert_awaited_once_with(fresh, checkpoint)
            sandbox.close.assert_awaited_once_with(fresh)
        finally:
            store.close()
