"""Frozen-source publishing and credential reuse without live GitHub operations."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from merlin.config import Settings
from merlin.contracts import Checkpoint, TaskSpec, ToolCall
from merlin.integrations.github import GitHubService, PublishBranch
from merlin.persistence import SQLTaskStore
from merlin.sandbox._remote import MANIFEST

BASE_SHA = "a" * 40


class AppService(GitHubService):
    def _app_token(self) -> str:
        return "fixture-app-jwt"


@pytest.fixture
def setup(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        artifact_dir=tmp_path / "artifacts",
        github_app_id="42",
        github_installation_id=7,
        github_private_key_file=tmp_path / "key.pem",
    )
    settings.artifact_dir.mkdir()
    store = SQLTaskStore("sqlite:///:memory:")
    store.create_schema()
    task = store.create_task(
        TaskSpec(prompt="edit", repository="owner/repo", permissions=["publish_branch"])
    )
    lease = store.claim("worker")
    assert lease is not None

    def save(files: dict[str, tuple[bytes, int]]) -> Checkpoint:
        buffer = io.BytesIO()
        manifest = {
            "version": 1,
            "files": {name: hashlib.sha256(value[0]).hexdigest() for name, value in files.items()},
        }
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for name, (data, mode) in {
                **files,
                MANIFEST: (json.dumps(manifest).encode(), 0o644),
            }.items():
                member = tarfile.TarInfo(name)
                member.mode, member.size = mode, len(data)
                archive.addfile(member, io.BytesIO(data))
        data = buffer.getvalue()
        checkpoint = Checkpoint(
            task_id=task.id,
            artifact=f"{hashlib.sha256(data).hexdigest()}.tar",
            sha256=hashlib.sha256(data).hexdigest(),
            base_sha=BASE_SHA,
        )
        (settings.artifact_dir / checkpoint.artifact).write_bytes(data)
        store.save_checkpoint(lease, checkpoint)
        return checkpoint

    yield settings, store, task, save
    store.close()


async def test_prepare_keeps_source_out_of_context_and_preserves_binary_mode(setup):
    settings, store, task, save = setup
    checkpoint = save(
        {"run.sh": (b"#!/bin/sh\nprintf frozen-bytes", 0o755), "image.bin": (b"\x00\xff", 0o644)}
    )
    requests = []

    async def forbidden(request):
        requests.append(request)
        raise AssertionError("Preparing present source must use only durable local checkpoints")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        service = AppService(settings, http, store=store)
        result = await service.execute(
            task,
            ToolCall(
                name="github_prepare_publish",
                arguments={"message": "Publish", "paths": ["run.sh", "image.bin"]},
            ),
        )
        assert result.ok, result.output
        assert "frozen-bytes" not in result.model_dump_json()
        assert result.data["publish_arguments"]["checkpoint_sha256"] == checkpoint.sha256
        args = PublishBranch.model_validate(result.data["publish_arguments"])
        frozen = service.frozen_publish(task.id, args)
        assert frozen.files[0].mode == "100755"
        assert base64.b64decode(frozen.files[1].content) == b"\x00\xff"
    assert requests == []


async def test_publish_rejects_fabricated_source_and_unbound_legacy_proposals_before_network(setup):
    settings, store, task, save = setup
    checkpoint = save({"app.py": (b"actual source\n", 0o644)})
    calls = []

    async def forbidden(request):
        calls.append(request)
        raise AssertionError("Invalid source must be rejected before minting credentials")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        service = AppService(settings, http, store=store)
        for extra in (
            {},
            {"checkpoint_id": checkpoint.id, "checkpoint_sha256": "b" * 64},
            {"checkpoint_id": checkpoint.id, "checkpoint_sha256": checkpoint.sha256},
        ):
            arguments = {
                "base_sha": BASE_SHA,
                "message": "Publish",
                "files": [{"path": "app.py", "content": "fabricated source"}],
                **extra,
            }
            result = await service.execute(
                task, ToolCall(name="github_publish_branch", arguments=arguments)
            )
            assert not result.ok and not result.uncertain
    assert calls == []


async def test_frozen_proposal_rejects_cross_task_and_tampered_artifact(setup):
    settings, store, task, save = setup
    checkpoint = save({"app.py": (b"actual source\n", 0o644)})
    args = PublishBranch(
        base_sha=BASE_SHA,
        message="Publish",
        checkpoint_id=checkpoint.id,
        checkpoint_sha256=checkpoint.sha256,
        paths=["app.py"],
    )
    service = AppService(settings, store=store)
    try:
        with pytest.raises(ValueError):
            service.frozen_publish("another-task", args)
        path = settings.artifact_dir / checkpoint.artifact
        path.write_bytes(path.read_bytes().replace(b"actual source", b"changed bytes"))
        with pytest.raises(ValueError, match="checksum"):
            service.frozen_publish(task.id, args)
    finally:
        await service.aclose()


async def test_prepared_snapshot_survives_later_checkpoint_without_changing_approved_bytes(setup):
    settings, store, task, save = setup
    first = save({"app.py": (b"first version\n", 0o644)})
    args = PublishBranch(
        base_sha=BASE_SHA,
        message="Publish",
        checkpoint_id=first.id,
        checkpoint_sha256=first.sha256,
        paths=["app.py"],
    )
    save({"app.py": (b"later version\n", 0o644)})
    async with httpx.AsyncClient() as http:
        frozen = AppService(settings, http, store=store).frozen_publish(task.id, args)
    assert base64.b64decode(frozen.files[0].content) == b"first version\n"


async def test_preparation_rejects_checkpoint_metadata_and_disallowed_paths(setup):
    settings, store, task, save = setup
    save({"app.py": (b"source\n", 0o644)})
    async with httpx.AsyncClient() as http:
        service = AppService(settings, http, store=store)
        for path in (MANIFEST, ".env", ".github/workflows/build.yml", "../app.py"):
            result = await service.execute(
                task,
                ToolCall(
                    name="github_prepare_publish", arguments={"message": "Publish", "paths": [path]}
                ),
            )
            assert not result.ok and not result.uncertain


async def test_nonexistent_deletion_is_rejected_with_read_only_credentials(setup):
    settings, store, task, save = setup
    save({"app.py": (b"source\n", 0o644)})
    requests = []

    async def handler(request):
        requests.append(request)
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 7})
        if request.url.path.endswith("/access_tokens"):
            assert json.loads(request.content)["permissions"] == {"contents": "read"}
            return httpx.Response(201, json={"token": "fixture-read-token"})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await AppService(settings, http, store=store).execute(
            task,
            ToolCall(
                name="github_prepare_publish",
                arguments={"message": "Delete", "paths": ["missing.py"]},
            ),
        )
    assert not result.ok and not result.uncertain
    assert all(
        request.method == "GET" or request.url.path.endswith("/access_tokens")
        for request in requests
    )


async def test_scoped_read_token_cache_coalesces_expiry_and_clears_on_close(setup):
    settings, _, _, _ = setup
    requests = []
    minted = 0

    async def handler(request):
        nonlocal minted
        requests.append(request)
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 7})
        minted += 1
        return httpx.Response(
            201,
            json={
                "token": f"fixture-read-{minted}",
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = AppService(settings, http)
        assert (
            len(set(await asyncio.gather(*(service.read_token("owner/repo") for _ in range(8)))))
            == 1
        )
        assert minted == 1 and len(requests) == 2
        await service.read_token("owner/another")
        assert minted == 2
        for key, (_, token) in list(service._read_tokens.items()):
            service._read_tokens[key] = (0, token)
        assert await service.read_token("owner/repo") == "fixture-read-3"
        await service.aclose()
        assert service._read_tokens == {}


async def test_token_cache_rechecks_changed_oauth_repository_grants(setup):
    settings, _, _, _ = setup
    settings = settings.model_copy(update={"github_client_id": "fixture-client"})
    minted = 0

    async def handler(request):
        nonlocal minted
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 7})
        minted += 1
        return httpx.Response(
            201,
            json={
                "token": f"fixture-read-{minted}",
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = AppService(settings, http)
        service._state_dir.mkdir(parents=True, exist_ok=True)
        link = service._state_dir / "connection.json"
        link.write_text(
            json.dumps({"installation_id": 7, "repositories": ["owner/repo"], "verified_at": 1})
        )
        await service.read_token("owner/repo")
        link.write_text(
            json.dumps({"installation_id": 7, "repositories": ["owner/repo"], "verified_at": 2})
        )
        await service.read_token("owner/repo")
        assert minted == 2
        link.write_text(json.dumps({"installation_id": 7, "repositories": []}))
        with pytest.raises(PermissionError):
            await service.read_token("owner/repo")
        assert minted == 2


@pytest.mark.parametrize(
    "method,path,retries",
    [
        ("GET", "/repos/owner/repo", True),
        ("POST", "/app/installations/7/access_tokens", True),
        ("POST", "/repos/owner/repo/issues", False),
    ],
)
async def test_rate_limit_retry_only_safe_reads_and_explicit_token_rejections(
    setup, method, path, retries
):
    settings, _, _, _ = setup
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return (
            httpx.Response(429, headers={"Retry-After": "0.01"}, json={})
            if calls == 1
            else httpx.Response(200, json={"ok": True})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = AppService(settings, http)
        with patch("merlin.integrations.github.asyncio.sleep", new_callable=AsyncMock) as sleep:
            if retries:
                assert await service._request(method, path, "fixture-token") == {"ok": True}
                sleep.assert_awaited_once_with(0.01)
            else:
                with pytest.raises(httpx.HTTPStatusError):
                    await service._request(method, path, "fixture-token")
                sleep.assert_not_awaited()
    assert calls == (2 if retries else 1)


async def test_long_retry_after_and_transport_loss_do_not_replay(setup):
    settings, _, _, _ = setup
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "300"}, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = AppService(settings, http)
        with patch("merlin.integrations.github.asyncio.sleep", new_callable=AsyncMock) as sleep:
            with pytest.raises(httpx.HTTPStatusError):
                await service._request("GET", "/repos/owner/repo", "fixture-token")
            sleep.assert_not_awaited()
    assert calls == 1

    async def lost(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("response lost", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(lost)) as http:
        with pytest.raises(httpx.ReadTimeout):
            await AppService(settings, http)._request(
                "POST", "/repos/owner/repo/issues", "fixture-token"
            )
    assert calls == 2


async def test_post_dispatch_journal_failure_remains_uncertain_and_reconciles_without_duplicate(
    setup,
):
    settings, _, task, _ = setup
    task = task.model_copy(deep=True)
    task.spec.permissions.append("create_issue")
    visible = []
    posts = 0

    async def handler(request):
        nonlocal posts
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 7})
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "fixture-write-token"})
        if request.method == "GET":
            return httpx.Response(200, json=visible)
        posts += 1
        visible.append(
            {
                **json.loads(request.content),
                "number": 1,
                "html_url": "https://github.com/owner/repo/issues/1",
            }
        )
        return httpx.Response(201, json=visible[-1])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = AppService(settings, http)
        original = service._write_json

        def disk_failure(path, value):
            if value.get("status") == "complete":
                raise OSError("journal flush failed")
            original(path, value)

        call = ToolCall(name="github_create_issue", arguments={"title": "Title"})
        with patch.object(service, "_write_json", side_effect=disk_failure):
            with pytest.raises(OSError):
                await service.execute(task, call)
        # The worker treats exceptions after dispatch as uncertain. Recovery
        # inspects the durable marker and never emits another visible POST.
        replay = await AppService(settings, http).execute(task, call)
    assert replay.ok and replay.data["reconciled"]
    assert posts == 1


async def test_unreadable_successful_write_response_is_uncertain_and_not_reposted(setup):
    settings, _, task, _ = setup
    task = task.model_copy(deep=True)
    task.spec.permissions.append("create_issue")
    visible = []

    async def handler(request):
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 7})
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "fixture-token"})
        if request.method == "GET":
            return httpx.Response(200, json=visible)
        visible.append(
            {
                **json.loads(request.content),
                "number": 1,
                "html_url": "https://github.com/owner/repo/issues/1",
            }
        )
        return httpx.Response(201, content=b"invalid-json")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        call = ToolCall(name="github_create_issue", arguments={"title": "Title"})
        first = await AppService(settings, http).execute(task, call)
        assert first.uncertain and not first.ok
        replay = await AppService(settings, http).execute(task, call)
        assert replay.ok and replay.data["reconciled"]
        assert len(visible) == 1
