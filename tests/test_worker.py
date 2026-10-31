from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

from merlin.context import RepositoryContext
from merlin.contracts import (
    Approval,
    Checkpoint,
    CheckpointUnavailable,
    Invocation,
    Lease,
    ModelReply,
    StaleLease,
    Task,
    TaskSpec,
    TaskState,
    ToolCall,
    ToolResult,
    Workspace,
    WorkspaceSetupError,
)
from merlin.persistence import SQLTaskStore
from merlin.worker import Worker


class MemoryStore:
    def __init__(self) -> None:
        self.task = Task(spec=TaskSpec(prompt="fix the code", repository="owner/repo"))
        self.calls: dict[str, Invocation] = {}
        self.checkpoints: list[Checkpoint] = []
        self.events_recorded: list[str] = []
        self.pending: Approval | None = None
        self.approved = False
        self.stale = False
        self.model_calls = 0

    def claim(self, worker_id: str, lease_seconds: int = 60) -> Lease | None:
        if self.task.state not in (TaskState.QUEUED, TaskState.RUNNING):
            return None
        self.task.generation += 1
        self.task.state = TaskState.RUNNING
        return Lease(task_id=self.task.id, worker_id=worker_id, generation=self.task.generation)

    def _validate(self, lease: Lease) -> None:
        if self.stale or lease.generation != self.task.generation:
            raise StaleLease

    def heartbeat(self, lease: Lease, lease_seconds: int = 60) -> None:
        self._validate(lease)

    def get_task(self, task_id: str) -> Task:
        return self.task.model_copy(deep=True)

    def update_task(self, lease: Lease, **changes: Any) -> Task:
        self._validate(lease)
        for key, value in changes.items():
            setattr(self.task, key, value)
        return self.get_task(self.task.id)

    def append_event(self, task_id: str, kind: str, data: dict[str, Any], lease: Lease) -> None:
        self._validate(lease)
        self.events_recorded.append(kind)

    def invocations(self, task_id: str) -> list[Invocation]:
        return list(self.calls.values())

    def begin_tool(self, lease: Lease, call: ToolCall) -> Invocation:
        self._validate(lease)
        if call.id not in self.calls:
            self.calls[call.id] = Invocation(task_id=lease.task_id, call=call)
        return self.calls[call.id]

    def finish_tool(
        self, lease: Lease, result: ToolResult, *, checkpoint: Checkpoint | None = None
    ) -> None:
        self._validate(lease)
        self.calls[result.call_id].result = result
        self.calls[result.call_id].state = "uncertain" if result.uncertain else "succeeded"
        self.events_recorded.append("tool.finished")
        if checkpoint:
            self.save_checkpoint(lease, checkpoint)

    def latest_checkpoint(self, task_id: str) -> Checkpoint | None:
        return self.checkpoints[-1] if self.checkpoints else None

    def save_checkpoint(self, lease: Lease, checkpoint: Checkpoint) -> None:
        self._validate(lease)
        self.checkpoints.append(checkpoint)
        self.task.checkpoint_id = checkpoint.id

    def reserve_usage(self, lease: Lease, **usage: Any) -> str:
        self._validate(lease)
        self.model_calls += usage.get("calls", 0)
        return "reservation"

    def settle_usage(self, lease: Lease, reservation: str, cost_usd: float) -> None:
        self._validate(lease)

    def consume_approval(self, lease: Lease, call: ToolCall) -> bool:
        if not self.approved:
            return False
        self.approved = False
        self.begin_tool(lease, call)
        return True

    def request_approval(self, lease: Lease, call: ToolCall) -> Approval:
        self.pending = Approval(task_id=lease.task_id, call=call, digest="digest")
        return self.pending


