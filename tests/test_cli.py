"""CLI behavior checks with an injected HTTP transport."""

import json
import os
import stat
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from merlin.cli import app, load_config, save_config, terminal_text


class CLITests(TestCase):
    def test_untrusted_text_cannot_emit_terminal_control_sequences(self):
        value = "Report\n\x1b]52;c;clipboard\x07\x9b2J\rDone\t✓"
        safe = terminal_text(value)
        self.assertNotIn("\x1b", safe)
        self.assertNotIn("\x07", safe)
        self.assertNotIn("\x9b", safe)
        self.assertNotIn("\r", safe)
        self.assertIn("\n", safe)
        self.assertIn("✓", safe)

    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "client.json"
        self.environment = patch.dict(
            os.environ,
            {
                "MERLIN_CLIENT_CONFIG": str(self.path),
                "MERLIN_OWNER_TOKEN": "test-owner-token-" * 3,
                "MERLIN_API_URL": "http://localhost:8000",
            },
        )
        self.environment.start()
        self.runner = CliRunner()

    def tearDown(self) -> None:
        self.environment.stop()
        self.directory.cleanup()

    def test_help_lists_operational_commands(self) -> None:
        result = self.runner.invoke(app, ["--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        for command in ("run", "chat", "attach", "approve", "cancel", "resume", "doctor"):
            self.assertIn(command, result.output)

    def test_ui_entrypoints_keep_noninteractive_output_plain(self) -> None:
        result = self.runner.invoke(app, [])
        self.assertEqual(result.exit_code, 2)
        self.assertIn("Usage:", result.output)
        result = self.runner.invoke(app, ["ui", "--demo"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("requires a terminal", result.output)
        with (
            patch("merlin.cli.interactive_terminal", return_value=True),
            patch("merlin.tui.launch") as launch,
        ):
            result = self.runner.invoke(app, ["ui", "--repo", "owner/repo"])
        self.assertEqual(result.exit_code, 0, result.output)
        launch.assert_called_once_with(repo="owner/repo", session=None, task=None, demo=False)

    def test_chat_opens_without_a_repository(self) -> None:
        with (
            patch("merlin.cli.interactive_terminal", return_value=True),
            patch("merlin.tui.launch") as launch,
        ):
            result = self.runner.invoke(app, ["chat"])
        self.assertEqual(result.exit_code, 0, result.output)
        launch.assert_called_once_with(repo=None, session=None, allow_domains=[])

    def test_legacy_chat_plain_does_not_launch_tui(self) -> None:
        with (
            patch("merlin.cli.interactive_terminal", return_value=True),
            patch("merlin.tui.launch") as launch,
            patch("prompt_toolkit.PromptSession") as prompt,
        ):
            prompt.return_value.prompt.return_value = "/quit"
            result = self.runner.invoke(app, ["chat", "--repo", "owner/repo", "--plain"])
        self.assertEqual(result.exit_code, 0, result.output)
        launch.assert_not_called()

    def test_attach_does_not_advance_cursor_past_incomplete_frame(self) -> None:
        cursors = []

        def handler(request):
            if request.url.path.endswith("/events"):
                cursors.append(request.headers["last-event-id"])
                if len(cursors) == 1:
                    return httpx.Response(200, text='id: 4\ndata: {"sequence": 4')
                record = {"sequence": 4, "kind": "model.reply", "data": {"text": "Recovered"}}
                return httpx.Response(200, text=f"id: 4\ndata: {json.dumps(record)}\n\n")
            return httpx.Response(
                200,
                json={
                    "id": "task-1",
                    "state": "running" if len(cursors) == 1 else "succeeded",
                    "result": None,
                },
            )

        with patch(
            "merlin.cli.api_client",
            side_effect=lambda: httpx.Client(
                base_url="http://test", transport=httpx.MockTransport(handler)
            ),
        ):
            result = self.runner.invoke(app, ["attach", "task-1", "--after", "3"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(cursors, ["3", "3"])
        self.assertEqual(result.output.count("Recovered"), 1)

    def test_login_stores_secret_with_restricted_permissions(self) -> None:
        result = self.runner.invoke(app, ["login", "--url", "https://merlin.example.com"])
        self.assertEqual(result.exit_code, 0, result.output)
        configuration = load_config()
        self.assertEqual(configuration["api_url"], "https://merlin.example.com")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertNotIn(configuration["owner_token"], result.output)

    def test_login_rejects_cleartext_remote_connection(self) -> None:
        result = self.runner.invoke(app, ["login", "--url", "http://merlin.example.com"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse(self.path.exists())

    def test_save_configuration_replaces_atomically(self) -> None:
        save_config("https://one.example.com", "one")
        save_config("https://two.example.com/", "two")
        self.assertEqual(
            load_config(), {"api_url": "https://two.example.com", "owner_token": "two"}
        )

    def test_submit_detached_task_is_json_and_does_not_grant_permissions_implicitly(self) -> None:
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            payload = json.loads(request.content)
            return httpx.Response(201, json={"id": "task-1", "spec": payload, "state": "queued"})

        with patch(
            "merlin.cli.api_client",
            side_effect=lambda: httpx.Client(
                base_url="http://test",
                transport=httpx.MockTransport(handler),
            ),
        ):
            result = self.runner.invoke(
                app, ["run", "Fix a bug", "--repo", "owner/repo", "--detach", "--json"]
            )
        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["id"], "task-1")
        self.assertEqual(payload["spec"]["permissions"], [])
        self.assertIn("idempotency-key", requests[0].headers)

    def test_attach_replays_events_and_prints_final_snapshot(self) -> None:
        cursors = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/events"):
                cursors.append(request.headers["last-event-id"])
                event = {"sequence": 4, "kind": "task.updated", "data": {"state": "succeeded"}}
                return httpx.Response(
                    200,
                    text=f"id: 4\ndata: {json.dumps(event)}\n\n",
                    headers={"Content-Type": "text/event-stream"},
                )
            return httpx.Response(
                200, json={"id": "task-1", "state": "succeeded", "result": "Done"}
            )

        with patch(
            "merlin.cli.api_client",
            side_effect=lambda: httpx.Client(
                base_url="http://test",
                transport=httpx.MockTransport(handler),
            ),
        ):
            result = self.runner.invoke(app, ["attach", "task-1", "--after", "3", "--json"])
        self.assertEqual(result.exit_code, 0, result.output)
        lines = [json.loads(line) for line in result.output.splitlines()]
        self.assertEqual(lines[0]["sequence"], 4)
        self.assertEqual(lines[1]["kind"], "task.snapshot")
        self.assertEqual(cursors, ["3"])

    def follow_events(self, events, *, result="The final answer", flags=()):
        def handler(request):
            if request.url.path.endswith("/events"):
                body = "".join(
                    f"id: {i}\ndata: {json.dumps(event | {'sequence': i})}\n\n"
                    for i, event in enumerate(events, 1)
                )
                return httpx.Response(200, text=body, headers={"Content-Type": "text/event-stream"})
            return httpx.Response(
                200, json={"id": "task-1", "state": "succeeded", "result": result}
            )

        with patch(
            "merlin.cli.api_client",
            side_effect=lambda: httpx.Client(
                base_url="http://test", transport=httpx.MockTransport(handler)
            ),
        ):
            return self.runner.invoke(app, ["attach", "task-1", *flags])

    def test_normal_output_shows_progress_and_final_answer_once_without_raw_tool_payloads(self):
        result = self.follow_events(
            [
                {"kind": "usage.reserved", "data": {"reservation": "internal-reservation"}},
                {
                    "kind": "tool.started",
                    "data": {"call": {"name": "read_file", "arguments": {"path": "README.md"}}},
                },
                {
                    "kind": "tool.finished",
                    "data": {"ok": True, "result": {"output": "large-raw-tool-payload"}},
                },
                {"kind": "model.delta", "data": {"text": "The final "}},
                {"kind": "model.delta", "data": {"text": "answer"}},
                {"kind": "model.reply", "data": {"text": "The final answer"}},
            ]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.output.count("The final answer"), 1)
        self.assertIn("Reading README.md", result.output)
        self.assertIn("Task succeeded", result.output)
        self.assertNotIn("large-raw-tool-payload", result.output)
        self.assertNotIn("internal-reservation", result.output)

    def test_answer_is_complete_when_attach_starts_without_streamed_deltas(self):
        for events in (
            [],
            [{"kind": "model.reply", "data": {"text": "The final answer"}}],
            [{"kind": "model.delta", "data": {"text": "The final "}}],
        ):
            with self.subTest(events=events):
                result = self.follow_events(events)
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(result.output.count("The final answer"), 1)

    def test_intermediate_reply_and_final_answer_have_separate_lines(self):
        result = self.follow_events(
            [
                {"kind": "model.delta", "data": {"text": "I will read the files."}},
                {"kind": "model.reply", "data": {"text": "I will read the files."}},
                {"kind": "tool.started", "data": {"call": {"name": "list_files"}}},
                {"kind": "model.delta", "data": {"text": "The final answer"}},
                {"kind": "model.reply", "data": {"text": "The final answer"}},
            ]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("I will read the files.\nListing repository files...\n", result.output)
        self.assertEqual(result.output.count("The final answer"), 1)

    def test_json_output_keeps_complete_events_and_final_snapshot(self):
        event = {
            "kind": "tool.finished",
            "data": {"ok": True, "result": {"output": "full raw payload", "data": {"extra": 1}}},
        }
        result = self.follow_events([event], flags=("--json",))
        self.assertEqual(result.exit_code, 0, result.output)
        lines = [json.loads(line) for line in result.output.splitlines()]
        self.assertEqual(lines[0], event | {"sequence": 1})
        self.assertEqual(lines[1]["kind"], "task.snapshot")

    def test_tool_failures_and_approval_review_remain_visible_and_sanitized(self):
        result = self.follow_events(
            [
                {
                    "kind": "tool.finished",
                    "data": {"ok": False, "result": {"output": "File unavailable\x1b\x07"}},
                },
                {"kind": "approval.requested", "data": {"approval_id": "review-1"}},
            ]
        )
        self.assertIn("Tool failed: File unavailable", result.output)
        self.assertIn("merlin inspect task-1", result.output)
        self.assertNotIn("\x1b", result.output)
        self.assertNotIn("\x07", result.output)

    def test_api_failures_exit_without_dumping_credentials(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"detail": "Owner authentication required"})

        with patch(
            "merlin.cli.api_client",
            side_effect=lambda: httpx.Client(
                base_url="http://test",
                transport=httpx.MockTransport(handler),
            ),
        ):
            result = self.runner.invoke(app, ["tasks", "--json"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("401", result.output)
        self.assertNotIn(os.environ["MERLIN_OWNER_TOKEN"], result.output)

    def test_approval_summary_omits_file_contents_while_json_preserves_them(self):
        value = {
            "id": "approval-1",
            "task_id": "task-1",
            "status": "approved",
            "call": {"arguments": {"files": [{"content": "large-reviewed-document"}]}},
        }
        with patch("merlin.cli.request", return_value=value):
            concise = self.runner.invoke(app, ["approve", "approval-1"])
            raw = self.runner.invoke(app, ["approve", "approval-1", "--json"])
        self.assertEqual(concise.exit_code, 0, concise.output)
        self.assertIn("Approval approved", concise.output)
        self.assertIn("task-1", concise.output)
        self.assertNotIn("large-reviewed-document", concise.output)
        self.assertEqual(json.loads(raw.output), value)

    def test_request_changes_sends_structured_feedback_and_prints_followup(self):
        note = "Disclose 'next: not found' and request a fresh approval."
        with patch("merlin.cli.request", return_value={"task_id": "task-1"}) as request:
            result = self.runner.invoke(app, ["request-changes", "approval-1", "--note", note])
        self.assertEqual(result.exit_code, 0, result.output)
        request.assert_called_once_with(
            "POST", "/v1/approvals/approval-1/changes", json={"note": note}
        )
        self.assertIn("merlin attach task-1", result.output)
