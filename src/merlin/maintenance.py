"""Artifact retention with durable workspace references protected from deletion."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from merlin.config import Settings
from merlin.contracts import Task, TaskState, TaskStore, now

TERMINAL = {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}


def prune_artifacts(
    store: TaskStore,
    settings: Settings,
    *,
    apply: bool = False,
    current_time: datetime | None = None,
) -> dict[str, Any]:
    """Remove expired files, retaining active roots and resumable session snapshots.

    GitHub mutation journals live outside artifact_dir and are never swept.
    Call only while worker/API writers are quiesced so new references cannot race
    a retention decision. The API intentionally does not expose this operation.
    """
    cutoff = (current_time or now()) - timedelta(days=settings.artifact_retention_days)
    tasks = store.list_tasks()
    active_roots = {task.root_id for task in tasks if task.state not in TERMINAL}
    latest_sessions: dict[tuple[str, str | None], Task] = {}
    for task in tasks:
        if (
            task.state == TaskState.SUCCEEDED
            and task.spec.parent_id is None
            and task.checkpoint_id is not None
        ):
            key = (task.spec.session_id, task.spec.repository)
            if key not in latest_sessions or latest_sessions[key].created_at < task.created_at:
                latest_sessions[key] = task
    protected: set[str] = set()

    def protect_checkpoint(task_id: str, reference: Any) -> None:
        if not isinstance(reference, str):
            return
        try:
            snapshot = store.get_checkpoint(reference)
        except (KeyError, ValueError):
            return
        if snapshot.task_id == task_id:
            protected.add(snapshot.artifact)

    for task in tasks:
        preserve = task.root_id in active_roots or task in latest_sessions.values()
        if not preserve:
            continue
        checkpoint = store.latest_checkpoint(task.id)
        if checkpoint:
            protected.add(checkpoint.artifact)
        if task.spec.base_checkpoint_id:
            protected.add(store.get_checkpoint(task.spec.base_checkpoint_id).artifact)
        if task.root_id in active_roots:
            for invocation in store.invocations(task.id):
                if invocation.result:
                    protected.update(invocation.result.artifacts)
                protect_checkpoint(task.id, invocation.call.arguments.get("checkpoint_id"))
                if invocation.call.name == "github_prepare_publish" and invocation.result:
                    arguments = invocation.result.data.get("publish_arguments", {})
                    if isinstance(arguments, dict):
                        protect_checkpoint(task.id, arguments.get("checkpoint_id"))
            # A proposal may target an older immutable snapshot than the latest
            # workspace checkpoint. Keep that reviewed content through approval.
            for approval in store.approvals(task.id):
                reference = approval.call.arguments.get("checkpoint_id")
                if approval.status in {"pending", "approved", "consumed"}:
                    protect_checkpoint(task.id, reference)
    root = settings.artifact_dir.resolve()
    candidates = []
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
                continue
            name = path.relative_to(root).as_posix()
            if name in protected or path.stat().st_mtime >= cutoff.timestamp():
                continue
            candidates.append(name)
            if apply:
                path.unlink()
    return {
        "applied": apply,
        "retention_days": settings.artifact_retention_days,
        "expired_artifacts": candidates,
        "protected_artifact_count": len(protected),
    }