class FakeSandbox:
    def __init__(self) -> None:
        self.closed = 0
        self.creates = 0
        self.restored = 0
        self.executed: list[str] = []
        self.unavailable = False

    async def create(self, task: Task) -> Workspace:
        self.creates += 1
        return Workspace(id=f"sandbox-{self.creates}")

    async def connect(self, sandbox_id: str) -> Workspace:
        if self.unavailable:
            raise RuntimeError("missing")
        return Workspace(id=sandbox_id)

    async def execute(self, workspace: Workspace, call: ToolCall) -> ToolResult:
        if call.name == "read_file" and call.arguments.get("path", "").endswith("MERLIN.md"):
            return ToolResult(call_id=call.id, ok=False, output="No such file or directory")
        self.executed.append(call.id)
        return ToolResult(call_id=call.id, output="done")

    async def checkpoint(self, workspace: Workspace, task_id: str) -> Checkpoint:
        return Checkpoint(task_id=task_id, artifact="snapshot", sha256="0" * 64)

    async def restore(self, workspace: Workspace, checkpoint: Checkpoint) -> None:
        self.restored += 1

    async def close(self, workspace: Workspace) -> None:
        self.closed += 1


class FakeModel:
    def __init__(self, replies: list[ModelReply]) -> None:
        self.replies = replies
        self.histories: list[list[dict[str, Any]]] = []

    async def generate(self, task: Task, history: Any, tools: Any) -> ModelReply:
        self.histories.append(history)
        return self.replies.pop(0)


