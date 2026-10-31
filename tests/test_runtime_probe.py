"""Offline checks for the fixture used by the Docker/PostgreSQL runtime smoke."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import IsolatedAsyncioTestCase, TestCase

import httpx
import runtime_probe as probe
from pydantic import SecretStr
from sqlalchemy import update

from merlin.api import create_app
from merlin.config import Settings
from merlin.contracts import Task, TaskSpec, TaskState, ToolCall, now
from merlin.persistence import SQLTaskStore
from merlin.persistence.models import TaskRow

TOKEN = "runtime-probe-dummy-owner-token-00000000"


async def direct_io(function: Any, *args: Any) -> Any:
    return function(*args)


class RuntimeProbeTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        root = Path(self.directory.name)
        self.paths = probe.ProbePaths(root / "artifacts", root / "probe")
        self.database_url = f"sqlite:///{root / 'runtime.db'}"
        self.store = SQLTaskStore(self.database_url)
        self.store.create_schema()
        self.settings = Settings(
            _env_file=None,
            owner_token=SecretStr(TOKEN),
            database_url=self.database_url,
            artifact_dir=self.paths.artifacts,
        )
        self.client = self.make_client()
        self.workers: list[asyncio.Task[None]] = []

    def make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=create_app(self.settings, self.store, run_sync=direct_io, poll_interval=0.001)
            ),
            base_url="http://runtime-test",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    def start_worker(self) -> asyncio.Task[None]:
        worker = asyncio.create_task(probe.serve_worker(self.store, self.paths))
        self.workers.append(worker)
        return worker

    async def stop_worker(self, worker: asyncio.Task[None]) -> None:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        self.workers.remove(worker)

    async def restart_api_and_store(self) -> None:
        await self.client.aclose()
        self.store.close()
        self.store = SQLTaskStore(self.database_url)
        self.client = self.make_client()

    async def asyncTearDown(self) -> None:
        for worker in list(self.workers):
            await self.stop_worker(worker)
        await self.client.aclose()
        self.store.close()
        self.directory.cleanup()

    async def test_completed_http_task_events_and_checkpoint_survive_store_restart(self) -> None:
        worker = self.start_worker()
        await probe.complete(self.client, self.paths)
        await self.stop_worker(worker)
        saved = probe.read_json(self.paths.record("complete"))
        task = self.store.get_task(saved["task_id"])
        self.assertEqual(task.generation, 1)
        self.assertEqual(len(self.store.invocations(task.id)), 1)
        self.assertEqual(probe.read_json(self.paths.effects(task.id)), {probe.call_for(task).id: 1})
        await self.restart_api_and_store()
        await probe.verify_complete(self.client, self.paths)
        self.assertEqual(probe.read_json(self.paths.record("complete")), saved)

    async def test_interrupted_effect_pauses_then_acknowledges_without_replay(self) -> None:
        worker = self.start_worker()
        await probe.interrupt(self.client, self.paths)
        saved = probe.read_json(self.paths.record("interrupt"))
        task_id = saved["task_id"]
        self.assertEqual(self.store.get_task(task_id).state, TaskState.RUNNING)
        invocation = self.store.invocations(task_id)[0]
        self.assertIsNone(invocation.result)
        self.assertEqual(invocation.state, "prepared")
        await self.stop_worker(worker)
        await self.restart_api_and_store()
        # Model the lease expiry after the driver's real SIGKILL without a
        # three-second wall-clock delay in deterministic unit tests.
        with self.store.engine.begin() as connection:
            connection.execute(
                update(TaskRow)
                .where(TaskRow.id == task_id)
                .values(lease_until=now() - timedelta(seconds=1))
            )
        worker = self.start_worker()
        await probe.verify_paused(self.client, self.paths)
        await self.stop_worker(worker)
        task = self.store.get_task(task_id)
        self.assertEqual(task.state, TaskState.SUCCEEDED)
        self.assertEqual(task.generation, 3)
        self.assertEqual(probe.read_json(self.paths.effects(task_id)), {invocation.call.id: 1})
        invocation = self.store.invocations(task_id)[0]
        self.assertEqual(invocation.state, "failed")
        self.assertIsNotNone(invocation.result)
        self.assertFalse(invocation.result.ok)
        self.assertEqual(probe.read_json(self.paths.restores(task_id))["count"], 1)

    async def test_model_replies_derive_only_from_task_and_durable_history(self) -> None:
        task = Task(spec=TaskSpec(prompt="smoke-complete", repository="smoke/runtime-fixture"))
        first = await probe.ProbeModel().generate(task, [], [])
        second = await probe.ProbeModel().generate(task, [], [])
        self.assertEqual(first, second)
        self.assertEqual(first.calls, [probe.call_for(task)])
        history = [
            {
                "role": "user",
                "parts": [{"function_response": {"id": first.calls[0].id, "response": {}}}],
            }
        ]
        final = await probe.ProbeModel().generate(task, history, [])
        self.assertEqual(final.text, probe.RESULT)
        self.assertEqual(final.calls, [])
        other = task.model_copy(update={"id": "another-task"})
        self.assertNotEqual(probe.call_for(task).id, probe.call_for(other).id)

    async def test_fake_sandbox_rejects_commands_and_checks_checkpoint_integrity(self) -> None:
        sandbox = probe.ProbeSandbox(self.paths)
        task = Task(spec=TaskSpec(prompt="smoke-complete", repository="smoke/runtime-fixture"))
        workspace = await sandbox.create(task)
        with self.assertRaisesRegex(probe.ProbeFailure, "deterministic fixture write"):
            await sandbox.execute(workspace, ToolCall(name="bash", arguments={"command": "false"}))
        self.assertFalse(self.paths.effects(task.id).exists())
        await sandbox.execute(workspace, probe.call_for(task))
        checkpoint = await sandbox.checkpoint(workspace, task.id)
        replacement = await sandbox.create(task)
        await sandbox.restore(replacement, checkpoint)
        self.assertEqual(
            sandbox.workspaces[replacement.id].files,
            {"smoke.txt": probe.call_for(task).arguments["content"]},
        )
        (self.paths.artifacts / checkpoint.artifact).write_text("{}")
        with self.assertRaisesRegex(probe.ProbeFailure, "hash changed"):
            await sandbox.restore(replacement, checkpoint)
        with self.assertRaisesRegex(probe.ProbeFailure, "another fixture task"):
            await sandbox.checkpoint(workspace, "other-task")

    async def test_task_wait_has_a_bounded_deadline(self) -> None:
        task = Task(spec=TaskSpec(prompt="smoke-complete", repository="smoke/runtime-fixture"))

        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=task.model_dump(mode="json"))

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="http://runtime-test"
        ) as client:
            with self.assertRaisesRegex(probe.ProbeFailure, "Timed out"):
                await probe.wait_for_task(client, task.id, TaskState.SUCCEEDED, timeout=0.01)


class ProbeFileTests(TestCase):
    def test_atomic_json_preserves_previous_record_when_serialization_fails(self) -> None:
        with TemporaryDirectory() as directory:
            target = Path(directory) / "task" / "effects.json"
            probe.atomic_json(target, {"fixture-write": 1})
            with self.assertRaises(TypeError):
                probe.atomic_json(target, {"invalid": object()})
            self.assertEqual(probe.read_json(target), {"fixture-write": 1})
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_fixture_state_and_identifiers_cannot_escape_their_scope(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(probe.ProbeFailure, "outside the artifact"):
                probe.ProbeSandbox(probe.ProbePaths(root, root / "probe"))
            paths = probe.ProbePaths(root / "artifacts", root / "probe")
            with self.assertRaisesRegex(probe.ProbeFailure, "identifier"):
                paths.effects("../another-task")
            with self.assertRaisesRegex(probe.ProbeFailure, "only smoke fixture prompts"):
                probe.call_for(
                    Task(spec=TaskSpec(prompt="arbitrary task", repository="owner/repo"))
                )
