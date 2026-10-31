"""Boundary regressions for deadlines, context preflight, and usage uncertainty."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from test_worker import FakeExecutor, FakeModel, FakeSandbox

from merlin.contracts import BudgetExceeded, ModelReply, TaskSpec, TaskState, ToolCall, ToolResult
from merlin.integrations.model_policy import ModelRequestFailure, RequestAccounting
from merlin.persistence import SQLTaskStore
from merlin.worker import Worker


@pytest.fixture
def store():
    value = SQLTaskStore("sqlite:///:memory:")
    value.create_schema()
    yield value
    value.close()


async def test_context_failure_does_not_consume_a_model_call(store):
    task = store.create_task(TaskSpec(prompt="fix", repository="owner/repo"))
    sandbox = FakeSandbox()
    model = FakeModel([])
    context = AsyncMock()
    context.build.side_effect = BudgetExceeded
    worker = Worker(store, model, sandbox, FakeExecutor(sandbox), context=context)
    await worker.run_once()
    assert store.get_task(task.id).state == TaskState.PAUSED
    assert store.usage(task.id)["calls"] == 0
    assert not store.reservations(task.id)


async def test_provider_preflight_does_not_consume_a_model_call(store):
    task = store.create_task(TaskSpec(prompt="explain"))
    sandbox = FakeSandbox()
    model = FakeModel([])
    model.estimate_call_cost = AsyncMock(side_effect=BudgetExceeded)
    worker = Worker(store, model, sandbox, FakeExecutor(sandbox))
    await worker.run_once()
    assert store.get_task(task.id).state == TaskState.PAUSED
    assert store.usage(task.id)["calls"] == 0


@pytest.mark.parametrize("request_started", [False, True])
async def test_only_proven_undispatched_requests_release_usage(store, request_started):
    task = store.create_task(TaskSpec(prompt="explain"))
    sandbox = FakeSandbox()
    model = FakeModel([])
    model.generate = AsyncMock(
        side_effect=ModelRequestFailure(
            "model_connection", RequestAccounting(request_started=request_started)
        )
    )
    await Worker(store, model, sandbox, FakeExecutor(sandbox)).run_once()
    assert store.get_task(task.id).state == TaskState.PAUSED
    assert store.usage(task.id)["calls"] == int(request_started)
    outstanding = store.reservations(task.id)
    if request_started:
        assert outstanding[0]["status"] == "dispatched"
    else:
        assert not outstanding
        assert any(event.kind == "usage.released" for event in store.events(task.id))


async def test_missing_usage_never_settles_a_completed_reply_to_zero(store):
    task = store.create_task(TaskSpec(prompt="explain"))
    sandbox = FakeSandbox()
    model = FakeModel([ModelReply(text="Answer", usage_complete=False)])
    await Worker(store, model, sandbox, FakeExecutor(sandbox)).run_once()
    assert store.get_task(task.id).state == TaskState.SUCCEEDED
    assert store.reservations(task.id)[0]["status"] == "dispatched"
    assert store.usage(task.id)["reserved_cost_usd"] > 0
    reply_event = next(event for event in store.events(task.id) if event.kind == "model.reply")
    assert reply_event.data["usage_complete"] is False


async def test_workspace_deadline_pauses_without_dispatching_pending_edit(store):
    task = store.create_task(TaskSpec(prompt="fix", repository="owner/repo"))
    sandbox = FakeSandbox()

    async def slow_create(_task):
        await asyncio.sleep(1)
        raise AssertionError("Deadline should cancel setup")

    sandbox.create = slow_create
    model = FakeModel([ModelReply(calls=[ToolCall(name="write_file", arguments={})])])
    await Worker(
        store, model, sandbox, FakeExecutor(sandbox), workspace_setup_timeout=0.01
    ).run_once()
    assert store.get_task(task.id).state == TaskState.PAUSED
    assert not store.invocations(task.id)
    assert not sandbox.executed


async def test_cancellation_resistant_adapter_drain_has_a_deadline(store):
    sandbox = FakeSandbox()
    worker = Worker(store, FakeModel([]), sandbox, FakeExecutor(sandbox), cleanup_timeout=0.01)
    release = asyncio.Event()

    async def reluctant():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await release.wait()

    execution = asyncio.create_task(reluctant())
    await asyncio.sleep(0)
    execution.cancel()
    try:
        async with asyncio.timeout(0.2):
            await worker._drain(execution)
        assert not execution.done()
    finally:
        release.set()
        await execution


async def test_cleanup_deadline_does_not_change_success(store):
    task = store.create_task(TaskSpec(prompt="read", repository="owner/repo"))
    sandbox = FakeSandbox()

    async def slow_close(_workspace):
        await asyncio.sleep(1)

    sandbox.close = slow_close
    model = FakeModel([ModelReply(calls=[ToolCall(name="list_files")]), ModelReply(text="Done")])
    async with asyncio.timeout(0.5):
        await Worker(store, model, sandbox, FakeExecutor(sandbox), cleanup_timeout=0.01).run_once()
    assert store.get_task(task.id).state == TaskState.SUCCEEDED


async def test_reads_overlap_with_a_bound_and_mutations_are_barriers(store):
    task = store.create_task(TaskSpec(prompt="inspect and edit", repository="owner/repo"))
    calls = [ToolCall(name="read_file", arguments={"path": f"{index}.py"}) for index in range(4)]
    mutation = ToolCall(name="write_file", arguments={"path": "new.py", "content": "pass"})
    final = ToolCall(name="list_files")
    sandbox = FakeSandbox()
    active, peak = 0, 0
    finished: list[str] = []

    async def execute(_workspace, call):
        nonlocal active, peak
        if call.name == "write_file":
            assert active == 0
            assert len(finished) == 4
        else:
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02 if call.id == calls[0].id else 0.005)
            active -= 1
        finished.append(call.id)
        return ToolResult(call_id=call.id, output="read")

    sandbox.execute = execute
    model = FakeModel([ModelReply(calls=[*calls, mutation, final]), ModelReply(text="Done")])
    await Worker(store, model, sandbox, FakeExecutor(sandbox), parallel_read_tools=2).run_once()
    assert store.get_task(task.id).state == TaskState.SUCCEEDED
    assert peak == 2
    responses = [
        part["function_response"]["id"]
        for entry in store.get_task(task.id).history
        for part in entry.get("parts", [])
        if "function_response" in part
    ]
    assert responses == [call.id for call in [*calls, mutation, final]]


async def test_uncertain_read_batch_persists_every_result_before_pausing(store):
    task = store.create_task(TaskSpec(prompt="inspect", repository="owner/repo"))
    calls = [ToolCall(name="read_file", arguments={"path": f"{index}.py"}) for index in range(2)]
    sandbox = FakeSandbox()

    async def execute(_workspace, call):
        return ToolResult(
            call_id=call.id, ok=call.id != calls[0].id, uncertain=call.id == calls[0].id
        )

    sandbox.execute = execute
    await Worker(
        store, FakeModel([ModelReply(calls=calls)]), sandbox, FakeExecutor(sandbox)
    ).run_once()
    assert store.get_task(task.id).state == TaskState.PAUSED
    assert all(item.result is not None for item in store.invocations(task.id))
    assert len(store.get_task(task.id).history[-1]["parts"]) == 2


async def test_cancellation_between_read_dispatches_is_not_wrapped_as_failure(store):
    task = store.create_task(TaskSpec(prompt="inspect", repository="owner/repo"))
    calls = [ToolCall(name="read_file", arguments={"path": f"{index}.py"}) for index in range(3)]
    sandbox = FakeSandbox()

    async def execute(_workspace, call):
        if call.id == calls[0].id:
            store.cancel(task.id)
        await asyncio.sleep(0)
        return ToolResult(call_id=call.id)

    sandbox.execute = execute
    model = FakeModel([ModelReply(calls=calls)])
    await Worker(store, model, sandbox, FakeExecutor(sandbox), parallel_read_tools=2).run_once()
    assert store.get_task(task.id).state == TaskState.CANCELLED
    assert sandbox.closed == 1


@pytest.mark.parametrize("slow_capture", [2, 3])
async def test_checkpoint_deadline_pauses_and_preserves_dispatch_boundary(store, slow_capture):
    from merlin.contracts import Checkpoint

    task = store.create_task(TaskSpec(prompt="edit", repository="owner/repo"))
    call = ToolCall(name="write_file", arguments={"path": "a.py", "content": "pass"})
    sandbox = FakeSandbox()
    captures = 0

    async def checkpoint(_workspace, task_id):
        nonlocal captures
        captures += 1
        if captures == slow_capture:
            await asyncio.sleep(1)
        return Checkpoint(task_id=task_id, artifact="fixture.tar", sha256="0" * 64)

    sandbox.checkpoint = checkpoint
    await Worker(
        store,
        FakeModel([ModelReply(calls=[call])]),
        sandbox,
        FakeExecutor(sandbox),
        checkpoint_timeout=0.01,
    ).run_once()
    assert store.get_task(task.id).state == TaskState.PAUSED
    assert (call.id in sandbox.executed) == (slow_capture == 3)
    invocations = store.invocations(task.id)
    if slow_capture == 2:
        assert not invocations
    else:
        assert len(invocations) == 1 and invocations[0].result is None


async def test_completion_evidence_does_not_treat_prose_or_old_checks_as_verification(store):
    task = store.create_task(TaskSpec(prompt="edit", repository="owner/repo"))
    check = ToolCall(name="bash", arguments={"command": "pytest"})
    edit = ToolCall(name="write_file", arguments={"path": "a.py", "content": "pass"})
    sandbox = FakeSandbox()

    async def execute(_workspace, call):
        return ToolResult(call_id=call.id, output="passed", data={"exit_code": 0})

    sandbox.execute = execute
    replies = [ModelReply(calls=[check, edit]), ModelReply(text="All tests passed")]
    await Worker(store, FakeModel(replies), sandbox, FakeExecutor(sandbox)).run_once()
    evidence = next(
        event.data for event in store.events(task.id) if event.kind == "task.completion"
    )
    assert evidence["changed_paths"] == ["a.py"]
    assert evidence["commands_after_last_edit"] == []
    assert evidence["verification"] == "not_recorded"
