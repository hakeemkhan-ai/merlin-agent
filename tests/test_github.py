from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from merlin.config import Settings
from merlin.contracts import Checkpoint, Task, TaskSpec, ToolCall
from merlin.integrations.github import GitHubService

BASE_SHA = "a" * 40
NEW_SHA = "b" * 40


class FakeGitHubService(GitHubService):
    def _app_token(self) -> str:
        return "test-app-jwt"


class GitHubTests(unittest.IsolatedAsyncioTestCase):
    async def test_approval_baseline_is_read_at_exact_commit_with_read_only_token(self) -> None:
        async def baseline_handler(request):
            if "/contents/" in request.url.path:
                self.requests.append(request)
                self.assertEqual(request.url.params["ref"], BASE_SHA)
                return httpx.Response(
                    200,
                    json={
                        "encoding": "base64",
                        "size": 4,
                        "content": base64.b64encode(b"old\n").decode(),
                    },
                )
            return await self.handler(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(baseline_handler)) as http:
            service = FakeGitHubService(self.settings, http)
            value = await service.read_file_at_commit("owner/repo", "docs/guide.md", BASE_SHA)
            self.assertEqual(value, b"old\n")
            token_request = next(request for request in self.requests if request.method == "POST")
            self.assertEqual(
                json.loads(token_request.content),
                {
                    "permissions": {"contents": "read"},
                    "repositories": ["repo"],
                },
            )
            count = len(self.requests)
            for path in ("../secret", ".git/config", ".env"):
                with self.assertRaises(ValueError):
                    await service.read_file_at_commit("owner/repo", path, BASE_SHA)
            self.assertEqual(len(self.requests), count)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.settings = Settings(
            artifact_dir=Path(self.temporary.name) / "artifacts",
            github_app_id="42",
            github_installation_id=7,
            github_private_key_file=Path(self.temporary.name) / "key.pem",
            owner_token="owner-token",
        )
        self.requests: list[httpx.Request] = []
        self.issues: list[dict[str, Any]] = []
        self.posts = 0
        self.visible_issue_response_lost = False
        self.invisible_issue_response_lost = False

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def task(self, permissions: list[str] | None = None, parent_id: str | None = None) -> Task:
        return Task(
            id="task123",
            spec=TaskSpec(
                prompt="do work",
                repository="owner/repo",
                permissions=permissions or [],
                parent_id=parent_id,
            ),
        )

    def frozen_snapshot(self, files: dict[str, bytes]) -> tuple[Checkpoint, Any]:
        from merlin.sandbox._remote import MANIFEST

        buffer = io.BytesIO()
        manifest = {
            "version": 1,
            "files": {name: hashlib.sha256(value).hexdigest() for name, value in files.items()},
        }
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for name, content in {**files, MANIFEST: json.dumps(manifest).encode()}.items():
                entry = tarfile.TarInfo(name)
                entry.mode, entry.size = 0o644, len(content)
                archive.addfile(entry, io.BytesIO(content))
        data = buffer.getvalue()
        self.settings.artifact_dir.mkdir(exist_ok=True)
        (self.settings.artifact_dir / "frozen.tar").write_bytes(data)
        checkpoint = Checkpoint(
            id="frozen",
            task_id="task123",
            artifact="frozen.tar",
            sha256=hashlib.sha256(data).hexdigest(),
            base_sha=BASE_SHA,
        )
        return checkpoint, SimpleNamespace(get_checkpoint=lambda identifier: checkpoint)

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/repos/owner/repo/installation":
            return httpx.Response(200, json={"id": 7})
        if path == "/app/installations/7/access_tokens":
            return httpx.Response(201, json={"token": "installation-token"})
        if path == "/repos/owner/repo":
            return httpx.Response(200, json={"default_branch": "main", "full_name": "owner/repo"})
        if path == "/repos/owner/repo/issues":
            if request.method == "GET":
                return httpx.Response(200, json=self.issues)
            self.posts += 1
            payload = json.loads(request.content)
            issue = {**payload, "number": 3, "html_url": "https://github.com/owner/repo/issues/3"}
            if self.invisible_issue_response_lost:
                raise httpx.ReadTimeout("lost response", request=request)
            self.issues.append(issue)
            if self.visible_issue_response_lost:
                self.visible_issue_response_lost = False
                raise httpx.ReadTimeout("lost response", request=request)
            return httpx.Response(201, json=issue)
        return httpx.Response(404, json={"message": "not found"})

    async def test_clone_token_is_read_only_and_single_repository(self) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.handler)) as http:
            token = await FakeGitHubService(self.settings, http).read_token("owner/repo")
        self.assertEqual(token, "installation-token")
        payload = json.loads(self.requests[-1].content)
        self.assertEqual(payload, {"permissions": {"contents": "read"}, "repositories": ["repo"]})

    async def test_unapproved_and_child_writes_do_not_mint_tokens(self) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.handler)) as http:
            service = FakeGitHubService(self.settings, http)
            call = ToolCall(name="github_create_issue", arguments={"title": "Title"})
            result = await service.execute(self.task(), call)
            self.assertFalse(result.ok)
            result = await service.execute(self.task(["create_issue"], "parent"), call)
            self.assertFalse(result.ok)
        self.assertEqual(self.requests, [])

    async def test_issue_replay_uses_durable_journal(self) -> None:
        call = ToolCall(name="github_create_issue", arguments={"title": "Title", "body": "Body"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.handler)) as http:
            first = await FakeGitHubService(self.settings, http).execute(
                self.task(["create_issue"]), call
            )
            second = await FakeGitHubService(self.settings, http).execute(
                self.task(["create_issue"]), call
            )
        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertEqual(self.posts, 1)
        self.assertNotIn("installation-token", first.model_dump_json())
        self.assertIn("merlin-operation:", self.issues[0]["body"])

    async def test_lost_issue_response_reconciles_without_duplicate(self) -> None:
        self.visible_issue_response_lost = True
        call = ToolCall(name="github_create_issue", arguments={"title": "Title"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.handler)) as http:
            result = await FakeGitHubService(self.settings, http).execute(
                self.task(["create_issue"]), call
            )
            self.assertTrue(result.uncertain)
            result = await FakeGitHubService(self.settings, http).execute(
                self.task(["create_issue"]), call
            )
        self.assertTrue(result.ok)
        self.assertTrue(result.data["reconciled"])
        self.assertEqual(self.posts, 1)

    async def test_unresolved_issue_is_never_reposted(self) -> None:
        self.invisible_issue_response_lost = True
        call = ToolCall(name="github_create_issue", arguments={"title": "Title"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.handler)) as http:
            result = await FakeGitHubService(self.settings, http).execute(
                self.task(["create_issue"]), call
            )
            self.assertTrue(result.uncertain)
            result = await FakeGitHubService(self.settings, http).execute(
                self.task(["create_issue"]), call
            )
        self.assertTrue(result.uncertain)
        self.assertEqual(self.posts, 1)

    async def test_publishing_rejects_paths_modes_and_argument_expansion(self) -> None:
        paths = [
            "../escape",
            "/etc/passwd",
            "src/../file",
            ".git/config",
            ".github/workflows/ci.yml",
            ".github",
            ".github/workflows",
            "src\\file",
            "src//file",
        ]
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.handler)) as http:
            service = FakeGitHubService(self.settings, http)
            for path in paths:
                call = ToolCall(
                    name="github_publish_branch",
                    arguments={
                        "base_sha": BASE_SHA,
                        "message": "Commit",
                        "files": [{"path": path, "content": "x"}],
                    },
                )
                self.assertFalse(
                    (await service.execute(self.task(["publish_branch"]), call)).ok, path
                )
            call = ToolCall(
                name="github_publish_branch",
                arguments={
                    "base_sha": BASE_SHA,
                    "message": "Commit",
                    "branch": "main",
                    "files": [{"path": "safe", "content": "x", "mode": "120000"}],
                },
            )
            self.assertFalse((await service.execute(self.task(["publish_branch"]), call)).ok)
        self.assertEqual(self.requests, [])

    async def test_fixed_task_branch_and_non_force_update(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if "/git/" not in path:
                return await self.handler(request)
            self.requests.append(request)
            if request.method == "GET" and "/git/ref/" in path:
                return httpx.Response(200, json={"object": {"sha": BASE_SHA}})
            if request.method == "GET" and path.endswith(BASE_SHA):
                return httpx.Response(
                    200, json={"message": "Old commit", "tree": {"sha": "c" * 40}}
                )
            if path.endswith("/git/blobs"):
                payload = json.loads(request.content)
                self.assertEqual(payload["encoding"], "base64")
                self.assertEqual(base64.b64decode(payload["content"]), b"\x00\xff")
                return httpx.Response(201, json={"sha": "d" * 40})
            if path.endswith("/git/trees"):
                return httpx.Response(201, json={"sha": "e" * 40})
            if path.endswith("/git/commits"):
                self.assertIn("Merlin-Operation:", json.loads(request.content)["message"])
                return httpx.Response(201, json={"sha": NEW_SHA})
            if request.method == "PATCH":
                self.assertEqual(path, "/repos/owner/repo/git/refs/heads/merlin/task-task123")
                self.assertEqual(json.loads(request.content), {"sha": NEW_SHA, "force": False})
                return httpx.Response(200, json={"object": {"sha": NEW_SHA}})
            return httpx.Response(404)

        call = ToolCall(
            name="github_publish_branch",
            arguments={
                "base_sha": BASE_SHA,
                "message": "Add binary",
                "files": [
                    {
                        "path": "fixture.bin",
                        "content": base64.b64encode(b"\x00\xff").decode(),
                        "encoding": "base64",
                    }
                ],
            },
        )
        checkpoint, store = self.frozen_snapshot({"fixture.bin": b"\x00\xff"})
        call.arguments.update(checkpoint_id=checkpoint.id, checkpoint_sha256=checkpoint.sha256)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            result = await FakeGitHubService(self.settings, http, store=store).execute(
                self.task(["publish_branch"]), call
            )
        self.assertTrue(result.ok, result.output)
        self.assertEqual(result.data["branch"], "merlin/task-task123")

    async def test_read_has_fixed_paths_and_no_credential_disclosure(self) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.handler)) as http:
            service = FakeGitHubService(self.settings, http)
            result = await service.execute(self.task(), ToolCall(name="github_read"))
            self.assertTrue(result.ok)
            self.assertEqual(result.data["resource"]["full_name"], "owner/repo")
            self.assertNotIn("installation-token", result.model_dump_json())
            result = await service.execute(
                self.task(), ToolCall(name="github_read", arguments={"url": "https://evil.test"})
            )
            self.assertFalse(result.ok)

    async def test_repository_outside_installation_cannot_get_token(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, json={"id": 999})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with self.assertRaises(PermissionError):
                await FakeGitHubService(self.settings, http).read_token("other/repo")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].method, "GET")

    async def test_oauth_signed_one_use_state_and_installation_check(self) -> None:
        settings = self.settings.model_copy(
            update={
                "github_client_id": "client-id",
                "github_client_secret": self.settings.owner_token,
            }
        )

        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.url.path == "/login/oauth/access_token":
                return httpx.Response(200, json={"access_token": "user-secret"})
            if request.url.path == "/user":
                return httpx.Response(200, json={"id": 1, "login": "owner"})
            if request.url.path == "/user/installations":
                return httpx.Response(200, json={"installations": [{"id": 7, "app_id": 42}]})
            if request.url.path == "/user/installations/7/repositories":
                return httpx.Response(200, json={"repositories": [{"full_name": "owner/repo"}]})
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            first = FakeGitHubService(settings, http)
            link = first.connection_start()["authorization_url"]
            state = parse_qs(urlparse(link).query)["state"][0]
            self.assertFalse(first.connection_status()["linked"])
            second = FakeGitHubService(settings, http)
            result = await second.connection_callback("code", state)
            self.assertTrue(result["linked"])
            self.assertEqual(result["user_login"], "owner")
            with self.assertRaises(ValueError):
                await second.connection_callback("code", state)
            with self.assertRaises(ValueError):
                await second.connection_callback("code", state + "forged")
            self.assertEqual(len(self.requests), 4)
        state_content = "".join(
            path.read_text() for path in Path(self.temporary.name).rglob("*.json")
        )
        self.assertNotIn("user-secret", state_content)

    async def test_oauth_rejects_an_unrelated_installation(self) -> None:
        settings = self.settings.model_copy(
            update={
                "github_client_id": "client-id",
                "github_client_secret": self.settings.owner_token,
            }
        )

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/login/oauth/access_token":
                return httpx.Response(200, json={"access_token": "user-secret"})
            if request.url.path == "/user":
                return httpx.Response(200, json={"id": 1, "login": "outsider"})
            return httpx.Response(200, json={"installations": [{"id": 999, "app_id": 42}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            service = FakeGitHubService(settings, http)
            state = parse_qs(urlparse(service.connection_start()["authorization_url"]).query)[
                "state"
            ][0]
            with self.assertRaises(PermissionError):
                await service.connection_callback("code", state)
            self.assertFalse(service.connection_status()["linked"])

    async def test_pr_uses_task_branch_and_reconciles_marker(self) -> None:
        prs: list[dict[str, Any]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path != "/repos/owner/repo/pulls":
                return await self.handler(request)
            self.requests.append(request)
            if request.method == "GET":
                self.assertEqual(request.url.params["head"], "owner:merlin/task-task123")
                return httpx.Response(200, json=prs)
            payload = json.loads(request.content)
            self.assertEqual(payload["head"], "merlin/task-task123")
            self.assertEqual(payload["base"], "main")
            prs.append({**payload, "number": 4, "html_url": "https://github.com/owner/repo/pull/4"})
            raise httpx.ReadTimeout("lost response", request=request)

        call = ToolCall(name="github_create_pr", arguments={"title": "Fix", "body": "Tests passed"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            result = await FakeGitHubService(self.settings, http).execute(
                self.task(["create_pr"]), call
            )
            self.assertTrue(result.uncertain)
            result = await FakeGitHubService(self.settings, http).execute(
                self.task(["create_pr"]), call
            )
        self.assertTrue(result.ok)
        self.assertEqual(len(prs), 1)

    async def test_published_branch_reconciliation_does_not_create_more_objects(self) -> None:
        call = ToolCall(
            name="github_publish_branch",
            arguments={
                "base_sha": BASE_SHA,
                "message": "Add file",
                "files": [{"path": "safe", "content": "x"}],
            },
        )
        task = self.task(["publish_branch"])
        checkpoint, store = self.frozen_snapshot({"safe": b"x"})
        call.arguments.update(checkpoint_id=checkpoint.id, checkpoint_sha256=checkpoint.sha256)
        digest = FakeGitHubService._digest(task, call)

        async def handler(request: httpx.Request) -> httpx.Response:
            if "/git/" not in request.url.path:
                return await self.handler(request)
            self.requests.append(request)
            self.assertEqual(request.method, "GET")
            if "/git/ref/" in request.url.path:
                return httpx.Response(200, json={"object": {"sha": NEW_SHA}})
            return httpx.Response(200, json={"message": f"Add file\nMerlin-Operation: {digest}"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            result = await FakeGitHubService(self.settings, http, store=store).execute(task, call)
        self.assertTrue(result.ok)
        self.assertTrue(result.data["reconciled"])
        self.assertFalse(any(request.method == "PATCH" for request in self.requests))

    async def test_oauth_link_restricts_app_access_to_user_repository_subset(self) -> None:
        settings = self.settings.model_copy(update={"github_client_id": "client-id"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.handler)) as http:
            service = FakeGitHubService(settings, http)
            service._directory("oauth")
            service._write_json(
                service._state_dir / "connection.json",
                {
                    "installation_id": 7,
                    "user_login": "owner",
                    "repositories": ["owner/repo"],
                },
            )
            with self.assertRaises(PermissionError):
                await service.read_token("owner/another-repo")
            self.assertEqual(self.requests, [])

    async def test_large_read_response_is_bounded_in_history_payload(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/repos/owner/repo":
                return httpx.Response(200, json={"description": "x" * 100000})
            return await self.handler(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            result = await FakeGitHubService(self.settings, http).execute(
                self.task(), ToolCall(name="github_read")
            )
        self.assertTrue(result.ok)
        self.assertLessEqual(len(result.output), 64000)
        self.assertEqual(result.data, {"truncated": True})
