"""API boundary tests using real persistence and no external services."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

import httpx
from pydantic import SecretStr

from merlin.api import create_app
from merlin.config import Settings
from merlin.contracts import Checkpoint, TaskState, ToolCall, ToolResult
from merlin.persistence import SQLTaskStore

TOKEN = "owner-secret-" * 4
AUTH = {"Authorization": f"Bearer {TOKEN}"}


async def direct_io(function, *args):  # type: ignore[no-untyped-def]
    """Inject synchronous I/O for deterministic ASGI contract tests.

    Production uses asyncio.to_thread; these tests focus HTTP behavior while
    persistence concurrency is verified independently using actual threads.
    """
    return function(*args)


class ASGIClient:
    def __init__(self, app):  # type: ignore[no-untyped-def]
        self.app = app

    def request(self, method, path, **kwargs):  # type: ignore[no-untyped-def]
        async def execute():  # type: ignore[no-untyped-def]
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url="http://test",
            ) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(execute())

    def get(self, path, **kwargs):  # type: ignore[no-untyped-def]
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):  # type: ignore[no-untyped-def]
        return self.request("POST", path, **kwargs)

    def close(self) -> None:
        pass


class FakeGitHub:
    def connection_status(self):  # type: ignore[no-untyped-def]
        return {"configured": True, "linked": True, "installation_id": 1, "user_login": "owner"}

    def connection_start(self):  # type: ignore[no-untyped-def]
        return {"authorization_url": "https://github.com/login/oauth/authorize?state=signed"}

    async def connection_callback(self, code, state):  # type: ignore[no-untyped-def]
        if state != "signed":
            raise ValueError("Invalid OAuth state")
        return {"linked": True}

    async def repositories(self):  # type: ignore[no-untyped-def]
        return [{"full_name": "owner/repo"}]


class APITests(TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.store = SQLTaskStore("sqlite:///:memory:")
        self.store.create_schema()
        self.settings = Settings(
            owner_token=SecretStr(TOKEN),
            artifact_dir=Path(self.directory.name) / "artifacts",
        )
        self.settings.artifact_dir.mkdir()
        self.client = ASGIClient(
            create_app(
                self.settings,
                self.store,
                github=FakeGitHub(),
                run_sync=direct_io,
            )
        )

    def tearDown(self) -> None:
        self.client.close()
        self.store.close()
        self.directory.cleanup()

    def submit(self, **changes: object):  # type: ignore[no-untyped-def]
        return self.client.post(
            "/v1/tasks",
            json={"prompt": "Fix", "repository": "owner/repo", **changes},
            headers=AUTH,
        )

    def test_health_is_public_and_every_task_route_requires_bearer_auth(self) -> None:
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertEqual(self.client.get("/v1/tasks").status_code, 401)
        self.assertEqual(self.client.get(f"/v1/tasks?token={TOKEN}").status_code, 401)
        self.assertEqual(
            self.client.get("/v1/tasks", headers={"Authorization": "Bearer wrong"}).status_code,
            401,
        )
        self.assertEqual(self.client.get("/v1/tasks", headers=AUTH).status_code, 200)

    def test_conversation_without_repository_round_trips_through_views_and_followups(self) -> None:
        response = self.client.post("/v1/tasks", json={"prompt": "Good Morning !"}, headers=AUTH)
        self.assertEqual(response.status_code, 201, response.text)
        task = response.json()
        self.assertIsNone(task["spec"]["repository"])
        summary = self.client.get(f"/v1/tasks/{task['id']}/summary", headers=AUTH).json()
        self.assertIsNone(summary["repository"])
        self.assertIsNone(summary["checkpoint_id"])
        sessions = self.client.get("/v1/sessions", headers=AUTH).json()
        self.assertIsNone(sessions["items"][0]["repository"])
        followup = self.client.post(
            f"/v1/tasks/{task['id']}/messages", json={"prompt": "How are you?"}, headers=AUTH
        )
        self.assertEqual(followup.status_code, 201, followup.text)
        self.assertIsNone(followup.json()["spec"]["repository"])

    def test_chat_rejects_publishing_permissions_and_repository_switch(self) -> None:
        invalid = self.submit(repository=None, permissions=["github_create_issue"])
        self.assertEqual(invalid.status_code, 409, invalid.text)
        self.assertEqual(self.submit(repository=None, session_id="chat-only").status_code, 201)
        changed = self.submit(repository="owner/repo", session_id="chat-only")
        self.assertEqual(changed.status_code, 409, changed.text)

    def test_short_owner_secret_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            create_app(Settings(owner_token=SecretStr("short")), self.store)

    def test_task_creation_and_inspection(self) -> None:
        response = self.submit(permissions=["create_pr"], allowed_domains=["Docs.Example.com."])
        self.assertEqual(response.status_code, 201, response.text)
        task = response.json()
        self.assertEqual(task["spec"]["allowed_domains"], ["docs.example.com"])
        inspected = self.client.get(f"/v1/tasks/{task['id']}", headers=AUTH)
        self.assertEqual(inspected.json()["id"], task["id"])
        self.assertEqual(len(self.client.get("/v1/tasks", headers=AUTH).json()), 1)

    def test_idempotency_without_explicit_session_is_stable(self) -> None:
        headers = {**AUTH, "Idempotency-Key": "original-request"}
        body = {"prompt": "Fix", "repository": "owner/repo"}
        first = self.client.post("/v1/tasks", json=body, headers=headers)
        second = self.client.post("/v1/tasks", json=body, headers=headers)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(first.json()["id"], second.json()["id"])
        conflicting = self.client.post(
            "/v1/tasks", json={**body, "prompt": "Different"}, headers=headers
        )
        self.assertEqual(conflicting.status_code, 409)

    def test_external_child_and_checkpoint_submission_are_rejected(self) -> None:
        self.assertEqual(self.submit(parent_id="parent").status_code, 409)
        self.assertEqual(self.submit(base_checkpoint_id="checkpoint").status_code, 409)

    def test_unknown_permissions_and_private_network_values_are_rejected(self) -> None:
        self.assertEqual(self.submit(permissions=["merge_pr"]).status_code, 409)
        for domain in (
            "127.0.0.1",
            "169.254.169.254",
            "localhost",
            "app.internal",
            "*.example.com",
            "https://example.com",
            "example..com",
        ):
            with self.subTest(domain=domain):
                self.assertEqual(self.submit(allowed_domains=[domain]).status_code, 409)

    def test_session_cannot_switch_repository(self) -> None:
        self.submit(session_id="session")
        self.assertEqual(
            self.submit(session_id="session", repository="different/repo").status_code, 409
        )

    def test_followup_preserves_session_but_requires_new_publishing_grant(self) -> None:
        original = self.submit(permissions=["create_pr"]).json()
        response = self.client.post(
            f"/v1/tasks/{original['id']}/messages",
            json={"prompt": "Follow-up"},
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 201, response.text)
        followup = response.json()
        self.assertEqual(followup["spec"]["session_id"], original["spec"]["session_id"])
        self.assertEqual(followup["spec"]["permissions"], [])
        self.assertIsNone(followup["spec"]["base_checkpoint_id"])

    def test_cancel_and_resume_routes(self) -> None:
        task = self.submit().json()
        lease = self.store.claim("worker")
        assert lease is not None
        self.store.update_task(lease, state=TaskState.PAUSED)
        response = self.client.post(f"/v1/tasks/{task['id']}/resume", headers=AUTH)
        self.assertEqual(response.json()["state"], "queued")
        response = self.client.post(f"/v1/tasks/{task['id']}/cancel", headers=AUTH)
        self.assertEqual(response.json()["state"], "cancelled")
        self.assertEqual(
            self.client.post(f"/v1/tasks/{task['id']}/resume", headers=AUTH).status_code, 409
        )

    def test_approval_routes(self) -> None:
        task = self.submit().json()
        lease = self.store.claim("worker")
        assert lease is not None
        approval = self.store.request_approval(lease, ToolCall(name="github_create_issue"))
        self.store.update_task(lease, state=TaskState.WAITING_FOR_APPROVAL)
        response = self.client.get(f"/v1/tasks/{task['id']}/approvals", headers=AUTH)
        self.assertEqual(response.json()[0]["id"], approval.id)
        decided = self.client.post(
            f"/v1/approvals/{approval.id}", json={"approved": True}, headers=AUTH
        )
        self.assertEqual(decided.json()["status"], "approved")
        self.assertEqual(self.store.get_task(task["id"]).state, TaskState.QUEUED)

    def test_review_feedback_requires_owner_auth_and_requeues_with_no_publishing_permission(self):
        task = self.submit().json()
        lease = self.store.claim("worker")
        call = ToolCall(name="github_create_pr", arguments={"title": "Docs"})
        self.store.update_task(
            lease,
            history=[{"role": "model", "parts": [], "_merlin_calls": [call.model_dump()]}],
        )
        approval = self.store.request_approval(lease, call)
        self.store.update_task(lease, state=TaskState.WAITING_FOR_APPROVAL)
        route = f"/v1/approvals/{approval.id}/changes"
        self.assertEqual(self.client.post(route, json={"note": "Revise"}).status_code, 401)
        self.assertEqual(self.client.post(route, json={"note": ""}, headers=AUTH).status_code, 422)
        response = self.client.post(route, json={"note": "Disclose failed tests"}, headers=AUTH)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "rejected")
        saved = self.store.get_task(task["id"])
        self.assertEqual(saved.state, TaskState.QUEUED)
        self.assertEqual(saved.spec.permissions, [])
        self.assertEqual(
            self.client.post(route, json={"note": "Again"}, headers=AUTH).status_code, 409
        )
        self.assertEqual(
            self.client.post(
                "/v1/approvals/missing/changes", json={"note": "Revise"}, headers=AUTH
            ).status_code,
            404,
        )

    def test_events_replay_only_after_last_event_id(self) -> None:
        task = self.submit().json()
        lease = self.store.claim("worker")
        assert lease is not None
        self.store.update_task(lease, state=TaskState.SUCCEEDED, result="Done")
        response = self.client.get(
            f"/v1/tasks/{task['id']}/events",
            headers={**AUTH, "Last-Event-ID": "1"},
        )
        self.assertEqual(response.status_code, 200)
        events = [
            json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
        ]
        self.assertTrue(events)
        self.assertTrue(all(event["sequence"] > 1 for event in events))
        self.assertIn("text/event-stream", response.headers["content-type"])

    def test_invalid_event_cursor_rejected_before_stream(self) -> None:
        task = self.submit().json()
        response = self.client.get(
            f"/v1/tasks/{task['id']}/events",
            headers={**AUTH, "Last-Event-ID": "bad"},
        )
        self.assertEqual(response.status_code, 400)

    def test_artifacts_must_be_recorded_and_inside_artifact_directory(self) -> None:
        task = self.submit().json()
        lease = self.store.claim("worker")
        assert lease is not None
        call = ToolCall(name="bash")
        self.store.begin_tool(lease, call)
        safe = self.settings.artifact_dir / "output.txt"
        safe.write_text("recorded output")
        self.store.finish_tool(lease, ToolResult(call_id=call.id, artifacts=["output.txt"]))
        response = self.client.get(f"/v1/tasks/{task['id']}/artifacts/output.txt", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "recorded output")
        (self.settings.artifact_dir / "unrecorded.txt").write_text("hidden")
        self.assertEqual(
            self.client.get(
                f"/v1/tasks/{task['id']}/artifacts/unrecorded.txt", headers=AUTH
            ).status_code,
            404,
        )
        outside = Path(self.directory.name) / "outside.txt"
        outside.write_text("secret")
        (self.settings.artifact_dir / "output.txt").unlink()
        (self.settings.artifact_dir / "output.txt").symlink_to(outside)
        self.assertEqual(
            self.client.get(
                f"/v1/tasks/{task['id']}/artifacts/output.txt", headers=AUTH
            ).status_code,
            404,
        )

    def test_checkpoint_artifact_can_be_downloaded(self) -> None:
        task = self.submit().json()
        lease = self.store.claim("worker")
        assert lease is not None
        (self.settings.artifact_dir / "checkpoint.tar").write_bytes(b"archive")
        self.store.save_checkpoint(
            lease,
            Checkpoint(
                task_id=task["id"],
                artifact="checkpoint.tar",
                sha256="a" * 64,
            ),
        )
        self.assertEqual(
            self.client.get(
                f"/v1/tasks/{task['id']}/artifacts/checkpoint.tar", headers=AUTH
            ).content,
            b"archive",
        )

    def test_github_connect_status_and_callback_state(self) -> None:
        self.assertEqual(self.client.get("/v1/github/status").status_code, 401)
        self.assertTrue(self.client.get("/v1/github/status", headers=AUTH).json()["linked"])
        self.assertIn(
            "authorization_url", self.client.post("/v1/github/connect", headers=AUTH).json()
        )
        self.assertEqual(
            self.client.get("/v1/github/callback?code=code&state=wrong").status_code, 400
        )
        self.assertEqual(
            self.client.get("/v1/github/callback?code=code&state=signed").status_code, 200
        )
        self.assertEqual(
            self.client.get("/v1/github/repositories", headers=AUTH).json()[0]["full_name"],
            "owner/repo",
        )

    def test_unknown_task_is_404(self) -> None:
        self.assertEqual(self.client.get("/v1/tasks/missing", headers=AUTH).status_code, 404)

    def test_owner_can_explicitly_resolve_uncertain_operation(self) -> None:
        task = self.submit().json()
        lease = self.store.claim("worker")
        assert lease is not None
        call = ToolCall(name="bash")
        self.store.begin_tool(lease, call)
        self.store.update_task(lease, state=TaskState.PAUSED)
        response = self.client.post(
            f"/v1/tasks/{task['id']}/tools/{call.id}/resolve",
            json={"note": "Inspected repository and external effects"},
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["state"], "queued")

    def test_metrics_are_public_and_have_no_prompt_labels(self) -> None:
        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertIn("merlin_task_transitions", response.text)
        self.assertNotIn("prompt=", response.text)
