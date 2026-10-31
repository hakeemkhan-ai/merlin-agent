import unittest

from merlin.agent import AgentToolExecutor
from merlin.contracts import ApprovalRequired, Task, TaskSpec, ToolCall, ToolResult, Workspace


class Publisher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, task: Task, call: ToolCall) -> ToolResult:
        self.calls.append(call.id)
        return ToolResult(call_id=call.id)


class Sandbox:
    async def execute(self, workspace: Workspace, call: ToolCall) -> ToolResult:
        return ToolResult(call_id=call.id, output="sandbox only")


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_unapproved_publishing_never_reaches_publisher(self) -> None:
        publisher = Publisher()
        executor = AgentToolExecutor(Sandbox(), publisher)
        task = Task(spec=TaskSpec(prompt="do work", repository="owner/repo"))
        with self.assertRaises(ApprovalRequired):
            await executor.execute(task, Workspace(id="sandbox"), ToolCall(name="github_create_pr"))
        self.assertEqual(publisher.calls, [])

    async def test_children_cannot_publish_even_with_inherited_permissions(self) -> None:
        publisher = Publisher()
        executor = AgentToolExecutor(Sandbox(), publisher)
        task = Task(
            spec=TaskSpec(
                prompt="do work",
                repository="owner/repo",
                parent_id="parent",
                permissions=["create_pr"],
            )
        )
        result = await executor.execute(
            task, Workspace(id="sandbox"), ToolCall(name="github_create_pr")
        )
        self.assertFalse(result.ok)
        self.assertEqual(publisher.calls, [])

    async def test_tool_arguments_cannot_grant_permission(self) -> None:
        publisher = Publisher()
        executor = AgentToolExecutor(Sandbox(), publisher)
        task = Task(spec=TaskSpec(prompt="do work", repository="owner/repo"))
        call = ToolCall(name="github_create_issue", arguments={"permissions": ["create_issue"]})
        with self.assertRaises(ApprovalRequired):
            await executor.execute(task, Workspace(id="sandbox"), call)
        self.assertEqual(publisher.calls, [])

    async def test_authorized_publish_and_sandbox_dispatch(self) -> None:
        publisher = Publisher()
        executor = AgentToolExecutor(Sandbox(), publisher)
        task = Task(
            spec=TaskSpec(
                prompt="do work",
                repository="owner/repo",
                permissions=["publish_branch"],
            )
        )
        call = ToolCall(name="github_publish_branch")
        result = await executor.execute(task, Workspace(id="sandbox"), call)
        self.assertTrue(result.ok)
        self.assertEqual(publisher.calls, [call.id])
        result = await executor.execute(task, Workspace(id="sandbox"), ToolCall(name="read_file"))
        self.assertEqual(result.output, "sandbox only")
