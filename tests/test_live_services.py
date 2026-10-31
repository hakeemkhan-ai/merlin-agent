"""Opt-in read/scratch provider checks, separate from default contract evidence."""

import os
import unittest

try:
    import pytest
except ImportError:
    pass
else:
    pytestmark = pytest.mark.live

from merlin.config import Settings
from merlin.contracts import Task, TaskSpec, ToolCall


@unittest.skipUnless(os.getenv("MERLIN_LIVE_PROVIDER_TESTS") == "1", "Live providers are opt-in")
class LiveProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_gemini_native_tool_history_roundtrip(self):
        from merlin.integrations.gemini import GeminiClient

        settings = Settings(max_output_tokens=1024)
        adapter = GeminiClient(settings)
        task = Task(
            spec=TaskSpec(
                prompt="Use read_file", repository="test/fixtures", model=settings.gemini_model
            )
        )
        try:
            history = [
                {
                    "role": "user",
                    "parts": [
                        {"text": "Call read_file with path smoke.txt, then report its content."}
                    ],
                }
            ]
            tools = [
                {
                    "name": "read_file",
                    "description": "Read one fixture file",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                }
            ]
            reply = await adapter.generate(task, history, tools)
            self.assertEqual(len(reply.calls), 1)
            call = reply.calls[0]
            self.assertEqual(call.name, "read_file")
            self.assertEqual(call.arguments["path"], "smoke.txt")
            history.extend(
                [
                    reply.content,
                    {
                        "role": "user",
                        "parts": [
                            {
                                "function_response": {
                                    "name": call.name,
                                    "id": call.id,
                                    "response": {"ok": True, "output": "1: Merlin"},
                                }
                            }
                        ],
                    },
                ]
            )
            final = await adapter.generate(task, history, tools)
            self.assertIn("merlin", final.text.lower())
            self.assertGreater(final.input_tokens, 0)
        finally:
            await adapter.client.aio.aclose()

    async def test_e2b_scratch_checkpoint_restores_source(self):
        from merlin.sandbox import E2BSandboxClient

        sandbox = E2BSandboxClient(Settings())
        workspace = await sandbox.create(
            Task(spec=TaskSpec(prompt="Scratch checkpoint smoke test", repository="test/fixtures"))
        )
        try:
            first = await sandbox.execute(
                workspace,
                ToolCall(name="write_file", arguments={"path": "smoke.txt", "content": "Merlin"}),
            )
            self.assertTrue(first.ok)
            checkpoint = await sandbox.checkpoint(workspace, "live-smoke")
            await sandbox.execute(
                workspace,
                ToolCall(name="write_file", arguments={"path": "smoke.txt", "content": "changed"}),
            )
            await sandbox.restore(workspace, checkpoint)
            restored = await sandbox.execute(
                workspace, ToolCall(name="read_file", arguments={"path": "smoke.txt"})
            )
            self.assertIn("Merlin", restored.output)
            server = await sandbox.execute(
                workspace,
                ToolCall(
                    name="bash",
                    arguments={
                        "command": "python3 -m http.server 3000 --bind 127.0.0.1",
                        "background": True,
                    },
                ),
            )
            self.assertTrue(server.ok)
            page = await sandbox.execute(
                workspace,
                ToolCall(name="browser_open", arguments={"url": "http://127.0.0.1:3000/smoke.txt"}),
            )
            self.assertTrue(page.ok)
            self.assertIn("Merlin", page.output)
            screenshot = await sandbox.execute(workspace, ToolCall(name="browser_screenshot"))
            self.assertTrue(screenshot.ok)
            self.assertTrue(screenshot.artifacts)
        finally:
            await sandbox.close(workspace)

    async def test_github_reads_repository_and_private_issue_permissions(self):
        from merlin.integrations.github import GitHubService

        repository = os.environ["MERLIN_LIVE_REPOSITORY"]
        service = GitHubService(Settings())
        task = Task(spec=TaskSpec(prompt="Read-only smoke", repository=repository))
        try:
            for resource in ("repository", "issues", "pulls"):
                result = await service.execute(
                    task, ToolCall(name="github_read", arguments={"resource": resource})
                )
                self.assertTrue(result.ok, f"GitHub {resource} read did not succeed")
        finally:
            await service.aclose()
