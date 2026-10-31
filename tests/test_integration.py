"""Exercise actual store, worker, and dispatch with only external services replaced."""

import unittest

from sqlalchemy import update

from merlin.agent import AgentToolExecutor
from merlin.contracts import (
    Checkpoint,
    ModelReply,
    TaskSpec,
    TaskState,
    ToolCall,
    ToolResult,
    Workspace,
)
from merlin.persistence import SQLTaskStore
from merlin.persistence.models import TaskRow
from merlin.worker import Worker


class MemorySandbox:
    def __init__(self):
        self.calls = []
        self.closed = []
        self.checkpoints = []

    async def create(self, task):
        return Workspace(id=task.id)

    async def connect(self, sandbox_id):
        return Workspace(id=sandbox_id)

    async def execute(self, workspace, call):
        if call.name == "read_file" and call.arguments.get("path", "").endswith("MERLIN.md"):
            return ToolResult(call_id=call.id, ok=False, output="No such file or directory")
        self.calls.append(call)
        return ToolResult(call_id=call.id, output="edited")

    async def checkpoint(self, workspace, task_id):
        checkpoint = Checkpoint(task_id=task_id, artifact="test.tar", sha256="0" * 64)
        self.checkpoints.append(checkpoint)
        return checkpoint

    async def restore(self, workspace, checkpoint):
        pass

    async def close(self, workspace):
        self.closed.append(workspace.id)


class ScriptedModel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = 0
        self.histories = []

    async def generate(self, task, history, tools):
        self.calls += 1
        self.histories.append(history)
        return next(self.replies)


