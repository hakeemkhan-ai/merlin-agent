"""GitHub App broker: scoped credentials and journaled repository mutations.

No Git commands or repository programs execute in this service. In particular,
write-capable App tokens are never returned by a tool or sent to a sandbox.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import tarfile
import time
from collections import OrderedDict
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import quote, urlencode

import httpx
from pydantic import Field, ValidationError

from merlin.agent import PUBLISHING_PERMISSIONS, requires_approval
from merlin.config import Settings
from merlin.contracts import Record, Task, TaskStore, ToolCall, ToolResult

API = "https://api.github.com"
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA = re.compile(r"^[a-fA-F0-9]{40}$")


class PublishFile(Record):
    path: str = Field(min_length=1, max_length=1024)
    content: str | None = None
    encoding: Literal["utf-8", "base64"] = "utf-8"
    mode: Literal["100644", "100755"] = "100644"


class PublishBranch(Record):
    base_sha: str = Field(pattern=r"^[a-fA-F0-9]{40}$")
    message: str = Field(min_length=1, max_length=10000)
    checkpoint_id: str | None = Field(default=None, min_length=1, max_length=64)
    checkpoint_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    paths: list[str] = Field(default_factory=list, max_length=1000)
    files: list[PublishFile] = Field(default_factory=list, max_length=1000)


class PreparePublish(Record):
    message: str = Field(min_length=1, max_length=10000)
    paths: list[str] = Field(min_length=1, max_length=100)


class CreatePR(Record):
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(default="", max_length=60000)
    base: str = Field(default="", max_length=255)


class CreateIssue(Record):
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(default="", max_length=60000)


class ReadRepository(Record):
    resource: Literal["repository", "issues", "pulls"] = "repository"
    number: int | None = Field(default=None, ge=1)


class ReconciliationRequired(RuntimeError):
    """A prior visible mutation may have succeeded; never blindly repeat it."""


def github_tool_definitions() -> list[dict[str, Any]]:
    schemas: list[tuple[str, str, type[Record]]] = [
        (
            "github_prepare_publish",
            "Prepare source paths from the current durable workspace checkpoint. "
            "Returns exact publish arguments without copying source into context.",
            PreparePublish,
        ),
        (
            "github_publish_branch",
            "Publish a frozen checkpoint proposal returned by github_prepare_publish "
            "to the task branch",
            PublishBranch,
        ),
        ("github_create_pr", "Create a PR for the task branch", CreatePR),
        ("github_create_issue", "Create a repository issue", CreateIssue),
        ("github_read", "Read this task's repository, issues, or pull requests", ReadRepository),
    ]
    return [
        {"name": name, "description": description, "parameters": schema.model_json_schema()}
        for name, description, schema in schemas
    ]


class GitHubService:
    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient | None = None,
        *,
        store: TaskStore | None = None,
    ) -> None:
        self.settings = settings
        self.http = http or httpx.AsyncClient(timeout=30, follow_redirects=False)
        self._owns_http = http is None
        self._state_dir = settings.artifact_dir.parent / "github-state"
        self.store = store
        self._read_tokens: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._token_lock = asyncio.Lock()

    async def aclose(self) -> None:
        self._read_tokens.clear()
        if self._owns_http:
            await self.http.aclose()

    def _directory(self, name: str) -> Path:
        path = self._state_dir / name
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
        try:
            with temporary.open("x", encoding="utf-8") as output:
                temporary.chmod(0o600)
                json.dump(value, output, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

    def _configured(self) -> bool:
        return bool(
            self.settings.github_app_id
            and self.settings.github_private_key_file
            and self.settings.github_installation_id
        )

    def _link(self) -> dict[str, Any]:
        path = self._state_dir / "connection.json"
        if not path.is_file():
            return {}
        record = json.loads(path.read_text())
        if record.get("installation_id") != self.settings.github_installation_id:
            return {}
        return dict(record)

    def connection_status(self) -> dict[str, Any]:
        linked = self._link()
        return {
            "configured": self._configured(),
            "linked": bool(linked),
            "installation_id": self.settings.github_installation_id,
            "user_login": linked.get("user_login"),
            "mode": "oauth" if self.settings.github_client_id else "operator_configured",
        }

    def _require_connection(self) -> None:
        if not self._configured():
            raise ValueError("Configure the GitHub App and its installation first")
        if self.settings.github_client_id and not self._link():
            raise ValueError("Complete GitHub connection before accessing repositories")

    def _app_token(self) -> str:
        if not self.settings.github_app_id or not self.settings.github_private_key_file:
            raise ValueError("GitHub App signing credentials are not configured")
        import jwt

        issued = int(time.time())
        return str(
            jwt.encode(
                {"iat": issued - 60, "exp": issued + 540, "iss": self.settings.github_app_id},
                self.settings.github_private_key_file.read_text(),
                algorithm="RS256",
            )
        )

    async def _request(
        self,
        method: str,
        path: str,
        token: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        missing_ok: bool = False,
    ) -> Any:
        if not path.startswith("/") or ".." in path or "?" in path or "#" in path:
            raise ValueError("Invalid GitHub API path")
        safe_retry = (
            method == "GET"
            or method == "POST"
            and re.fullmatch(r"/app/installations/[0-9]+/access_tokens", path) is not None
        )
        for attempt in range(3):
            response = await self.http.request(
                method,
                API + path,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json=body,
                params=params,
            )
            limited = (
                response.status_code == 429
                or response.status_code == 403
                and (
                    "retry-after" in response.headers
                    or response.headers.get("x-ratelimit-remaining") == "0"
                )
            )
            if not safe_retry or not limited or attempt == 2:
                break
            delay = self._retry_delay(response, attempt)
            if delay is None:
                break
            await asyncio.sleep(delay)
        if response.status_code == 401:
            self._read_tokens.clear()
        if missing_ok and response.status_code == 404:
            return None
        response.raise_for_status()
        try:
            return response.json()
        except ValueError:
            if method != "GET" and not safe_retry:
                # A successful write can have an unreadable response. Treating
                # that as argument rejection would permit a duplicate mutation.
                raise ReconciliationRequired from None
            raise ValueError("GitHub returned an invalid JSON response") from None

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float | None:
        value = response.headers.get("retry-after")
        delay = float(2**attempt)
        try:
            if value:
                try:
                    delay = float(value)
                except ValueError:
                    stamp = parsedate_to_datetime(value)
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=UTC)
                    delay = (stamp - datetime.now(UTC)).total_seconds()
            elif response.headers.get("x-ratelimit-remaining") == "0":
                delay = float(response.headers["x-ratelimit-reset"]) - time.time()
        except (ValueError, TypeError, KeyError, OverflowError):
            return None
        # Never send an early retry when the provider requires a longer pause.
        return max(0, delay) if math.isfinite(delay) and delay <= 30 else None

    def _token_cache_key(self, repository: str | None, permissions: dict[str, str]) -> str:
        identity: dict[str, Any] = {
            "app": self.settings.github_app_id,
            "installation": self.settings.github_installation_id,
            "connection": self._link() if self.settings.github_client_id else "operator",
            "repository": repository,
            "permissions": permissions,
        }
        if self.settings.github_private_key_file is not None:
            try:
                stat = self.settings.github_private_key_file.stat()
                identity["signer"] = [
                    stat.st_dev,
                    stat.st_ino,
                    stat.st_mtime_ns,
                    stat.st_ctime_ns,
                    stat.st_size,
                ]
            except FileNotFoundError:
                identity["signer"] = "unavailable"
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    async def _installation_token(
        self,
        repository: str | None,
        permissions: dict[str, str],
    ) -> str:
        self._require_connection()
        if not permissions or any(value not in {"read", "write"} for value in permissions.values()):
            raise ValueError("Explicit installation token permissions are required")
        if (
            repository is not None
            and self.settings.github_client_id
            and repository not in self._link().get("repositories", [])
        ):
            raise PermissionError("Repository is outside the connected user access")
        cacheable = all(value == "read" for value in permissions.values())
        if cacheable:
            async with self._token_lock:
                key = self._token_cache_key(repository, permissions)
                cached = self._read_tokens.get(key)
                if cached is not None and cached[0] > time.time():
                    self._read_tokens.move_to_end(key)
                    return cached[1]
                token, expires = await self._mint_installation_token(repository, permissions)
                if expires > time.time() + 60:
                    self._read_tokens[key] = (min(expires - 60, time.time() + 300), token)
                    self._read_tokens.move_to_end(key)
                    while len(self._read_tokens) > 64:
                        self._read_tokens.popitem(last=False)
                return token
        return (await self._mint_installation_token(repository, permissions))[0]

    async def _mint_installation_token(
        self, repository: str | None, permissions: dict[str, str]
    ) -> tuple[str, float]:
        app = self._app_token()
        payload: dict[str, Any] = {"permissions": permissions}
        if repository is not None:
            if self.settings.github_client_id and repository not in self._link().get(
                "repositories", []
            ):
                raise PermissionError("Repository is outside the connected user access")
            if not REPOSITORY.fullmatch(repository):
                raise ValueError("Invalid repository")
            installation = await self._request("GET", f"/repos/{repository}/installation", app)
            if installation.get("id") != self.settings.github_installation_id:
                raise PermissionError("Repository is outside the configured installation")
            payload["repositories"] = [repository.split("/", 1)[1]]
        result = await self._request(
            "POST",
            f"/app/installations/{self.settings.github_installation_id}/access_tokens",
            app,
            body=payload,
        )
        token = result.get("token")
        if not isinstance(token, str) or not token or len(token) > 10000:
            raise ValueError("Invalid installation token response")
        expires = 0.0
        try:
            stamp = datetime.fromisoformat(str(result["expires_at"]).replace("Z", "+00:00"))
            if stamp.tzinfo is not None:
                expires = stamp.timestamp()
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
        return token, expires

    async def read_token(self, repository: str) -> str:
        """Return only a repository-scoped, read-only, short-lived clone credential."""
        return await self._installation_token(repository, {"contents": "read"})

    async def read_file_at_commit(self, repository: str, path: str, sha: str) -> bytes | None:
        """Read an immutable approval baseline with a repository-scoped read token."""
        from merlin.presentation import source_path

        if not REPOSITORY.fullmatch(repository) or not SHA.fullmatch(sha):
            raise ValueError("Invalid repository or commit")
        source_path(path)
        token = await self.read_token(repository)
        item = await self._request(
            "GET",
            f"/repos/{repository}/contents/{quote(path, safe='/')}",
            token,
            params={"ref": sha},
            missing_ok=True,
        )
        if item is None:
            return None
        if (
            not isinstance(item, dict)
            or item.get("encoding") != "base64"
            or item.get("size", 0) > 200000
        ):
            raise ValueError("Baseline is not a bounded source file")
        encoded = item.get("content")
        if not isinstance(encoded, str) or len(encoded) > 300000:
            raise ValueError("Baseline is not a bounded source file")
        raw = base64.b64decode("".join(encoded.split()), validate=True)
        if len(raw) > 200000 or len(raw) != item.get("size"):
            raise ValueError("Baseline is not a bounded source file")
        return raw

    async def repositories(self) -> list[dict[str, Any]]:
        token = await self._installation_token(None, {"contents": "read"})
        repositories: list[dict[str, Any]] = []
        for page in range(1, 101):
            response = await self._request(
                "GET", "/installation/repositories", token, params={"per_page": 100, "page": page}
            )
            items = response.get("repositories", [])
            page_size = len(items)
            if self.settings.github_client_id:
                permitted = set(self._link().get("repositories", []))
                items = [item for item in items if item.get("full_name") in permitted]
            repositories.extend(
                {key: item.get(key) for key in ("id", "full_name", "private", "default_branch")}
                for item in items
            )
            if page_size < 100:
                return repositories
        raise ValueError("Installation repository pagination exceeded its safety limit")

    def connection_start(self) -> dict[str, str]:
        self._require_oauth_config()
        nonce = secrets.token_urlsafe(32)
        payload = {
            "nonce": nonce,
            "exp": int(time.time()) + 600,
            "installation_id": self.settings.github_installation_id,
        }
        encoded = (
            base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode())
            .decode()
            .rstrip("=")
        )
        signature = hmac.new(
            self.settings.owner_token.get_secret_value().encode(), encoded.encode(), hashlib.sha256
        ).hexdigest()
        self._write_json(self._directory("oauth") / f"{nonce}.json", payload)
        query = urlencode(
            {
                "client_id": self.settings.github_client_id,
                "redirect_uri": self.settings.github_callback_url,
                "state": f"{encoded}.{signature}",
            }
        )
        return {"authorization_url": f"https://github.com/login/oauth/authorize?{query}"}

    def _require_oauth_config(self) -> None:
        if not (
            self._configured()
            and self.settings.github_client_id
            and self.settings.github_client_secret.get_secret_value()
            and self.settings.owner_token.get_secret_value()
        ):
            raise ValueError("GitHub OAuth and owner authentication must be configured")

    def _consume_state(self, state: str) -> dict[str, Any]:
        self._require_oauth_config()
        try:
            if len(state) > 4096:
                raise ValueError("Invalid OAuth state")
            encoded, signature = state.split(".", 1)
            expected = hmac.new(
                self.settings.owner_token.get_secret_value().encode(),
                encoded.encode(),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(expected, signature):
                raise ValueError("Invalid OAuth state")
            payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            nonce = payload["nonce"]
            if not re.fullmatch(r"[A-Za-z0-9_-]{20,100}", nonce):
                raise ValueError("Invalid OAuth nonce")
            if (
                payload["exp"] < time.time()
                or payload["installation_id"] != self.settings.github_installation_id
            ):
                raise ValueError("Expired OAuth state")
            path = self._directory("oauth") / f"{nonce}.json"
            if json.loads(path.read_text()) != payload:
                raise ValueError("Invalid OAuth state")
            path.unlink()  # Atomic one-use claim; a concurrent callback loses here.
            return dict(payload)
        except (KeyError, TypeError, json.JSONDecodeError, FileNotFoundError, ValueError) as exc:
            raise ValueError("Invalid, expired, or consumed OAuth state") from exc

    async def connection_callback(self, code: str, state: str) -> dict[str, Any]:
        self._consume_state(state)
        response = await self.http.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            json={
                "client_id": self.settings.github_client_id,
                "client_secret": self.settings.github_client_secret.get_secret_value(),
                "code": code,
                "redirect_uri": self.settings.github_callback_url,
            },
        )
        response.raise_for_status()
        result = response.json()
        token = result.get("access_token")
        if not isinstance(token, str) or not token:
            raise PermissionError("GitHub user authorization failed")
        user = await self._request("GET", "/user", token)
        installation = None
        for page in range(1, 101):
            data = await self._request(
                "GET", "/user/installations", token, params={"per_page": 100, "page": page}
            )
            items = data.get("installations", [])
            installation = next(
                (item for item in items if item.get("id") == self.settings.github_installation_id),
                None,
            )
            if installation is not None or len(items) < 100:
                break
        if installation is None or str(installation.get("app_id")) != self.settings.github_app_id:
            raise PermissionError("GitHub user cannot access the configured App installation")
        allowed_repositories: list[str] = []
        for page in range(1, 101):
            data = await self._request(
                "GET",
                f"/user/installations/{self.settings.github_installation_id}/repositories",
                token,
                params={"per_page": 100, "page": page},
            )
            items = data.get("repositories", [])
            allowed_repositories.extend(str(item["full_name"]) for item in items)
            if len(items) < 100:
                break
        else:
            raise PermissionError("User repository access exceeded its safety limit")
        self._write_json(
            self._state_dir / "connection.json",
            {
                "installation_id": self.settings.github_installation_id,
                "user_id": user["id"],
                "user_login": user["login"],
                "repositories": allowed_repositories,
                "verified_at": int(time.time()),
            },
        )
        self._read_tokens.clear()
        return self.connection_status()

    @staticmethod
    def _digest(task: Task, call: ToolCall) -> str:
        data = {
            "task_id": task.id,
            "repository": task.spec.repository,
            "call": call.model_dump(mode="json"),
        }
        return hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _branch(task: Task) -> str:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", task.id):
            raise ValueError("Invalid task identity")
        return f"merlin/task-{task.id}"

    @staticmethod
    def _files(files: list[PublishFile]) -> list[PublishFile]:
        from merlin.sandbox._remote import MANIFEST, excluded

        seen: set[str] = set()
        total = 0
        for entry in files:
            path = PurePosixPath(entry.path)
            parts = entry.path.split("/")
            if (
                path.is_absolute()
                or any(part in ("", ".", "..") for part in parts)
                or "\\" in entry.path
                or "\x00" in entry.path
                or any(ord(char) < 32 for char in entry.path)
                or any(part.lower() == ".git" for part in parts)
                or entry.path == MANIFEST
                or entry.path.lower() in (".github", ".github/workflows")
                or entry.path.lower().startswith(".github/workflows/")
                or excluded(entry.path, caches=False)
            ):
                raise ValueError("Unsafe or forbidden publishing path")
            if entry.path in seen:
                raise ValueError("Duplicate publishing path")
            seen.add(entry.path)
            if entry.content is not None:
                content = (
                    base64.b64decode(entry.content, validate=True)
                    if entry.encoding == "base64"
                    else entry.content.encode("utf-8")
                )
                total += len(content)
                if total > 10 * 1024 * 1024:
                    raise ValueError("Publishing delta exceeds 10 MiB")
        return files

    def frozen_publish(self, task_id: str, proposal: PublishBranch) -> PublishBranch:
        """Materialize exact owned checkpoint bytes before any GitHub request."""
        from merlin.sandbox._remote import MAX_ARCHIVE_BYTES, validate_archive

        if (
            self.store is None
            or proposal.checkpoint_id is None
            or proposal.checkpoint_sha256 is None
        ):
            raise ValueError("Prepare an owned checkpoint before publishing")
        checkpoint = self.store.get_checkpoint(proposal.checkpoint_id)
        if (
            checkpoint.task_id != task_id
            or checkpoint.sha256 != proposal.checkpoint_sha256
            or checkpoint.base_sha != proposal.base_sha
        ):
            raise ValueError("Publishing checkpoint identity does not match the proposal")
        root = self.settings.artifact_dir.resolve()
        relative = Path(checkpoint.artifact)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Checkpoint escapes artifact storage")
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("Checkpoint contains a symlink")
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Checkpoint escapes artifact storage")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            if os.fstat(stream.fileno()).st_size > MAX_ARCHIVE_BYTES:
                raise ValueError("Checkpoint exceeds size limit")
            data = stream.read(MAX_ARCHIVE_BYTES + 1)
        if len(data) > MAX_ARCHIVE_BYTES or hashlib.sha256(data).hexdigest() != checkpoint.sha256:
            raise ValueError("Checkpoint checksum mismatch")
        validate_archive(data)
        paths = proposal.paths or [entry.path for entry in proposal.files]
        if not paths or len(paths) > 1000:
            raise ValueError("Select source paths before publishing")
        self._files([PublishFile(path=value) for value in paths])
        if proposal.files and {entry.path for entry in proposal.files} != set(paths):
            raise ValueError("Proposed file paths differ from the frozen selection")
        supplied = {entry.path: entry for entry in self._files(proposal.files)}
        frozen = []
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            members = {member.name: member for member in archive.getmembers()}
            for name in paths:
                member = members.get(name)
                if member is None:
                    entry = PublishFile(path=name, content=None)
                else:
                    source = archive.extractfile(member)
                    if source is None:
                        raise ValueError("Missing checkpoint file")
                    raw = source.read()
                    entry = PublishFile(
                        path=name,
                        content=base64.b64encode(raw).decode(),
                        encoding="base64",
                        mode="100755" if member.mode & 0o111 else "100644",
                    )
                original = supplied.get(name)
                if original is not None:
                    raw_original = (
                        (
                            base64.b64decode(original.content, validate=True)
                            if original.encoding == "base64"
                            else original.content.encode()
                        )
                        if original.content is not None
                        else None
                    )
                    raw_frozen = (
                        base64.b64decode(entry.content) if entry.content is not None else None
                    )
                    if raw_original != raw_frozen or original.mode != entry.mode:
                        raise ValueError("Proposed bytes or mode differ from the frozen checkpoint")
                frozen.append(entry)
        self._files(frozen)
        return proposal.model_copy(update={"paths": paths, "files": frozen})

    async def _prepare_publish(
        self, task: Task, call: ToolCall, args: PreparePublish
    ) -> ToolResult:
        if self.store is None:
            raise ValueError("Checkpoint storage is not configured")
        checkpoint = await asyncio.to_thread(self.store.latest_checkpoint, task.id)
        if checkpoint is None or checkpoint.task_id != task.id:
            raise ValueError("An owned checkpoint is required before publishing")
        proposal = PublishBranch(
            base_sha=checkpoint.base_sha,
            message=args.message,
            checkpoint_id=checkpoint.id,
            checkpoint_sha256=checkpoint.sha256,
            paths=args.paths,
        )
        frozen = await asyncio.to_thread(self.frozen_publish, task.id, proposal)
        await self._validate_deletions(task, frozen)
        metadata = []
        for entry in frozen.files:
            raw = base64.b64decode(entry.content) if entry.content is not None else None
            metadata.append(
                {
                    "path": entry.path,
                    "deleted": raw is None,
                    "mode": entry.mode,
                    "sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
                    "size": len(raw) if raw is not None else 0,
                }
            )
        return ToolResult(
            call_id=call.id,
            output="Frozen source proposal prepared. Use these exact publish arguments.",
            data={
                "publish_arguments": proposal.model_dump(mode="json", exclude_none=True),
                "files": metadata,
            },
        )

    async def _validate_deletions(self, task: Task, proposal: PublishBranch) -> None:
        deletions = [entry for entry in proposal.files if entry.content is None]
        if not deletions:
            return
        token = await self.read_token(task.spec.repository or "")
        for entry in deletions:
            value = await self._request(
                "GET",
                f"/repos/{task.spec.repository}/contents/{quote(entry.path, safe='/')}",
                token,
                params={"ref": proposal.base_sha},
                missing_ok=True,
            )
            if not isinstance(value, dict) or value.get("type") != "file":
                raise ValueError("A deleted source path must exist as a file at the frozen base")

    async def execute(self, task: Task, call: ToolCall) -> ToolResult:
        if task.spec.repository is None:
            return ToolResult(call_id=call.id, ok=False, output="Select a repository first")
        if call.name not in {"github_read", "github_prepare_publish"}:
            if call.name not in PUBLISHING_PERMISSIONS:
                return ToolResult(call_id=call.id, ok=False, output="Unknown GitHub operation")
            if task.spec.parent_id is not None or requires_approval(task, call):
                return ToolResult(
                    call_id=call.id, ok=False, output="GitHub publishing is not authorized"
                )
        try:
            if call.name == "github_prepare_publish":
                try:
                    return await self._prepare_publish(
                        task, call, PreparePublish.model_validate(call.arguments)
                    )
                except (KeyError, OSError, tarfile.TarError):
                    raise ValueError("Frozen checkpoint is unavailable") from None
            if call.name == "github_read":
                args = ReadRepository.model_validate(call.arguments)
                permission = {
                    "repository": "contents",
                    "issues": "issues",
                    "pulls": "pull_requests",
                }[args.resource]
                token = await self._installation_token(task.spec.repository, {permission: "read"})
                path = f"/repos/{task.spec.repository}"
                if args.resource != "repository":
                    path += f"/{args.resource}"
                    if args.number is not None:
                        path += f"/{args.number}"
                elif args.number is not None:
                    raise ValueError("Repository metadata has no issue or PR number")
                data = await self._request("GET", path, token, params={"per_page": 30})
                rendered = json.dumps(data)
                return ToolResult(
                    call_id=call.id,
                    output=rendered[:64000],
                    data={"resource": data} if len(rendered) <= 64000 else {"truncated": True},
                )
            if call.name == "github_publish_branch":
                publish = PublishBranch.model_validate(call.arguments)
                if publish.checkpoint_id is None or publish.checkpoint_sha256 is None:
                    return ToolResult(
                        call_id=call.id,
                        ok=False,
                        output="Prepare a frozen workspace proposal with github_prepare_publish, "
                        "then issue a fresh publishing call with its exact arguments.",
                    )
                self._files(publish.files)
                try:
                    publish = await asyncio.to_thread(self.frozen_publish, task.id, publish)
                except (KeyError, OSError, tarfile.TarError):
                    raise ValueError("Frozen checkpoint is unavailable") from None
                await self._validate_deletions(task, publish)
                return await self._publish(task, call, publish)
            if call.name == "github_create_pr":
                return await self._create_content(
                    task, call, CreatePR.model_validate(call.arguments)
                )
            return await self._create_content(
                task, call, CreateIssue.model_validate(call.arguments)
            )
        except ReconciliationRequired:
            return ToolResult(
                call_id=call.id,
                ok=False,
                uncertain=True,
                output="Prior GitHub mutation is unresolved; inspect before retrying",
            )
        except (ValidationError, ValueError, PermissionError):
            return ToolResult(
                call_id=call.id, ok=False, output="GitHub arguments or authorization rejected"
            )
        except httpx.HTTPStatusError as exc:
            uncertain = exc.response.status_code >= 500 and call.name not in {
                "github_read",
                "github_prepare_publish",
            }
            return ToolResult(
                call_id=call.id,
                ok=False,
                uncertain=uncertain,
                output=f"GitHub returned HTTP {exc.response.status_code}",
            )
        except httpx.RequestError:
            return ToolResult(
                call_id=call.id,
                ok=False,
                uncertain=call.name not in {"github_read", "github_prepare_publish"},
                output="GitHub transport failed; no automatic mutation retry",
            )

    async def _find_content(
        self, task: Task, token: str, endpoint: str, marker: str
    ) -> dict[str, Any] | None:
        if task.spec.repository is None:
            raise ValueError("Select a repository first")
        for page in range(1, 21):
            params: dict[str, Any] = {
                "state": "all",
                "per_page": 100,
                "page": page,
                "sort": "created",
                "direction": "desc",
            }
            if endpoint == "pulls":
                params["head"] = f"{task.spec.repository.split('/')[0]}:{self._branch(task)}"
            items = await self._request(
                "GET", f"/repos/{task.spec.repository}/{endpoint}", token, params=params
            )
            for item in items:
                if endpoint == "issues" and "pull_request" in item:
                    continue
                if marker in (item.get("body") or ""):
                    return dict(item)
            if len(items) < 100:
                return None
        raise ReconciliationRequired

    async def _create_content(
        self, task: Task, call: ToolCall, args: CreatePR | CreateIssue
    ) -> ToolResult:
        endpoint = "pulls" if isinstance(args, CreatePR) else "issues"
        permission = "pull_requests" if endpoint == "pulls" else "issues"
        token = await self._installation_token(
            task.spec.repository, {permission: "write", "contents": "read"}
        )
        digest = self._digest(task, call)
        marker = f"<!-- merlin-operation:{digest} -->"
        journal = self._directory("operations") / f"{digest}.json"
        prior = json.loads(journal.read_text()) if journal.exists() else {}
        if prior.get("status") == "complete":
            return ToolResult(
                call_id=call.id, output="GitHub operation already completed", data=prior["result"]
            )
        existing = await self._find_content(task, token, endpoint, marker)
        if existing is not None:
            data = {"number": existing["number"], "url": existing["html_url"], "reconciled": True}
            self._write_json(journal, {"status": "complete", "result": data})
            return ToolResult(
                call_id=call.id, output="Existing GitHub operation reconciled", data=data
            )
        if prior.get("status") == "requested":
            raise ReconciliationRequired
        payload = {"title": args.title, "body": f"{args.body}\n\n{marker}"}
        if isinstance(args, CreatePR):
            metadata = await self._request("GET", f"/repos/{task.spec.repository}", token)
            base = args.base or metadata["default_branch"]
            if (
                base.startswith("merlin/")
                or any(char in base for char in "\\\x00?#")
                or ".." in base
            ):
                raise ValueError("Invalid PR base branch")
            payload.update({"head": self._branch(task), "base": base})
        # Write the intent before the visible HTTP mutation. A lost response never causes a repost.
        self._write_json(journal, {"status": "requested", "operation": call.name})
        try:
            result = await self._request(
                "POST", f"/repos/{task.spec.repository}/{endpoint}", token, body=payload
            )
        except httpx.HTTPStatusError as exc:
            if 400 <= exc.response.status_code < 500:
                self._write_json(journal, {"status": "rejected", "operation": call.name})
            raise
        data = {"number": result["number"], "url": result["html_url"]}
        self._write_json(journal, {"status": "complete", "result": data})
        return ToolResult(call_id=call.id, output=f"Created GitHub {endpoint}", data=data)

    async def _publish(self, task: Task, call: ToolCall, args: PublishBranch) -> ToolResult:
        token = await self._installation_token(task.spec.repository, {"contents": "write"})
        prefix = f"/repos/{task.spec.repository}"
        branch = self._branch(task)
        metadata = await self._request("GET", prefix, token)
        if metadata["default_branch"] == branch:
            raise PermissionError("Cannot update the default branch")
        digest = self._digest(task, call)
        marker = f"Merlin-Operation: {digest}"
        reference_path = "/".join((prefix, "git", "ref", "heads", branch))
        reference = await self._request("GET", reference_path, token, missing_ok=True)
        if reference is not None:
            current = reference["object"]["sha"]
            commit = await self._request("GET", f"{prefix}/git/commits/{current}", token)
            if marker in commit.get("message", ""):
                return ToolResult(
                    call_id=call.id,
                    output="Published branch reconciled",
                    data={"branch": branch, "sha": current, "reconciled": True},
                )
            if current != args.base_sha:
                raise ValueError("Task branch changed; refresh the base before publishing")
        parent = await self._request("GET", f"{prefix}/git/commits/{args.base_sha}", token)
        tree: list[dict[str, Any]] = []
        for entry in args.files:
            item: dict[str, Any] = {
                "path": entry.path,
                "mode": entry.mode,
                "type": "blob",
                "sha": None,
            }
            if entry.content is not None:
                blob = await self._request(
                    "POST",
                    f"{prefix}/git/blobs",
                    token,
                    body={"content": entry.content, "encoding": entry.encoding},
                )
                item["sha"] = blob["sha"]
            tree.append(item)
        created_tree = await self._request(
            "POST",
            f"{prefix}/git/trees",
            token,
            body={"base_tree": parent["tree"]["sha"], "tree": tree},
        )
        commit = await self._request(
            "POST",
            f"{prefix}/git/commits",
            token,
            body={
                "message": f"{args.message}\n\n{marker}",
                "tree": created_tree["sha"],
                "parents": [args.base_sha],
            },
        )
        if not SHA.fullmatch(commit["sha"]):
            raise ValueError("Invalid commit response")
        if reference is None:
            await self._request(
                "POST",
                f"{prefix}/git/refs",
                token,
                body={"ref": f"refs/heads/{branch}", "sha": commit["sha"]},
            )
        else:
            await self._request(
                "PATCH",
                f"{prefix}/git/refs/heads/{branch}",
                token,
                body={"sha": commit["sha"], "force": False},
            )
        return ToolResult(
            call_id=call.id,
            output="Published task branch",
            data={"branch": branch, "sha": commit["sha"]},
        )