class FakeExecutor:
    def __init__(self, sandbox: FakeSandbox) -> None:
        self.sandbox = sandbox
        self.permissions: list[list[str]] = []

    def definitions(self) -> list[dict[str, Any]]:
        return []

    async def execute(self, task: Task, workspace: Workspace, call: ToolCall) -> ToolResult:
        self.permissions.append(task.spec.permissions)
        return await self.sandbox.execute(workspace, call)


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_between_claim_and_reservation_starts_no_work(self) -> None:
        store = SQLTaskStore("sqlite+pysqlite:///:memory:")
        store.create_schema()
        self.addCleanup(store.close)
        task = store.create_task(TaskSpec(prompt="fix", repository="owner/repo"))
        original_claim = store.claim

        def cancel_on_claim(worker_id, lease_seconds):
            lease = original_claim(worker_id, lease_seconds)
            store.cancel(task.id)
            return lease

        sandbox = FakeSandbox()
        model = FakeModel([])
        worker = Worker(store, model, sandbox, FakeExecutor(sandbox))
        with patch.object(store, "claim", side_effect=cancel_on_claim):
            self.assertTrue(await worker.run_once())
        self.assertEqual(store.get_task(task.id).state, TaskState.CANCELLED)
        self.assertEqual(store.usage(task.id)["sandbox_seconds"], 0)
        self.assertEqual(sandbox.creates, 0)
        self.assertEqual(model.histories, [])
        self.assertFalse(await worker.run_once())

    async def test_cancel_racing_with_reservation_finalizes_through_valid_lease(self) -> None:
        store = SQLTaskStore("sqlite+pysqlite:///:memory:")
        store.create_schema()
        self.addCleanup(store.close)
        task = store.create_task(TaskSpec(prompt="fix", repository="owner/repo"))
        reserve = store.reserve_usage

        def cancel_on_reserve(lease, **usage):
            store.cancel(task.id)
            return reserve(lease, **usage)

        sandbox = FakeSandbox()
        worker = Worker(store, FakeModel([]), sandbox, FakeExecutor(sandbox))
        with patch.object(store, "reserve_usage", side_effect=cancel_on_reserve):
            self.assertTrue(await worker.run_once())
        self.assertEqual(store.get_task(task.id).state, TaskState.CANCELLED)
        self.assertEqual(store.usage(task.id)["calls"], 0)
        self.assertEqual(sandbox.creates, 0)

    async def test_cancelled_stale_owner_cannot_finalize_a_new_generation(self) -> None:
        store = SQLTaskStore("sqlite+pysqlite:///:memory:")
        store.create_schema()
        self.addCleanup(store.close)
        task = store.create_task(TaskSpec(prompt="fix", repository="owner/repo"))

        def replace_owner(lease, **usage):
            from sqlalchemy import update

            from merlin.persistence.models import TaskRow

            store.cancel(task.id)
            with store.engine.begin() as connection:
                connection.execute(
                    update(TaskRow)
                    .where(TaskRow.id == task.id)
                    .values(generation=lease.generation + 1, worker_id="replacement")
                )
            raise StaleLease

        sandbox = FakeSandbox()
        worker = Worker(store, FakeModel([]), sandbox, FakeExecutor(sandbox))
        with patch.object(store, "reserve_usage", side_effect=replace_owner):
            self.assertTrue(await worker.run_once())
        current = store.get_task(task.id)
        self.assertEqual(current.state, TaskState.RUNNING)
        self.assertEqual(current.worker_id, "replacement")
        self.assertEqual(sandbox.creates, 0)

    async def test_large_published_file_finishes_without_repeating_publication(self) -> None:
        store = SQLTaskStore("sqlite+pysqlite:///:memory:")
        store.create_schema()
        self.addCleanup(store.close)
        task = store.create_task(
            TaskSpec(
                prompt="Publish the approved change and summarize it",
                repository="owner/repo",
                permissions=["publish_branch"],
            )
        )
        call = ToolCall(
            name="github_publish_branch",
            arguments={
                "base_sha": "a" * 40,
                "message": "docs: record verification",
                "files": [{"path": "PROGRESS.md", "content": "x" * 55000}],
            },
        )
        native = {
            "role": "model",
            "parts": [
                {
                    "function_call": {
                        "id": call.id,
                        "name": call.name,
                        "args": call.arguments,
                    },
                    "thought_signature": "opaque-provider-signature",
                }
            ],
        }
        model = FakeModel(
            [ModelReply(calls=[call], content=native), ModelReply(text="Published and verified")]
        )
        sandbox = FakeSandbox()
        sandbox.checkpoint = AsyncMock(
            side_effect=lambda workspace, task_id: Checkpoint(
                task_id=task_id, artifact="snapshot", sha256="b" * 64
            )
        )
        worker = Worker(
            store,
            model,
            sandbox,
            FakeExecutor(sandbox),
            context=RepositoryContext(sandbox, store, max_chars=80000),
        )
        self.assertTrue(await worker.run_once())
        finished = store.get_task(task.id)
        self.assertEqual(finished.state, TaskState.SUCCEEDED, finished.error)
        self.assertEqual(finished.result, "Published and verified")
        self.assertEqual(len(model.histories), 2)
        self.assertIn(native, model.histories[-1])
        self.assertEqual(sandbox.executed.count(call.id), 1)
        self.assertEqual(len(store.invocations(task.id)), 1)
        self.assertIsNotNone(store.invocations(task.id)[0].result)
        self.assertFalse(await worker.run_once())
        self.assertEqual(sandbox.executed.count(call.id), 1)

    async def test_failed_pre_edit_checkpoint_pauses_without_intent_and_resumes_once(self):
        store, sandbox = MemoryStore(), FakeSandbox()
        call = ToolCall(name="replace_text", arguments={"path": "docs/guide.md"})
        captures = 0

        async def checkpoint(workspace, task_id):
            nonlocal captures
            captures += 1
            if captures == 2:
                raise CheckpointUnavailable
            return Checkpoint(task_id=task_id, artifact="snapshot", sha256="hash")

        sandbox.checkpoint = AsyncMock(side_effect=checkpoint)
        worker = Worker(
            store,
            FakeModel([ModelReply(calls=[call]), ModelReply(text="Done")]),
            sandbox,
            FakeExecutor(sandbox),
        )
        await worker.run_once()
        self.assertEqual(store.task.state, TaskState.PAUSED)
        self.assertIn("checkpoint connection failed", store.task.error)
        self.assertFalse(store.calls)
        self.assertFalse(sandbox.executed)
        self.assertNotIn("tool.started", store.events_recorded)
        store.task.state = TaskState.QUEUED
        await worker.run_once()
        self.assertEqual(store.task.state, TaskState.SUCCEEDED)
        self.assertEqual(sandbox.executed, [call.id])

    async def test_failed_post_edit_checkpoint_pauses_and_never_replays_mutation(self):
        store, sandbox = MemoryStore(), FakeSandbox()
        call = ToolCall(name="write_file", arguments={"path": "docs/guide.md"})
        snapshot = Checkpoint(task_id=store.task.id, artifact="snapshot", sha256="hash")
        sandbox.checkpoint = AsyncMock(side_effect=[snapshot, snapshot, CheckpointUnavailable()])
        worker = Worker(
            store, FakeModel([ModelReply(calls=[call])]), sandbox, FakeExecutor(sandbox)
        )
        await worker.run_once()
        self.assertEqual(store.task.state, TaskState.PAUSED)
        self.assertEqual(sandbox.executed, [call.id])
        self.assertIsNone(store.calls[call.id].result)
        store.task.state = TaskState.QUEUED
        await worker.run_once()
        self.assertEqual(store.task.state, TaskState.PAUSED)
        self.assertIn("uncertain", store.task.error)
        self.assertEqual(sandbox.executed, [call.id])

    async def test_setup_diagnostics_are_visible_but_provider_secrets_are_not(self) -> None:
        for error, expected in (
            (RuntimeError("private-provider-token"), "RuntimeError: task failed"),
            (
                WorkspaceSetupError("repository_clone", 128),
                "WorkspaceSetupError: Repository clone failed (exit code 128)",
            ),
        ):
            with self.subTest(error=type(error).__name__):
                store, sandbox = MemoryStore(), FakeSandbox()
                sandbox.create = AsyncMock(side_effect=error)
                model = FakeModel([ModelReply(calls=[ToolCall(name="list_files")])])
                worker = Worker(store, model, sandbox, FakeExecutor(sandbox))
                await worker.run_once()
                self.assertEqual(store.task.state, TaskState.FAILED)
                self.assertEqual(store.task.error, expected)
                self.assertNotIn("private-provider-token", store.task.error or "")

    async def test_durable_loop_checkpoints_and_strips_internal_metadata(self) -> None:
        store, sandbox = MemoryStore(), FakeSandbox()
        call = ToolCall(name="write_file", arguments={"path": "a.py", "content": "x"})
        model = FakeModel(
            [
                ModelReply(
                    calls=[call],
                    content={"role": "model", "parts": [{"thought_signature": "opaque"}]},
                ),
                ModelReply(text="fixed"),
            ]
        )
        worker = Worker(store, model, sandbox, FakeExecutor(sandbox))
        assert await worker.run_once()
        assert store.task.state == TaskState.SUCCEEDED
        assert store.task.result == "fixed"
        assert sandbox.executed == [call.id]
        assert len(store.checkpoints) == 4
        assert sandbox.closed == 1
        assert store.model_calls == 2
        assert all("_merlin_calls" not in part for part in model.histories[-1])
        assert model.histories[-1][1]["parts"][0]["thought_signature"] == "opaque"
        assert not await worker.run_once()

    async def test_reclaimed_prepared_invocation_is_not_replayed(self) -> None:
        store, sandbox = MemoryStore(), FakeSandbox()
        call = ToolCall(name="bash", arguments={"command": "change things"})
        store.calls[call.id] = Invocation(task_id=store.task.id, call=call)
        worker = Worker(store, FakeModel([]), sandbox, FakeExecutor(sandbox))
        await worker.run_once()
        assert store.task.state == TaskState.PAUSED
        assert "uncertain" in (store.task.error or "")
        assert not sandbox.executed
        assert sandbox.creates == 0

    async def test_completed_invocation_missing_history_is_not_executed_twice(self) -> None:
        store, sandbox = MemoryStore(), FakeSandbox()
        call = ToolCall(name="bash", arguments={"command": "change things"})
        store.task.history = [{"role": "model", "parts": [], "_merlin_calls": [call.model_dump()]}]
        store.calls[call.id] = Invocation(
            task_id=store.task.id,
            call=call,
            result=ToolResult(call_id=call.id, output="already done"),
        )
        worker = Worker(
            store, FakeModel([ModelReply(text="complete")]), sandbox, FakeExecutor(sandbox)
        )
        await worker.run_once()
        assert store.task.state == TaskState.SUCCEEDED
        assert not sandbox.executed

    async def test_approval_resumes_same_call_without_another_model_request(self) -> None:
        store, sandbox = MemoryStore(), FakeSandbox()
        call = ToolCall(name="github_create_issue", arguments={"title": "Issue", "body": "Text"})
        model = FakeModel([ModelReply(calls=[call]), ModelReply(text="created")])
        executor = FakeExecutor(sandbox)
        worker = Worker(store, model, sandbox, executor)
        await worker.run_once()
        assert store.task.state == TaskState.WAITING_FOR_APPROVAL
        assert store.pending and store.pending.call.id == call.id
        assert not store.calls
        assert len(model.histories) == 1
        store.approved = True
        store.task.state = TaskState.QUEUED
        await worker.run_once()
        assert store.task.state == TaskState.SUCCEEDED
        assert sandbox.executed == [call.id]
        assert "github_create_issue" in executor.permissions[0]
        assert store.task.spec.permissions == []
        assert len(model.histories) == 2

    async def test_missing_sandbox_restores_checkpoint(self) -> None:
        store, sandbox = MemoryStore(), FakeSandbox()
        store.task.sandbox_id = "gone"
        store.checkpoints.append(Checkpoint(task_id=store.task.id, artifact="saved", sha256="hash"))
        sandbox.unavailable = True
        model = FakeModel(
            [ModelReply(calls=[ToolCall(name="list_files")]), ModelReply(text="done")]
        )
        worker = Worker(store, model, sandbox, FakeExecutor(sandbox))
        await worker.run_once()
        assert sandbox.restored == 1
        assert store.task.state == TaskState.SUCCEEDED

    async def test_cancellation_interrupts_model_and_cleans_workspace(self) -> None:
        store, sandbox = MemoryStore(), FakeSandbox()
        started = asyncio.Event()

        class BlockingModel:
            async def generate(self, task: Task, history: Any, tools: Any) -> ModelReply:
                if not sandbox.creates:
                    return ModelReply(calls=[ToolCall(name="list_files")])
                started.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        worker = Worker(
            store, BlockingModel(), sandbox, FakeExecutor(sandbox), heartbeat_interval=0.01
        )
        execution = asyncio.create_task(worker.run_once())
        await asyncio.wait_for(started.wait(), timeout=1)
        store.task.cancel_requested = True
        await asyncio.wait_for(execution, timeout=1)
        assert store.task.state == TaskState.CANCELLED
        assert sandbox.closed == 1

    async def test_stale_worker_stops_without_persisting_success(self) -> None:
        store, sandbox = MemoryStore(), FakeSandbox()
        started = asyncio.Event()

        class BlockingModel:
            async def generate(self, task: Task, history: Any, tools: Any) -> ModelReply:
                if not sandbox.creates:
                    return ModelReply(calls=[ToolCall(name="list_files")])
                started.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        worker = Worker(
            store, BlockingModel(), sandbox, FakeExecutor(sandbox), heartbeat_interval=0.01
        )
        execution = asyncio.create_task(worker.run_once())
        await asyncio.wait_for(started.wait(), timeout=1)
        store.stale = True
        await asyncio.wait_for(execution, timeout=1)
        assert store.task.state == TaskState.RUNNING
        assert sandbox.closed == 1
