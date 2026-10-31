"""Conversation stays independent of repository execution and publishing."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from test_integration import MemorySandbox
from test_worker import FakeExecutor, FakeModel, FakeSandbox

from merlin.agent import AgentToolExecutor
from merlin.context import RepositoryContext
from merlin.contracts import ModelReply, TaskSpec, TaskState, ToolCall
from merlin.persistence import SQLTaskStore
from merlin.worker import Worker


@pytest.fixture
def store():
    database = SQLTaskStore("sqlite+pysqlite:///:memory:")
    database.create_schema()
    yield database
    database.close()


@pytest.mark.parametrize("repository", [None, "owner/repo"])
async def test_greeting_needs_no_workspace_checkpoint_or_sandbox_budget(store, repository):
    task = store.create_task(TaskSpec(prompt="Good Morning !", repository=repository))
    sandbox = FakeSandbox()
    sandbox.execute = AsyncMock(side_effect=AssertionError("Chat must not read repository files"))

    class SlowModel(FakeModel):
        async def generate(self, *args):
            await asyncio.sleep(0.04)
            return await super().generate(*args)

    model = SlowModel([ModelReply(text="Good morning! How can I help?")])
    model.generate = AsyncMock(wraps=model.generate)
    executor = FakeExecutor(sandbox)
    executor.definitions = lambda: [{"name": "list_files"}]
    worker = Worker(
        store,
        model,
        sandbox,
        executor,
        context=RepositoryContext(sandbox, store),
        heartbeat_interval=0.01,
    )
    assert await worker.run_once()
    finished = store.get_task(task.id)
    assert finished.state == TaskState.SUCCEEDED
    assert finished.result == "Good morning! How can I help?"
    assert finished.sandbox_id is None and finished.checkpoint_id is None
    assert sandbox.creates == sandbox.closed == 0
    sandbox.execute.assert_not_awaited()
    assert store.usage(task.id)["sandbox_seconds"] == 0
    assert model.generate.call_args.args[2] == (
        [] if repository is None else executor.definitions()
    )


async def test_chat_retains_previous_messages_without_replaying_tools(store):
    sandbox = FakeSandbox()
    model = FakeModel(
        [ModelReply(text="Nice to meet you, Ada."), ModelReply(text="Your name is Ada.")]
    )
    worker = Worker(
        store, model, sandbox, FakeExecutor(sandbox), context=RepositoryContext(sandbox, store)
    )
    first = store.create_task(TaskSpec(prompt="My name is Ada", session_id="chat"))
    await worker.run_once()
    second = store.create_task(TaskSpec(prompt="What is my name?", session_id="chat"))
    await worker.run_once()
    sent = model.histories[1]
    assert sent[-3:] == [
        {"role": "user", "parts": [{"text": first.spec.prompt}]},
        {"role": "model", "parts": [{"text": "Nice to meet you, Ada."}]},
        {"role": "user", "parts": [{"text": second.spec.prompt}]},
    ]
    assert store.get_task(second.id).state == TaskState.SUCCEEDED
    assert sandbox.creates == 0


async def test_approved_plan_stays_in_context_through_repository_execution(store):
    plan = (
        "Add light, dark, and system themes.\n"
        + "Preserve existing navigation and accessible form controls.\n" * 60
        + "Listen for system preference changes and remove that listener on unmount."
    )
    sandbox = MemorySandbox()
    model = FakeModel(
        [
            ModelReply(text=plan),
            ModelReply(calls=[ToolCall(name="read_file", arguments={"path": "client/App.tsx"})]),
            ModelReply(
                calls=[
                    ToolCall(
                        name="write_file",
                        arguments={"path": "client/theme.ts", "content": "export const theme = 1;"},
                    )
                ]
            ),
            ModelReply(text="Implemented the theme switcher."),
        ]
    )
    worker = Worker(
        store,
        model,
        sandbox,
        AgentToolExecutor(sandbox),
        context=RepositoryContext(sandbox, store),
    )
    store.create_task(
        TaskSpec(prompt="Plan a theme switcher", repository="owner/repo", session_id="themes")
    )
    await worker.run_once()
    approval = store.create_task(
        TaskSpec(prompt="Yes please implement this", repository="owner/repo", session_id="themes")
    )
    await worker.run_once()
    assert store.get_task(approval.id).state == TaskState.SUCCEEDED
    assert [item.call.name for item in store.invocations(approval.id)] == [
        "read_file",
        "write_file",
    ]
    for history in model.histories[1:]:
        assert {"role": "model", "parts": [{"text": plan}]} in history
        assert {"role": "user", "parts": [{"text": approval.spec.prompt}]} in history
    assert store.get_task(approval.id).checkpoint_id is not None


async def test_model_outage_pauses_and_resumes_without_repeating_completed_edit(store):
    from merlin.contracts import ModelServiceError

    class OutageModel(FakeModel):
        async def generate(self, task, history, tools):
            reply = await super().generate(task, history, tools)
            if isinstance(reply, Exception):
                raise reply
            return reply

    sandbox = MemorySandbox()
    model = OutageModel(
        [
            ModelReply(
                calls=[ToolCall(name="write_file", arguments={"path": "a.py", "content": "x=1"})]
            ),
            ModelServiceError(503),
            ModelReply(text="The saved edit is complete."),
        ]
    )
    worker = Worker(store, model, sandbox, AgentToolExecutor(sandbox))
    task = store.create_task(TaskSpec(prompt="Implement this", repository="owner/repo"))
    await worker.run_once()
    assert store.get_task(task.id).state == TaskState.PAUSED
    assert "/resume" in store.get_task(task.id).error
    assert store.get_task(task.id).checkpoint_id is not None
    store.resume(task.id)
    await worker.run_once()
    assert store.get_task(task.id).state == TaskState.SUCCEEDED
    assert [call.name for call in sandbox.calls] == ["write_file"]


@pytest.mark.parametrize("name", ["bash", "github_publish_branch", "spawn_child"])
async def test_no_repository_refuses_unexpected_model_tools_server_side(store, name):
    task = store.create_task(TaskSpec(prompt="Hello"))
    sandbox = FakeSandbox()
    executor = FakeExecutor(sandbox)
    executor.execute = AsyncMock(side_effect=AssertionError("Tool must not execute"))
    model = FakeModel([ModelReply(calls=[ToolCall(name=name)]), ModelReply(text="Hello!")])
    await Worker(store, model, sandbox, executor).run_once()
    assert store.get_task(task.id).state == TaskState.SUCCEEDED
    assert sandbox.creates == 0
    executor.execute.assert_not_awaited()
    assert not store.approvals(task.id)
    assert not store.invocations(task.id)[0].result.ok


async def test_coding_checkpoint_survives_an_intervening_conversation(store):
    sandbox = MemorySandbox()
    sandbox.create = AsyncMock(wraps=sandbox.create)
    sandbox.restore = AsyncMock(wraps=sandbox.restore)

    def call():
        return ModelReply(
            calls=[ToolCall(name="write_file", arguments={"path": "a.py", "content": "x=1"})]
        )

    model = FakeModel(
        [
            call(),
            ModelReply(text="Saved."),
            ModelReply(text="Good morning!"),
            call(),
            ModelReply(text="Updated."),
        ]
    )
    worker = Worker(store, model, sandbox, AgentToolExecutor(sandbox))
    first = store.create_task(
        TaskSpec(prompt="Write a.py", repository="owner/repo", session_id="code")
    )
    await worker.run_once()
    original = store.latest_checkpoint(first.id)
    assert original is not None
    chat = store.create_task(
        TaskSpec(prompt="Good morning!", repository="owner/repo", session_id="code")
    )
    await worker.run_once()
    assert store.get_task(chat.id).checkpoint_id is None
    assert sandbox.create.await_count == 1
    following = store.create_task(
        TaskSpec(prompt="Update a.py", repository="owner/repo", session_id="code")
    )
    await worker.run_once()
    assert store.get_task(following.id).state == TaskState.SUCCEEDED
    assert (
        sandbox.restore.await_count == 1 and sandbox.create.await_count == len(sandbox.closed) == 2
    )
    assert sandbox.restore.call_args.args[1].sha256 == original.sha256
    assert store.latest_checkpoint(following.id).task_id == following.id


@pytest.mark.parametrize("repositories", [(None, "owner/repo"), ("owner/repo", None)])
def test_chat_session_cannot_switch_repository_identity(store, repositories):
    first, second = repositories
    store.create_task(TaskSpec(prompt="Hello", repository=first, session_id="bound"))
    with pytest.raises(ValueError, match="cannot switch repositories"):
        store.create_task(TaskSpec(prompt="Hello", repository=second, session_id="bound"))
    assert len(store.list_tasks("bound")) == 1


async def test_chat_context_is_bounded_without_reading_instructions(store):
    sandbox = FakeSandbox()
    model = FakeModel([ModelReply(text="a" * 2000) for _ in range(4)])
    context = RepositoryContext(sandbox, store, max_chars=6000)
    worker = Worker(store, model, sandbox, FakeExecutor(sandbox), context=context)
    for index in range(4):
        task = store.create_task(TaskSpec(prompt=f"Question {index}", session_id="bounded"))
        await worker.run_once()
        assert store.get_task(task.id).state == TaskState.SUCCEEDED
    assert all(len(json.dumps(history)) <= 6000 for history in model.histories)
    assert sandbox.creates == 0