class Publisher:
    def __init__(self):
        self.calls = []

    async def execute(self, task, call):
        self.calls.append(call)
        return ToolResult(call_id=call.id, output="https://github.com/test/repo/issues/1")


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = SQLTaskStore("sqlite:///:memory:")
        self.store.create_schema()
        self.sandbox = MemorySandbox()

    def tearDown(self):
        self.store.close()

    async def test_parallel_model_calls_receive_one_complete_response_block(self):
        calls = [
            ToolCall(name="read_file", arguments={"path": "main.py"}),
            ToolCall(name="list_files"),
        ]
        model = ScriptedModel([ModelReply(calls=calls), ModelReply(text="Read both")])
        task = self.store.create_task(TaskSpec(prompt="inspect", repository="test/repo"))
        await Worker(self.store, model, self.sandbox, AgentToolExecutor(self.sandbox)).run_once()
        history = self.store.get_task(task.id).history
        responses = [
            entry
            for entry in history
            if any("function_response" in part for part in entry.get("parts", []))
        ]
        self.assertEqual(len(responses), 1)
        self.assertEqual(
            [part["function_response"]["id"] for part in responses[0]["parts"]],
            [call.id for call in calls],
        )

    async def test_end_to_end_edit_persists_events_checkpoints_and_result(self):
        call = ToolCall(name="write_file", arguments={"path": "main.py", "content": "print(1)"})
        model = ScriptedModel([ModelReply(calls=[call]), ModelReply(text="Changed and verified")])
        task = self.store.create_task(TaskSpec(prompt="fix", repository="test/repo"))
        worker = Worker(self.store, model, self.sandbox, AgentToolExecutor(self.sandbox))
        self.assertTrue(await worker.run_once())
        result = self.store.get_task(task.id)
        self.assertEqual(result.state, TaskState.SUCCEEDED)
        self.assertEqual(result.result, "Changed and verified")
        self.assertEqual(len(self.sandbox.calls), 1)
        self.assertGreaterEqual(len(self.sandbox.checkpoints), 3)
        events = self.store.events(task.id)
        self.assertIn("tool.finished", [event.kind for event in events])
        finished = [event for event in events if event.kind == "tool.finished"]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0].data["result"]["call_id"], call.id)
        self.assertEqual(self.store.usage(task.id)["calls"], 2)

    async def test_reviewed_github_call_resumes_and_executes_once(self):
        call = ToolCall(name="github_create_issue", arguments={"title": "Bug", "body": "Details"})
        model = ScriptedModel([ModelReply(calls=[call]), ModelReply(text="Issue created")])
        publisher = Publisher()
        task = self.store.create_task(TaskSpec(prompt="investigate", repository="test/repo"))
        worker = Worker(self.store, model, self.sandbox, AgentToolExecutor(self.sandbox, publisher))
        await worker.run_once()
        self.assertEqual(self.store.get_task(task.id).state, TaskState.WAITING_FOR_APPROVAL)
        self.assertEqual(publisher.calls, [])
        approval = self.store.approvals(task.id)[0]
        self.store.decide_approval(approval.id, True)
        await worker.run_once()
        self.assertEqual(self.store.get_task(task.id).state, TaskState.SUCCEEDED)
        self.assertEqual(len(publisher.calls), 1)
        self.assertEqual(model.calls, 2)

        # Simulate losing only the history append after a completed reviewed operation.
        history = self.store.get_task(task.id).history[:2]
        with self.store.engine.begin() as connection:
            connection.execute(
                update(TaskRow)
                .where(TaskRow.id == task.id)
                .values(
                    state="queued", history=history, result=None, worker_id=None, lease_until=None
                )
            )
        worker.model = ScriptedModel([ModelReply(text="Recovered saved result")])
        await worker.run_once()
        self.assertEqual(self.store.get_task(task.id).state, TaskState.SUCCEEDED)
        self.assertEqual(len(publisher.calls), 1)
        self.assertEqual(len(self.store.approvals(task.id)), 1)

    async def test_requested_changes_generate_a_fresh_review_before_any_publishing(self):
        original = ToolCall(name="github_create_pr", arguments={"title": "Docs", "body": "Summary"})
        revised = ToolCall(
            name="github_create_pr",
            arguments={
                "title": "Docs",
                "body": "Typecheck failed: next not found; dependencies absent.",
            },
        )
        model = ScriptedModel(
            [
                ModelReply(calls=[original]),
                ModelReply(calls=[revised]),
                ModelReply(text="PR created"),
            ]
        )
        publisher = Publisher()
        task = self.store.create_task(TaskSpec(prompt="Prepare a PR", repository="test/repo"))
        worker = Worker(self.store, model, self.sandbox, AgentToolExecutor(self.sandbox, publisher))
        await worker.run_once()
        old = self.store.approvals(task.id)[0]
        self.store.request_approval_changes(old.id, "Disclose the failed typecheck")
        await worker.run_once()
        self.assertEqual(publisher.calls, [])
        self.assertEqual(self.store.get_task(task.id).state, TaskState.WAITING_FOR_APPROVAL)
        approvals = self.store.approvals(task.id)
        pending = next(item for item in approvals if item.status == "pending")
        self.assertNotEqual(pending.id, old.id)
        self.assertEqual(pending.call, revised)
        self.assertIn("Disclose the failed typecheck", str(model.histories[1]))
        self.store.decide_approval(pending.id, True)
        await worker.run_once()
        self.assertEqual(publisher.calls, [revised])
        self.assertEqual(self.store.get_task(task.id).state, TaskState.SUCCEEDED)

    async def test_child_publication_is_denied_without_waiting_for_approval(self):
        parent = self.store.create_task(TaskSpec(prompt="delegate", repository="test/repo"))
        parent_lease = self.store.claim("parent")
        child = self.store.create_task(
            TaskSpec(prompt="inspect", repository="test/repo", parent_id=parent.id),
            parent_lease=parent_lease,
        )
        publisher = Publisher()
        model = ScriptedModel(
            [
                ModelReply(calls=[ToolCall(name="github_create_issue", arguments={"title": "x"})]),
                ModelReply(text="Returned findings to parent"),
            ]
        )
        worker = Worker(self.store, model, self.sandbox, AgentToolExecutor(self.sandbox, publisher))
        await worker.run_once()
        self.assertEqual(self.store.get_task(child.id).state, TaskState.SUCCEEDED)
        self.assertEqual(self.store.approvals(child.id), [])
        self.assertEqual(publisher.calls, [])
