"""Context caching, instruction fencing, and memory regressions; no external providers."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from datetime import timedelta

import pytest

from merlin.context import RepositoryContext, compact_history, provider_history
from merlin.contracts import (
    BudgetExceeded,
    Task,
    TaskSpec,
    TaskState,
    ToolCall,
    ToolResult,
    Workspace,
)


class Sandbox:
    def __init__(self) -> None:
        self.files = {
            "MERLIN.md": "Keep public interfaces compatible.",
            "src/MERLIN.md": "Use types.",
        }
        self.reads: list[str] = []
        self.active = self.high_water = 0

    async def execute(self, workspace: Workspace, call: ToolCall) -> ToolResult:
        self.active += 1
        self.high_water = max(self.high_water, self.active)
        try:
            await asyncio.sleep(0)
            path = call.arguments["path"]
            self.reads.append(path)
            return ToolResult(
                call_id=call.id, ok=path in self.files, output=self.files.get(path, "missing")
            )
        finally:
            self.active -= 1


class Store:
    def __init__(self, tasks: list[Task] | None = None) -> None:
        self.tasks = tasks or []
        self.checkpoints: dict[str, dict] = {}
        self.saved = 0

    def list_tasks(self, session_id: str | None = None) -> list[Task]:
        return self.tasks

    def get_task(self, task_id: str) -> Task:
        return next(task for task in self.tasks if task.id == task_id)

    def latest_event_sequence(self, task_id: str) -> int:
        return 7

    def context_checkpoint(self, task_id: str) -> dict | None:
        return self.checkpoints.get(task_id)

    def save_context_checkpoint(self, lease, payload: dict, history_sequence: int) -> None:
        self.checkpoints[lease.task_id] = deepcopy(payload)
        self.saved += 1


def task_with_history() -> Task:
    return Task(
        spec=TaskSpec(
            prompt="Preserve the public API", repository="owner/repo", session_id="session"
        ),
        worker_id="worker",
        generation=1,
        history=[{"role": "user", "parts": [{"text": "Preserve the public API"}]}],
    )


async def test_instruction_reads_are_batched_cached_and_invalidated():
    task = task_with_history()
    task.history.append(
        {
            "role": "model",
            "parts": [],
            "_merlin_calls": [
                {"id": "read", "name": "read_file", "arguments": {"path": "src/app.py"}}
            ],
        }
    )
    sandbox, store = Sandbox(), Store([task])
    context = RepositoryContext(sandbox, store)
    workspace = Workspace(id="workspace")
    await context.build(task, workspace)
    first = list(sandbox.reads)
    await context.build(task, workspace)
    assert sandbox.reads == first
    assert 1 < sandbox.high_water <= 4
    sandbox.files["src/MERLIN.md"] = "New source guidance."
    context.invalidate(workspace.id, ["src/MERLIN.md"])
    rebuilt = await context.build(task, workspace)
    assert "New source guidance." in json.dumps(rebuilt)
    assert sandbox.reads.count("MERLIN.md") == 1
    assert sandbox.reads.count("src/MERLIN.md") == 2


async def test_first_write_must_expose_new_scope_guidance_to_model():
    task, sandbox = task_with_history(), Sandbox()
    context = RepositoryContext(sandbox, Store([task]))
    workspace = Workspace(id="workspace")
    call = ToolCall(name="write_file", arguments={"path": "src/app.py", "content": "x = 1"})
    denial = await context.before_tool(task, workspace, call)
    assert denial is not None and not denial.ok and not denial.uncertain
    assert "Keep public interfaces compatible." in denial.output
    assert "Use types." in denial.output
    # Another call in the same pending group must not treat a denial as model exposure.
    assert await context.before_tool(task, workspace, call) is not None
    task.history.append({"role": "user", "parts": [{"text": denial.output}]})
    await context.build(task, workspace)
    assert await context.before_tool(task, workspace, call) is None
    sandbox.files["src/MERLIN.md"] = "Changed source guidance."
    context.invalidate(workspace.id, ["src/MERLIN.md"])
    assert await context.before_tool(task, workspace, call) is not None


async def test_missing_instructions_do_not_reject_shell_and_workspace_cache_is_isolated():
    task, sandbox = task_with_history(), Sandbox()
    sandbox.files.clear()
    context = RepositoryContext(sandbox, Store([task]))
    call = ToolCall(name="bash", arguments={"command": "ls"})
    assert await context.before_tool(task, Workspace(id="one"), call) is None
    sandbox.files["MERLIN.md"] = "Other workspace instructions."
    denial = await context.before_tool(task, Workspace(id="two"), call)
    assert denial is not None and "Other workspace instructions." in denial.output


async def test_instruction_exposure_is_scoped_to_task_even_when_workspace_is_reused():
    task, sandbox = task_with_history(), Sandbox()
    context = RepositoryContext(sandbox, Store([task]))
    workspace = Workspace(id="reused")
    await context.build(task, workspace)
    other = task_with_history()
    call = ToolCall(name="write_file", arguments={"path": "app.py", "content": "x = 1"})
    assert await context.before_tool(task, workspace, call) is None
    assert await context.before_tool(other, workspace, call) is not None


async def test_instruction_permission_failure_is_not_cached_as_missing():
    class Unreadable(Sandbox):
        async def execute(self, workspace, call):
            return ToolResult(call_id=call.id, ok=False, output="Permission denied")

    task = task_with_history()
    context = RepositoryContext(Unreadable(), Store([task]))
    denial = await context.before_tool(
        task, Workspace(id="workspace"), ToolCall(name="bash", arguments={"command": "ls"})
    )
    assert denial is not None and not denial.ok
    assert not context._instructions


async def test_ambiguous_instruction_error_is_not_treated_as_missing():
    class PermissionFailure(Sandbox):
        async def execute(self, workspace, call):
            return ToolResult(
                call_id=call.id, ok=False, output="Permission denied: missing read grant"
            )

    task = task_with_history()
    context = RepositoryContext(PermissionFailure(), Store([task]))
    denied = await context.before_tool(
        task, Workspace(id="workspace"), ToolCall(name="bash", arguments={"command": "ls"})
    )
    assert denied is not None and not denied.ok
    assert not context._instructions


async def test_mutation_revalidates_instructions_before_ttl_expires():
    task, sandbox = task_with_history(), Sandbox()
    context = RepositoryContext(sandbox, Store([task]), instruction_ttl_seconds=3600)
    workspace = Workspace(id="workspace")
    await context.build(task, workspace)
    sandbox.files["MERLIN.md"] = "Changed after a detached writer ran."
    denied = await context.before_tool(
        task,
        workspace,
        ToolCall(name="write_file", arguments={"path": "app.py", "content": "x = 1"}),
    )
    assert denied is not None and "Changed after a detached writer ran." in denied.output


async def test_empty_instruction_error_is_not_treated_as_missing():
    class EmptyFailure(Sandbox):
        async def execute(self, workspace, call):
            return ToolResult(call_id=call.id, ok=False)

    task = task_with_history()
    context = RepositoryContext(EmptyFailure(), Store([task]))
    denied = await context.before_tool(
        task, Workspace(id="workspace"), ToolCall(name="bash", arguments={"command": "ls"})
    )
    assert denied is not None and not denied.ok
    assert not context._instructions


async def test_background_writers_cannot_hide_changed_instructions_behind_ttl():
    task, sandbox = task_with_history(), Sandbox()
    context = RepositoryContext(sandbox, Store([task]))
    workspace = Workspace(id="workspace")
    await context.build(task, workspace)
    background = ToolCall(name="bash", arguments={"command": "watch", "background": True})
    context.after_tool(
        workspace,
        background,
        ToolResult(call_id=background.id, data={"pid": 23, "state": "running"}),
    )
    await context.build(task, workspace)
    sandbox.files["MERLIN.md"] = "Changed while the background writer runs."
    denied = await context.before_tool(
        task,
        workspace,
        ToolCall(name="write_file", arguments={"path": "app.py", "content": "x = 1"}),
    )
    assert denied is not None and "Changed while the background writer runs." in denied.output
    context.release(workspace.id)
    assert not context._background_writers


async def test_repository_map_is_bounded_cached_and_invalidated_without_reading_source():
    class Tree(Sandbox):
        def __init__(self):
            super().__init__()
            self.listings = 0

        async def execute(self, workspace, call):
            assert call.name == "list_files"
            self.listings += 1
            paths = [f"src/module_{index}.py" for index in range(250)]
            paths.extend([".env", ".git/config", "../outside.py", "README.md"])
            return ToolResult(call_id=call.id, output="\n".join(paths))

    task, sandbox = task_with_history(), Tree()
    context = RepositoryContext(sandbox, Store([task]))
    workspace = Workspace(id="workspace")
    call = ToolCall(name="repository_map", arguments={"limit": 20})
    first = await context.execute(task, call, workspace=workspace)
    assert first is not None and first.ok
    mapped = json.loads(first.output)
    assert len(mapped["files"]) == 20 and mapped["truncated"]
    assert mapped["listed_files"] == 251 and mapped["language_counts"][".py"] == 250
    assert ".env" not in first.output and ".git/config" not in first.output
    await context.execute(task, call, workspace=workspace)
    assert sandbox.listings == 1
    write = ToolCall(name="write_file", arguments={"path": "src/new.py", "content": "x=1"})
    context.after_tool(workspace, write, ToolResult(call_id=write.id))
    await context.execute(task, call, workspace=workspace)
    assert sandbox.listings == 2


async def test_history_lookup_uses_metadata_and_bounded_page_before_visible_projection():
    task = task_with_history()
    previous = Task(
        spec=TaskSpec(prompt="prior", repository="owner/repo", session_id="session"),
        created_at=task.created_at - timedelta(minutes=1),
    )

    class PagedStore(Store):
        def get_task(self, task_id):
            raise AssertionError("Lookup must not fetch a full historical task")

        def get_task_metadata(self, task_id):
            assert task_id == previous.id
            return previous

        def history_page(self, task_id, cursor, limit, max_chars):
            assert task_id == previous.id and cursor == 0 and limit == 10 and max_chars == 48000
            return {
                "items": [
                    {
                        "role": "model",
                        "parts": [
                            {"text": "visible"},
                            {"thought": True, "text": "hidden", "thought_signature": "signature"},
                        ],
                    },
                    {"omitted": True, "entry": 1, "characters": 100000},
                ],
                "next_cursor": 2,
                "total_entries": 5,
            }

    result = await RepositoryContext(Sandbox(), PagedStore([task, previous])).execute(
        task, ToolCall(name="history_lookup", arguments={"task_id": previous.id})
    )
    assert result is not None and result.ok and result.data["next_cursor"] == 2
    assert "visible" in result.output and "omitted" in result.output
    assert "hidden" not in result.output and "signature" not in result.output


async def test_large_instruction_is_not_silently_truncated_before_edit():
    task, sandbox = task_with_history(), Sandbox()
    sandbox.files["MERLIN.md"] = "Instruction " * 4000
    context = RepositoryContext(sandbox, Store([task]))
    with pytest.raises(BudgetExceeded):
        await context.before_tool(
            task, Workspace(id="workspace"), ToolCall(name="bash", arguments={"command": "ls"})
        )


@pytest.mark.parametrize("key", ["resource", "result", "entries"])
def test_provider_projection_deduplicates_only_exact_structured_representations(key):
    value = [{"path": "a.py", "line": 7}]
    history = [
        {
            "role": "user",
            "parts": [
                {
                    "function_response": {
                        "response": {
                            "output": json.dumps(value),
                            "data": {key: value, "supported": True},
                        }
                    }
                }
            ],
        }
    ]
    original = deepcopy(history)
    projected = provider_history(history)
    assert projected[0]["parts"][0]["function_response"]["response"]["data"] == {"supported": True}
    assert history == original
    history[0]["parts"][0]["function_response"]["response"]["output"] = "different"
    assert key in provider_history(history)[0]["parts"][0]["function_response"]["response"]["data"]


async def test_failed_predecessor_retains_constraints_but_never_permissions():
    task = task_with_history()
    previous = Task(
        spec=TaskSpec(
            prompt="Never alter the stable API",
            repository="owner/repo",
            session_id="session",
            permissions=["publish_branch"],
        ),
        state=TaskState.FAILED,
        error="Tests failed",
        created_at=task.created_at - timedelta(minutes=1),
    )
    built = await RepositoryContext(Sandbox(), Store([previous, task])).build(task, None)
    text = json.dumps(built)
    assert "Never alter the stable API" in text and "Tests failed" in text
    assert "publish_branch" not in text


async def test_token_budget_never_splits_native_interactions_or_sends_oversized_context():
    task = task_with_history()

    async def count(_task, history):
        return len(json.dumps(provider_history(history)))

    context = RepositoryContext(
        Sandbox(), Store([task]), token_counter=count, max_input_tokens=100, token_reserve=10
    )
    with pytest.raises(BudgetExceeded):
        await context.build(task, None)


async def test_summary_checkpoint_survives_context_recreation_and_history_resolution():
    task = task_with_history()
    task.history.extend(
        {"role": "model", "parts": [{"text": "Finding " + str(index) + "x" * 400}]}
        for index in range(30)
    )
    store = Store([task])
    first = RepositoryContext(Sandbox(), store, max_chars=6000)
    built = await first.build(task, None)
    assert "conversation_checkpoint_summary" in json.dumps(built)
    assert store.saved == 1
    second = RepositoryContext(Sandbox(), store, max_chars=6000)
    assert await second.build(task, None) == built
    assert store.saved == 1
    task.history[1]["parts"][0]["text"] = "Corrected historical observation"
    changed = await second.build(task, None)
    assert "conversation_checkpoint_summary" in json.dumps(changed)
    assert store.saved == 2


async def test_history_lookup_is_bounded_and_denies_cross_session_native_thoughts():
    task = task_with_history()
    task.history.append(
        {
            "role": "model",
            "parts": [
                {"text": "Useful visible result"},
                {"text": "private reasoning", "thought": True, "thought_signature": "opaque"},
            ],
        }
    )
    outsider = Task(spec=TaskSpec(prompt="private", repository="owner/repo", session_id="other"))
    context = RepositoryContext(Sandbox(), Store([task, outsider]))
    result = await context.execute(task, ToolCall(name="history_lookup", arguments={"limit": 1}))
    assert result is not None and result.ok and result.data["next_cursor"] == 1
    result = await context.execute(task, ToolCall(name="history_lookup", arguments={"cursor": 1}))
    assert "Useful visible result" in result.output
    assert "private reasoning" not in result.output and "opaque" not in result.output
    denied = await context.execute(
        task, ToolCall(name="history_lookup", arguments={"task_id": outsider.id})
    )
    assert denied is not None and not denied.ok


async def test_remembered_constraints_are_bounded_idempotent_and_persisted():
    task, store = task_with_history(), Store()
    store.tasks = [task]
    context = RepositoryContext(Sandbox(), store)
    call = ToolCall(
        name="remember_context", arguments={"kind": "constraint", "text": "Keep API stable"}
    )
    first = await context.execute(task, call)
    second = await context.execute(task, call)
    assert first is not None and first.ok and second is not None and second.ok
    assert len(store.checkpoints[task.id]["memory"]) == 1
    rebuilt = await RepositoryContext(Sandbox(), store).build(task, None)
    assert "Keep API stable" in json.dumps(rebuilt)


def test_compaction_preserves_signed_native_parts_exactly():
    task = task_with_history()
    task.history.extend({"role": "model", "parts": [{"text": "x" * 1000}]} for _ in range(20))
    native = {
        "role": "model",
        "parts": [
            {"function_call": {"name": "read_file", "args": {}}, "thought_signature": "opaque"}
        ],
        "_merlin_calls": [{"id": "native", "name": "read_file", "arguments": {}}],
    }
    response = {
        "role": "user",
        "parts": [
            {"function_response": {"id": "native", "response": {"ok": True, "output": "current"}}}
        ],
    }
    task.history.extend([native, response])
    original = deepcopy(task.history)
    compacted = compact_history(task, budget=6000)
    assert compacted[-2:] == [native, response]
    assert task.history == original
