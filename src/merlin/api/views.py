"""Owner-only terminal projections and immutable previews; never run repository code."""

from __future__ import annotations

import base64
import difflib
import hashlib
import os
import tarfile
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from merlin.config import Settings
from merlin.contracts import Record, TaskStore
from merlin.presentation import (
    SessionPage,
    TaskPage,
    TaskSummary,
    checkpoint_file,
    checkpoint_files,
    source_path,
    text_page,
)
from merlin.sandbox._remote import MAX_ARCHIVE_BYTES, validate_archive


def read_snapshot(root: Path, artifact: str, expected_hash: str) -> bytes:
    directory = root.resolve()
    path = (directory / artifact).resolve()
    if not path.is_relative_to(directory):
        raise ValueError("Unavailable checkpoint")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        if os.fstat(stream.fileno()).st_size > MAX_ARCHIVE_BYTES:
            raise ValueError("Unavailable checkpoint")
        data = stream.read(MAX_ARCHIVE_BYTES + 1)
    if len(data) > MAX_ARCHIVE_BYTES or hashlib.sha256(data).hexdigest() != expected_hash:
        raise ValueError("Unavailable checkpoint")
    return data


class SnapshotCache:
    """Verified immutable archives, invalidated by filesystem identity or changes."""

    def __init__(self, max_bytes: int = MAX_ARCHIVE_BYTES) -> None:
        self.max_bytes = max_bytes
        self.size = 0
        self.entries: OrderedDict[tuple[Any, ...], tuple[bytes, dict[str, Any]]] = OrderedDict()
        self.lock = threading.RLock()

    def read(self, root: Path, artifact: str, expected_hash: str) -> tuple[bytes, dict[str, Any]]:
        directory = root.resolve()
        path = (directory / artifact).resolve()
        if not path.is_relative_to(directory):
            raise ValueError("Unavailable checkpoint")
        with self.lock:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                stat = os.fstat(stream.fileno())
                key = (
                    str(path),
                    expected_hash,
                    stat.st_dev,
                    stat.st_ino,
                    stat.st_size,
                    stat.st_mtime_ns,
                    stat.st_ctime_ns,
                )
                if stat.st_size > MAX_ARCHIVE_BYTES:
                    raise ValueError("Unavailable checkpoint")
                data = stream.read(MAX_ARCHIVE_BYTES + 1)
                after = os.fstat(stream.fileno())
            if (
                len(data) > MAX_ARCHIVE_BYTES
                or (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                != (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                or hashlib.sha256(data).hexdigest() != expected_hash
            ):
                raise ValueError("Unavailable checkpoint")
            # Same-size rewrites can share stat metadata on filesystems with
            # coarse timestamps. Recheck bytes, then reuse per-member validation.
            if key in self.entries:
                self.entries.move_to_end(key)
                return self.entries[key]
            manifest = validate_archive(data)
            if len(data) <= self.max_bytes:
                while self.entries and (
                    self.size + len(data) > self.max_bytes or len(self.entries) >= 8
                ):
                    _, (removed, _) = self.entries.popitem(last=False)
                    self.size -= len(removed)
                self.entries[key] = (data, manifest)
                self.size += len(data)
            return data, manifest


def view_router(
    settings: Settings,
    store: TaskStore,
    github: Any,
    run_sync: Callable[..., Awaitable[Any]],
) -> APIRouter:
    router = APIRouter()
    snapshots = SnapshotCache()

    async def require(task_id: str) -> TaskSummary:
        try:
            return await run_sync(store.task_summary, task_id)
        except KeyError as error:
            raise HTTPException(404, "Task not found") from error

    @router.get("/v1/client-info")
    async def info() -> dict[str, Any]:
        return {
            "api_version": "1",
            "default_model": settings.gemini_model,
            "features": [
                "session_views",
                "approval_previews",
                "checkpoint_files",
                "history_pages",
                "budget_amendment",
                "usage_reconciliation",
                "compact_events",
                "task_keyset",
            ],
        }

    @router.get("/v1/sessions", response_model=SessionPage)
    async def sessions(
        repository: str | None = None,
        cursor: Annotated[int, Query(ge=0, le=1000000)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> SessionPage:
        return await run_sync(store.session_summaries, repository, cursor, limit)

    @router.get("/v1/sessions/{session_id}/tasks", response_model=TaskPage)
    async def session_tasks(
        session_id: str,
        cursor: Annotated[int, Query(ge=0, le=1000000)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        before: Annotated[str | None, Query(max_length=64)] = None,
    ) -> TaskPage:
        try:
            return await run_sync(
                partial(store.session_tasks, session_id, cursor, limit, before=before)
            )
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @router.get("/v1/tasks/{task_id}/summary", response_model=TaskSummary)
    async def summary(task_id: str) -> TaskSummary:
        return await require(task_id)

    @router.get("/v1/tasks/{task_id}/approval-summaries")
    async def approval_summaries(task_id: str) -> list[dict[str, Any]]:
        await require(task_id)
        return await run_sync(store.approval_summaries, task_id)

    async def snapshot(
        task_id: str, checkpoint_id: str | None
    ) -> tuple[Any, bytes, dict[str, Any]]:
        await require(task_id)
        try:
            checkpoint = (
                await run_sync(store.get_checkpoint, checkpoint_id)
                if checkpoint_id
                else await run_sync(store.latest_checkpoint, task_id)
            )
            if checkpoint is None:
                raise HTTPException(
                    409, "File suggestions are available after the first checkpoint"
                )
            if checkpoint.task_id != task_id:
                raise KeyError
            data, manifest = await run_sync(
                snapshots.read, settings.artifact_dir, checkpoint.artifact, checkpoint.sha256
            )
            return checkpoint, data, manifest
        except KeyError as error:
            raise HTTPException(404, "Checkpoint not found") from error
        except (OSError, ValueError, tarfile.TarError) as error:
            raise HTTPException(409, "Checkpoint is unavailable or invalid") from error

    @router.get("/v1/tasks/{task_id}/workspace/files")
    async def files(
        task_id: str,
        checkpoint_id: str | None = None,
        query: Annotated[str, Query(max_length=1024)] = "",
        cursor: Annotated[int, Query(ge=0, le=1000000)] = 0,
    ) -> dict[str, Any]:
        checkpoint, data, manifest = await snapshot(task_id, checkpoint_id)
        try:
            page = await run_sync(partial(checkpoint_files, data, query, cursor, manifest=manifest))
        except (ValueError, KeyError, tarfile.TarError) as error:
            raise HTTPException(409, "Checkpoint is unavailable or invalid") from error
        return {**page, "checkpoint_id": checkpoint.id, "created_at": checkpoint.created_at}

    @router.get("/v1/tasks/{task_id}/workspace/file")
    async def file(
        task_id: str,
        path: Annotated[str, Query(min_length=1, max_length=1024)],
        checkpoint_id: str,
        start_line: Annotated[int, Query(ge=1, le=10000000)] = 1,
    ) -> dict[str, Any]:
        checkpoint, data, manifest = await snapshot(task_id, checkpoint_id)
        try:
            page = await run_sync(
                partial(checkpoint_file, data, path, start_line, manifest=manifest)
            )
        except (ValueError, KeyError, tarfile.TarError) as error:
            raise HTTPException(404, "Source file is unavailable") from error
        return {**page, "checkpoint_id": checkpoint.id, "created_at": checkpoint.created_at}

    @router.get("/v1/approvals/{approval_id}/preview")
    async def approval_preview(
        approval_id: str,
        path: str | None = None,
        start_line: Annotated[int, Query(ge=1, le=10000000)] = 1,
    ) -> dict[str, Any]:
        try:
            approval = await run_sync(store.get_approval, approval_id)
        except KeyError as error:
            raise HTTPException(404, "Approval not found") from error
        task = await require(approval.task_id)
        args = approval.call.arguments
        from pydantic import ValidationError

        from merlin.integrations.github import CreateIssue, CreatePR, PublishBranch

        schemas: dict[str, type[Record]] = {
            "github_publish_branch": PublishBranch,
            "github_create_pr": CreatePR,
            "github_create_issue": CreateIssue,
        }
        try:
            schema = schemas[approval.call.name]
            proposal = schema.model_validate(args)
            if isinstance(proposal, PublishBranch) and proposal.checkpoint_id is not None:
                if github is None:
                    raise ValueError("Checkpoint publishing is not configured")
                proposal = await run_sync(github.frozen_publish, approval.task_id, proposal)
            args = proposal.model_dump()
        except (KeyError, ValidationError, ValueError, OSError, tarfile.TarError) as error:
            raise HTTPException(409, "The stored proposal is invalid; request changes") from error
        entries = args.get("files", []) if approval.call.name == "github_publish_branch" else []
        names = [entry["path"] for entry in entries]
        selected = path or (names[0] if names else None)
        result: dict[str, Any] = {
            "approval_id": approval.id,
            "task_id": approval.task_id,
            "digest": approval.digest,
            "status": approval.status,
            "operation": approval.call.name,
            "repository": task.repository,
            "branch": f"merlin/task-{task.id}",
            "base_sha": args.get("base_sha"),
            "checkpoint_id": args.get("checkpoint_id"),
            "checkpoint_sha256": args.get("checkpoint_sha256"),
            "message": args.get("message", ""),
            "title": args.get("title", ""),
            "base": args.get("base", ""),
            "files": names,
            "path": selected,
        }
        if selected is None:
            return {
                **result,
                "preview_kind": "proposal",
                **text_page(args.get("body", ""), start_line),
            }
        if selected not in names:
            raise HTTPException(404, "Proposed file not found")
        entry = entries[names.index(selected)]
        result.update({"mode": entry["mode"], "encoding": entry["encoding"]})
        try:
            source_path(selected)
        except ValueError as error:
            raise HTTPException(
                409, "This source path cannot be reviewed; request changes"
            ) from error
        try:
            proposed = entry.get("content")
            raw = (
                base64.b64decode(proposed, validate=True)
                if entry.get("encoding") == "base64" and proposed is not None
                else (proposed or "").encode("utf-8")
            )
            result.update({"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)})
            text = raw.decode("utf-8")
            if "\x00" in text:
                raise UnicodeError
        except (ValueError, UnicodeError):
            return {
                **result,
                "preview_kind": "binary",
                "content": "Binary or unavailable source preview.",
            }
        result.update({"sha256": hashlib.sha256(raw).hexdigest(), "deleted": proposed is None})
        try:
            if github is None or task.repository is None or len(raw) > 200000:
                raise ValueError
            baseline = await github.read_file_at_commit(task.repository, selected, args["base_sha"])
            if baseline is not None and len(baseline) > 200000:
                raise ValueError
            original = baseline.decode("utf-8") if baseline is not None else ""
            lines = difflib.unified_diff(
                original.splitlines(keepends=True),
                text.splitlines(keepends=True),
                fromfile=f"a/{selected}" if baseline is not None else "/dev/null",
                tofile=f"b/{selected}" if proposed is not None else "/dev/null",
            )
            text = "".join(
                line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                for line in lines
            )
            result["preview_kind"] = "diff"
        except Exception:
            # Arbitrary upstream exceptions may contain tokens or response bodies.
            result["preview_kind"] = "content"
            result["notice"] = (
                "Baseline unavailable. Showing exact proposed content instead of a diff."
            )
        return {**result, **text_page(text, start_line)}

    return router
