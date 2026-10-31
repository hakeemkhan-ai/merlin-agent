from __future__ import annotations

import json
import unittest
from copy import deepcopy
from datetime import timedelta

from merlin.context import RepositoryContext, compact_history
from merlin.contracts import Task, TaskSpec, TaskState, ToolCall, ToolResult, Workspace
from merlin.integrations.gemini import provider_history


def completed_tool_task(call: ToolCall, result: ToolResult) -> Task:
    return Task(
        spec=TaskSpec(prompt="Summarize the completed operation", repository="owner/repo"),
        history=[
            {"role": "user", "parts": [{"text": "Summarize the completed operation"}]},
            {
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
                "_merlin_calls": [call.model_dump(mode="json")],
            },
            {
                "role": "user",
                "parts": [
                    {
                        "function_response": {
                            "id": call.id,
                            "name": call.name,
                            "response": result.model_dump(mode="json"),
                        }
                    }
                ],
            },
        ],
    )


class FakeSandbox:
    def __init__(self) -> None:
        self.files = {
            "MERLIN.md": "root guidance",
            "src/MERLIN.md": "source guidance",
            "other/MERLIN.md": "unrelated",
        }
        self.reads: list[str] = []

    async def execute(self, workspace: Workspace, call: ToolCall) -> ToolResult:
        path = call.arguments["path"]
        self.reads.append(path)
        return ToolResult(
            call_id=call.id, ok=path in self.files, output=self.files.get(path, "missing")
        )


class FakeStore:
    def __init__(self, tasks: list[Task] | None = None) -> None:
        self.tasks = tasks or []

    def list_tasks(self, session_id: str | None = None) -> list[Task]:
        return self.tasks

    def events(self, task_id: str) -> list:
        return []


class ContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_publication_fits_with_session_context_without_losing_signed_parts(self):
        call = ToolCall(
            name="github_publish_branch",
            arguments={"files": [{"path": "PROGRESS.md", "content": "x" * 55000}]},
        )
        task = completed_tool_task(call, ToolResult(call_id=call.id, output="Published"))
        original = deepcopy(task.history)
        previous = [
            Task(
                spec=TaskSpec(
                    prompt="p" * 4000,
                    repository=task.spec.repository,
                    session_id=task.spec.session_id,
                ),
                state=TaskState.SUCCEEDED,
                result="r" * 2000,
                created_at=task.created_at - timedelta(minutes=index + 1),
            )
            for index in range(5)
        ]
        context = await RepositoryContext(
            FakeSandbox(), FakeStore([*previous, task]), max_chars=100000
        ).build(task, Workspace(id="fake"))
        self.assertEqual(context[-len(original) :], original)
        self.assertEqual(task.history, original)
        self.assertLessEqual(len(json.dumps(provider_history(context), default=str)), 100000)

    async def test_tool_output_budget_deduplicates_only_identical_text(self):
        for duplicate in (True, False):
            with self.subTest(duplicate=duplicate):
                output = "x" * 14000
                call = ToolCall(name="read_file", arguments={"path": "README.md"})
                result = ToolResult(
                    call_id=call.id,
                    output=output,
                    data={"output": output if duplicate else "y" * 14000, "sha256": "hash"},
                )
                task = completed_tool_task(call, result)
                original = deepcopy(task.history)
                context = RepositoryContext(FakeSandbox(), FakeStore(), max_chars=24000)
                if duplicate:
                    built = await context.build(task, Workspace(id="fake"))
                    self.assertEqual(built[1:], original)
                    self.assertLessEqual(len(json.dumps(provider_history(built))), 24000)
                else:
                    with self.assertRaisesRegex(ValueError, "complete tool interaction"):
                        await context.build(task, Workspace(id="fake"))
                self.assertEqual(task.history, original)

    async def test_oversized_native_publication_is_still_rejected_without_truncation(self):
        call = ToolCall(
            name="github_publish_branch",
            arguments={"files": [{"path": "PROGRESS.md", "content": "x" * 55000}]},
        )
        task = completed_tool_task(call, ToolResult(call_id=call.id, output="Published"))
        original = deepcopy(task.history)
        with self.assertRaisesRegex(ValueError, "complete tool interaction"):
            await RepositoryContext(FakeSandbox(), FakeStore(), max_chars=40000).build(
                task, Workspace(id="fake")
            )
        self.assertEqual(task.history, original)

    async def test_nested_instructions_apply_only_to_touched_subtrees(self) -> None:
        task = Task(
            spec=TaskSpec(prompt="fix source", repository="owner/repo"),
            history=[
                {
                    "role": "model",
                    "parts": [],
                    "_merlin_calls": [
                        {"id": "x", "name": "read_file", "arguments": {"path": "src/app.py"}}
                    ],
                },
                {
                    "role": "user",
                    "parts": [{"function_response": {"id": "x", "response": {"ok": True}}}],
                },
            ],
        )
        sandbox = FakeSandbox()
        context = await RepositoryContext(sandbox, FakeStore()).build(task, Workspace(id="fake"))
        metadata = json.loads(context[0]["parts"][0]["text"])
        self.assertEqual(
            [entry["path"] for entry in metadata["instructions"]], ["MERLIN.md", "src/MERLIN.md"]
        )
        self.assertNotIn("other/MERLIN.md", sandbox.reads)

    async def test_previous_session_prompts_and_results_are_referenced(self) -> None:
        task = Task(spec=TaskSpec(prompt="continue", repository="owner/repo", session_id="session"))
        previous = Task(
            spec=TaskSpec(
                prompt="Preserve API compatibility", repository="owner/repo", session_id="session"
            ),
            state=TaskState.SUCCEEDED,
            result="Fixed parser",
            created_at=task.created_at - timedelta(minutes=1),
        )
        context = await RepositoryContext(FakeSandbox(), FakeStore([previous, task])).build(
            task, Workspace(id="fake")
        )
        metadata = json.loads(context[0]["parts"][0]["text"])
        self.assertIn(
            "Preserve API compatibility", metadata["previous_session_tasks"][0]["prompt_excerpt"]
        )
        self.assertEqual(
            metadata["previous_session_tasks"][0]["full_history_reference"], previous.id
        )

    def test_compaction_preserves_prompt_and_native_signed_blocks(self) -> None:
        history = [
            {"role": "model", "parts": [{"text": "historical decision " + "x" * 1000}]}
            for _ in range(20)
        ]
        native = {
            "role": "model",
            "parts": [
                {
                    "function_call": {"name": "read_file", "args": {"path": "a"}},
                    "thought_signature": "opaque-provider-signature",
                }
            ],
            "_merlin_calls": [{"id": "recent", "name": "read_file", "arguments": {"path": "a"}}],
        }
        response = {
            "role": "user",
            "parts": [
                {
                    "function_response": {
                        "id": "recent",
                        "name": "read_file",
                        "response": {"ok": True, "output": "code"},
                    }
                }
            ],
        }
        history.extend([native, response])
        original = deepcopy(history)
        task = Task(
            spec=TaskSpec(prompt="Do not change public APIs", repository="owner/repo"),
            history=history,
        )
        compacted = compact_history(task, budget=6000, event_sequence=55)
        self.assertIn(native, compacted)
        self.assertIn(response, compacted)
        summary = json.loads(compacted[0]["parts"][0]["text"])
        self.assertEqual(summary["objective_and_constraints_verbatim"], task.spec.prompt)
        self.assertEqual(summary["event_reference"]["through_sequence"], 55)
        self.assertEqual(task.history, original)

    def test_incomplete_interaction_cannot_be_compacted(self) -> None:
        task = Task(
            spec=TaskSpec(prompt="task", repository="owner/repo"),
            history=[
                {
                    "role": "model",
                    "parts": [{"text": "x" * 9000}],
                    "_merlin_calls": [
                        {"id": "unanswered", "name": "bash", "arguments": {"command": "run"}}
                    ],
                }
            ],
        )
        with self.assertRaisesRegex(ValueError, "incomplete"):
            compact_history(task, budget=1000)

    def test_small_history_preserved_exactly(self) -> None:
        task = Task(
            spec=TaskSpec(prompt="task", repository="owner/repo"),
            history=[{"role": "user", "parts": [{"text": "task"}]}],
        )
        self.assertEqual(compact_history(task, budget=10000), task.history)
