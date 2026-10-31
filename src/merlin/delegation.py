"""Isolated child tasks and conflict-aware integration of checkpoint file deltas."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import tarfile
from pathlib import Path
from typing import Any

from merlin.contracts import (
    Lease,
    SandboxClient,
    Task,
    TaskSpec,
    TaskState,
    TaskStore,
    ToolCall,
    ToolExecutor,
    ToolResult,
    Workspace,
)
from merlin.sandbox._remote import MANIFEST, excluded, validate_archive

TERMINAL = {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED, TaskState.PAUSED}


def child_tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "spawn_child",
            "description": "Delegate a coding task to an isolated workspace.",
            "parameters": {
                "type": "object",
                "properties": {"prompt": {"type": "string"}},
                "required": ["prompt"],
                "additionalProperties": False,
            },
        },
        {
            "name": "child_status",
            "description": "Inspect this task's child tasks and results.",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "await_children",
            "description": "Wait for child tasks, preserving cancellation.",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "integrate_child",
            "description": (
                "Apply a successful child's source delta. Conflicts abort the merge; "
                "rerun affected tests afterward."
            ),
            "parameters": {
                "type": "object",
                "properties": {"child_id": {"type": "string"}},
                "required": ["child_id"],
                "additionalProperties": False,
            },
        },
    ]


class DelegatingExecutor:
    def __init__(
        self,
        delegate: ToolExecutor,
        sandbox: SandboxClient,
        store: TaskStore,
        artifact_dir: Path | None = None,
        poll_interval: float = 1.0,
    ) -> None:
        self.delegate, self.sandbox, self.store = delegate, sandbox, store
        self.artifact_dir = artifact_dir or Path(".merlin/artifacts")
        self.poll_interval = poll_interval

    def definitions(self) -> list[dict[str, Any]]:
        return [*self.delegate.definitions(), *child_tool_definitions()]

    def _children(self, task: Task) -> list[Task]:
        children = getattr(self.store, "children", None)
        if children is not None:
            return list(children(task.id))
        return [child for child in self.store.list_tasks() if child.spec.parent_id == task.id]

    def _files(self, checkpoint_id: str) -> dict[str, bytes]:
        checkpoint = self.store.get_checkpoint(checkpoint_id)
        root = self.artifact_dir.resolve()
        path = (root / checkpoint.artifact).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Checkpoint escapes artifact storage")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != checkpoint.sha256:
            raise ValueError("Checkpoint checksum mismatch")
        validate_archive(content)
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
            files = {}
            for member in archive.getmembers():
                if (
                    member.name == MANIFEST
                    or member.name.startswith(".git/")
                    or excluded(member.name)
                ):
                    continue
                stream = archive.extractfile(member)
                if stream is not None:
                    files[member.name] = stream.read()
            return files

    async def execute(self, task: Task, workspace: Workspace, call: ToolCall) -> ToolResult:
        if call.name not in {"spawn_child", "child_status", "await_children", "integrate_child"}:
            return await self.delegate.execute(task, workspace, call)
        if task.spec.parent_id:
            return ToolResult(
                call_id=call.id, ok=False, output="Children cannot delegate or integrate"
            )
        current = self.store.get_task(task.id)
        if current.cancel_requested:
            raise asyncio.CancelledError
        if call.name == "spawn_child":
            if set(call.arguments) != {"prompt"}:
                return ToolResult(call_id=call.id, ok=False, output="Only prompt is accepted")
            session_id = hashlib.sha256(f"{task.id}:{call.id}".encode()).hexdigest()[:32]
            children = self._children(task)
            previous = next(
                (child for child in children if child.spec.session_id == session_id), None
            )
            if previous is None and len(children) >= 8:
                return ToolResult(
                    call_id=call.id, ok=False, output="Maximum eight children per task"
                )
            checkpoint = self.store.latest_checkpoint(task.id)
            if checkpoint is None:
                raise ValueError("Checkpoint the parent before spawning a child")
            spec = (
                previous.spec
                if previous
                else TaskSpec(
                    prompt=str(call.arguments["prompt"]),
                    repository=task.spec.repository,
                    session_id=session_id,
                    parent_id=task.id,
                    permissions=[],
                    allowed_domains=task.spec.allowed_domains,
                    model=task.spec.model,
                    budget=task.spec.budget,
                    base_checkpoint_id=checkpoint.id,
                )
            )
            lease = Lease(
                task_id=task.id, worker_id=task.worker_id or "", generation=task.generation
            )
            child = self.store.create_task(
                spec, idempotency_key=f"child:{task.id}:{call.id}", parent_lease=lease
            )
            return ToolResult(
                call_id=call.id, output=f"Child queued: {child.id}", data={"child_id": child.id}
            )
        if call.name in {"child_status", "await_children"}:
            if call.arguments:
                return ToolResult(call_id=call.id, ok=False, output="No arguments accepted")
            while True:
                children = self._children(task)
                if call.name == "child_status" or all(
                    child.state in TERMINAL for child in children
                ):
                    break
                if self.store.get_task(task.id).cancel_requested:
                    raise asyncio.CancelledError
                await asyncio.sleep(self.poll_interval)
            data = [
                {
                    "id": c.id,
                    "state": c.state.value,
                    "result": c.result,
                    "error": c.error,
                    "checkpoint_id": c.checkpoint_id,
                }
                for c in children
            ]
            return ToolResult(call_id=call.id, output="Child task status", data={"children": data})
        if set(call.arguments) != {"child_id"}:
            return ToolResult(call_id=call.id, ok=False, output="Only child_id is accepted")
        child = self.store.get_task(str(call.arguments["child_id"]))
        if child.spec.parent_id != task.id or child.state != TaskState.SUCCEEDED:
            return ToolResult(
                call_id=call.id, ok=False, output="A successful direct child is required"
            )
        if not child.spec.base_checkpoint_id or not child.checkpoint_id:
            raise ValueError("Child checkpoints are incomplete")
        base = self._files(child.spec.base_checkpoint_id)
        changed = self._files(child.checkpoint_id)
        delta = []
        for name in sorted(set(base) | set(changed)):
            before, after = base.get(name), changed.get(name)
            if before == after:
                continue
            delta.append(
                {
                    "path": name,
                    "base_sha256": hashlib.sha256(before).hexdigest()
                    if before is not None
                    else None,
                    "content_base64": base64.b64encode(after).decode()
                    if after is not None
                    else None,
                }
            )
        if not delta:
            return ToolResult(call_id=call.id, output="Child has no source changes")
        result = await self.sandbox.execute(
            workspace, ToolCall(id=call.id, name="apply_delta", arguments={"changes": delta})
        )
        return result.model_copy(update={"data": {**result.data, "child_id": child.id}})
