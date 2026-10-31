"""Typed, explicit tool dispatch. All repository execution stays in the sandbox."""

from __future__ import annotations

from typing import Any

from merlin.contracts import (
    ApprovalRequired,
    GitHubPublisher,
    SandboxClient,
    Task,
    TaskStore,
    ToolCall,
    ToolResult,
    Workspace,
)

PUBLISHING_PERMISSIONS = {
    "github_publish_branch": "publish_branch",
    "github_create_pr": "create_pr",
    "github_create_issue": "create_issue",
}
MUTATING_TOOLS = frozenset(
    {
        "bash",
        "write_file",
        "replace_text",
        "terminate_process",
        "spawn_child",
        "integrate_child",
        "browser_click",
        *PUBLISHING_PERMISSIONS,
    }
)

SAFE_REPLAY_TOOLS = frozenset(
    {
        "read_file",
        "search",
        "list_files",
        "poll_process",
        "child_status",
        "await_children",
        "spawn_child",
        "github_read",
        "github_prepare_publish",
        "lsp_definition",
        "lsp_references",
        "lsp_hover",
        "lsp_symbols",
        "lsp_diagnostics",
        "history_lookup",
        "repository_map",
        "remember_context",
    }
)


def requires_approval(task: Task, call: ToolCall) -> bool:
    """Permissions are service-owned task state, never model-supplied arguments."""
    permission = PUBLISHING_PERMISSIONS.get(call.name)
    return permission is not None and not (
        permission in task.spec.permissions or call.name in task.spec.permissions
    )


class AgentToolExecutor:
    def __init__(
        self,
        sandbox: SandboxClient,
        publisher: GitHubPublisher | None = None,
        store: TaskStore | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.publisher = publisher
        self.store = store

    def definitions(self) -> list[dict[str, Any]]:
        from merlin.tools import sandbox_tool_definitions

        definitions = sandbox_tool_definitions()
        from merlin.browser import browser_tool_definitions
        from merlin.lsp import lsp_tool_definitions

        definitions.extend(browser_tool_definitions())
        definitions.extend(lsp_tool_definitions())
        if self.publisher is not None:
            from merlin.integrations.github import github_tool_definitions

            definitions.extend(github_tool_definitions())
        return definitions

    async def execute(self, task: Task, workspace: Workspace, call: ToolCall) -> ToolResult:
        if call.name.startswith("github_"):
            if call.name not in {*PUBLISHING_PERMISSIONS, "github_read", "github_prepare_publish"}:
                return ToolResult(call_id=call.id, ok=False, output="Unknown GitHub tool")
            if task.spec.parent_id is not None and call.name != "github_read":
                return ToolResult(call_id=call.id, ok=False, output="Children cannot publish")
            if requires_approval(task, call):
                raise ApprovalRequired(call.name)
            if self.publisher is None:
                return ToolResult(call_id=call.id, ok=False, output="GitHub is not configured")
            return await self.publisher.execute(task, call)
        if call.name not in {definition["name"] for definition in self.definitions()}:
            return ToolResult(call_id=call.id, ok=False, output="Unknown agent tool")
        return await self.sandbox.execute(workspace, call)
