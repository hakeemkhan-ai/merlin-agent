#!/usr/bin/env python3
"""Trusted fake-provider fixture for the real HTTP/PostgreSQL runtime smoke.

This file is mounted into the smoke image, not installed in the Merlin package.
It exercises the production worker and store without running repository commands
or contacting Gemini, E2B, or GitHub. Effect ledgers live outside artifact storage.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from merlin.config import Settings
from merlin.contracts import (
    Checkpoint,
    Event,
    ModelReply,
    Task,
    TaskState,
    ToolCall,
    ToolResult,
    Workspace,
    new_id,
)
from merlin.persistence import SQLTaskStore
from merlin.worker import Worker

EVIDENCE = "trusted fake model and fake sandbox; no external provider execution"
RESULT = "Trusted fake providers completed one in-memory write."
WAIT_SECONDS = 45.0
POLL_SECONDS = 0.1
PROMPTS = {"smoke-complete", "smoke-interrupt"}


class ProbeFailure(RuntimeError):
    """A smoke invariant failed; messages intentionally exclude credentials."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ProbeFailure(message)


def segment(value: str) -> str:
    require(bool(re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", value)), "Invalid fixture identifier")
    return value


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def atomic_json(path: Path, value: Any) -> None:
    """Publish a complete, durable file before signaling the SIGKILL boundary."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".probe-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(json_bytes(value))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    require(isinstance(value, dict), "Fixture record must be a JSON object")
    return value


@dataclass(frozen=True)
class ProbePaths:
    artifacts: Path
    state: Path

    def effects(self, task_id: str) -> Path:
        return self.state / "tasks" / segment(task_id) / "effects.json"

    def restores(self, task_id: str) -> Path:
        return self.state / "tasks" / segment(task_id) / "restores.json"

    @property
    def ready(self) -> Path:
        return self.state / "interrupt-ready"

    def record(self, name: str) -> Path:
        require(name in {"complete", "interrupt"}, "Unknown fixture record")
        return self.state / f"{name}.json"


def call_for(task: Task) -> ToolCall:
    require(task.spec.prompt in PROMPTS, "The trusted worker accepts only smoke fixture prompts")
    call_id = hashlib.sha256(f"{task.id}:runtime-probe-write-v1".encode()).hexdigest()[:32]
    return ToolCall(
        id=call_id,
        name="write_file",
        arguments={"path": "smoke.txt", "content": f"fake-provider-evidence:{task.id}\n"},
    )


class ProbeModel:
    """Derive replies from durable history, never a process-local reply counter."""

    async def generate(
        self, task: Task, history: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelReply:
        call = call_for(task)
        answered = any(
            isinstance(part, dict) and part.get("function_response", {}).get("id") == call.id
            for entry in history
            for part in entry.get("parts", [])
        )
        if answered:
            return ModelReply(text=RESULT)
        return ModelReply(
            calls=[call],
            content={
                "role": "model",
                "parts": [{"function_call": {"name": call.name, "args": call.arguments}}],
            },
        )


@dataclass
class ProbeWorkspace:
    task: Task
    files: dict[str, str] = field(default_factory=dict)


class ProbeSandbox:
    """Only an in-memory fixture write and JSON checkpoints are implemented."""

    def __init__(self, paths: ProbePaths) -> None:
        require(
            not paths.state.resolve().is_relative_to(paths.artifacts.resolve()),
            "Probe state must be outside the artifact directory",
        )
        self.paths = paths
        self.workspaces: dict[str, ProbeWorkspace] = {}

    async def create(self, task: Task) -> Workspace:
        call_for(task)
        workspace = Workspace(id=f"probe-{new_id()}", root="/trusted/in-memory-fixture")
        self.workspaces[workspace.id] = ProbeWorkspace(task=task)
        return workspace

    async def connect(self, sandbox_id: str) -> Workspace:
        require(sandbox_id in self.workspaces, "Fake sandbox does not survive process restart")
        return Workspace(id=sandbox_id, root="/trusted/in-memory-fixture")

    async def execute(self, workspace: Workspace, call: ToolCall) -> ToolResult:
        state = self.workspaces[workspace.id]
        expected = call_for(state.task)
        require(call == expected, "Fake sandbox accepts only its deterministic fixture write")
        state.files["smoke.txt"] = str(call.arguments["content"])
        effect_path = self.paths.effects(state.task.id)
        effects = read_json(effect_path) if effect_path.exists() else {}
        effects[call.id] = effects.get(call.id, 0) + 1
        atomic_json(effect_path, effects)
        if state.task.spec.prompt == "smoke-interrupt":
            atomic_json(self.paths.ready, {"task_id": state.task.id, "call_id": call.id})
            # The driver SIGKILLs the process here. No tool result can be committed.
            await asyncio.sleep(3600)
            raise ProbeFailure("Interrupted fixture was not killed at its ready marker")
        return ToolResult(call_id=call.id, output="Trusted fake sandbox wrote smoke.txt")

    async def checkpoint(self, workspace: Workspace, task_id: str) -> Checkpoint:
        state = self.workspaces[workspace.id]
        require(state.task.id == task_id, "Checkpoint belongs to another fixture task")
        checkpoint_id = new_id()
        artifact = f"{segment(task_id)}/{checkpoint_id}.json"
        value = {"evidence": EVIDENCE, "task_id": task_id, "files": state.files}
        atomic_json(self.paths.artifacts / artifact, value)
        return Checkpoint(
            id=checkpoint_id,
            task_id=task_id,
            artifact=artifact,
            sha256=hashlib.sha256(json_bytes(value)).hexdigest(),
        )

    async def restore(self, workspace: Workspace, checkpoint: Checkpoint) -> None:
        state = self.workspaces[workspace.id]
        target = (self.paths.artifacts / checkpoint.artifact).resolve()
        require(target.is_relative_to(self.paths.artifacts.resolve()), "Unsafe checkpoint path")
        encoded = target.read_bytes()
        require(hashlib.sha256(encoded).hexdigest() == checkpoint.sha256, "Checkpoint hash changed")
        value = json.loads(encoded)
        require(value.get("evidence") == EVIDENCE, "Checkpoint lacks fake-provider evidence")
        files = value.get("files")
        require(
            isinstance(files, dict)
            and set(files) <= {"smoke.txt"}
            and all(isinstance(content, str) for content in files.values()),
            "Invalid fake workspace checkpoint",
        )
        state.files = dict(files)
        restore_path = self.paths.restores(state.task.id)
        restores = read_json(restore_path) if restore_path.exists() else {"count": 0}
        atomic_json(
            restore_path,
            {"count": restores["count"] + 1, "checkpoint_id": checkpoint.id},
        )

    async def close(self, workspace: Workspace) -> None:
        self.workspaces.pop(workspace.id, None)


class ProbeExecutor:
    def __init__(self, sandbox: ProbeSandbox) -> None:
        self.sandbox = sandbox

    def definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "write_file",
                "description": "Trusted in-memory runtime smoke fixture only",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
            }
        ]

    async def execute(self, task: Task, workspace: Workspace, call: ToolCall) -> ToolResult:
        require(call == call_for(task), "Unexpected fixture task or call")
        return await self.sandbox.execute(workspace, call)


async def serve_worker(store: SQLTaskStore, paths: ProbePaths) -> None:
    sandbox = ProbeSandbox(paths)
    worker = Worker(
        store,
        ProbeModel(),
        sandbox,
        ProbeExecutor(sandbox),
        lease_seconds=3,
        heartbeat_interval=0.2,
    )

    async def presence() -> None:
        while True:
            await asyncio.to_thread(store.worker_heartbeat, "runtime-probe-worker")
            Path("/tmp/merlin-worker-heartbeat").touch(mode=0o600)
            await asyncio.sleep(1)

    async def consume() -> None:
        while True:
            if not await worker.run_once():
                await asyncio.sleep(POLL_SECONDS)

    async with asyncio.TaskGroup() as group:
        group.create_task(presence())
        group.create_task(consume())


async def request_json(client: httpx.AsyncClient, method: str, path: str, **kwargs: Any) -> Any:
    response = await client.request(method, path, **kwargs)
    require(
        response.is_success, f"Runtime HTTP check failed: {method} {path} ({response.status_code})"
    )
    return response.json()


async def wait_for_task(
    client: httpx.AsyncClient,
    task_id: str,
    expected: TaskState,
    timeout: float = WAIT_SECONDS,
) -> Task:
    try:
        async with asyncio.timeout(timeout):
            while True:
                task = Task.model_validate(
                    await request_json(client, "GET", f"/v1/tasks/{task_id}")
                )
                if task.state == expected:
                    return task
                require(
                    task.state in {TaskState.QUEUED, TaskState.RUNNING},
                    f"Fixture task stopped in unexpected state: {task.state.value}",
                )
                await asyncio.sleep(POLL_SECONDS)
    except TimeoutError:
        raise ProbeFailure(f"Timed out waiting for fixture task state: {expected.value}") from None


async def task_events(client: httpx.AsyncClient, task_id: str, after: int = 0) -> list[Event]:
    response = await client.get(f"/v1/tasks/{task_id}/events", params={"after": after})
    require(response.status_code == 200, "Could not read fixture task events")
    events = [
        Event.model_validate_json(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    require(all(event.task_id == task_id for event in events), "Events crossed task boundaries")
    require(
        [event.sequence for event in events] == list(range(after + 1, after + len(events) + 1)),
        "Fixture event sequence has gaps or duplicates",
    )
    return events


def check_one_effect(paths: ProbePaths, task: Task) -> None:
    require(
        read_json(paths.effects(task.id)) == {call_for(task).id: 1},
        "Fixture write was missing or executed more than once",
    )


async def submit(client: httpx.AsyncClient, prompt: str) -> Task:
    return Task.model_validate(
        await request_json(
            client,
            "POST",
            "/v1/tasks",
            json={
                "prompt": prompt,
                "repository": "smoke/runtime-fixture",
                "model": "trusted-runtime-fixture",
                "budget": {"max_calls": 4, "max_sandbox_seconds": 3600, "max_cost_usd": 1},
            },
            headers={"Idempotency-Key": f"runtime-probe-{new_id()}"},
        )
    )


async def completed_evidence(
    client: httpx.AsyncClient, paths: ProbePaths, task: Task
) -> dict[str, Any]:
    require(task.result == RESULT, "Fixture task result was not durable")
    require(task.spec.prompt == "smoke-complete", "Completion record referenced another fixture")
    check_one_effect(paths, task)
    events = await task_events(client, task.id)
    kinds = [event.kind for event in events]
    finished = [event for event in events if event.kind == "tool.finished"]
    require(
        kinds.count("tool.started") == 1
        and len(finished) == 1
        and finished[0].data.get("call_id") == call_for(task).id
        and finished[0].data.get("result", {}).get("call_id") == call_for(task).id
        and finished[0].data.get("result", {}).get("ok") is True,
        "Write event mismatch",
    )
    require(kinds.count("model.reply") == 2, "Fixture model history changed")
    require(kinds.count("checkpoint.saved") >= 3, "Worker did not save fixture checkpoints")
    require(bool(events) and events[0].kind == "task.created", "Task creation event was lost")
    require(
        any(
            event.kind == "task.updated" and event.data.get("state") == "succeeded"
            for event in events
        ),
        "Task success event was lost",
    )
    require(
        await task_events(client, task.id, after=events[0].sequence) == events[1:],
        "SSE replay changed",
    )
    checkpoint = Checkpoint.model_validate(
        await request_json(client, "GET", f"/v1/tasks/{task.id}/checkpoint")
    )
    require(task.checkpoint_id == checkpoint.id, "Task checkpoint reference changed")
    response = await client.get(f"/v1/tasks/{task.id}/artifacts/{checkpoint.artifact}")
    require(response.status_code == 200, "Checkpoint artifact is not downloadable")
    require(
        hashlib.sha256(response.content).hexdigest() == checkpoint.sha256,
        "Downloaded checkpoint hash differs from durable metadata",
    )
    require(
        response.json()
        == {
            "evidence": EVIDENCE,
            "task_id": task.id,
            "files": {"smoke.txt": call_for(task).arguments["content"]},
        },
        "Downloaded checkpoint lost the fixture write",
    )
    private = await client.get(f"/v1/tasks/{task.id}/artifacts/tasks/{task.id}/effects.json")
    require(private.status_code == 404, "Fixture effect ledger was exposed as an artifact")
    unauthenticated = await client.get(f"/v1/tasks/{task.id}", headers={"Authorization": ""})
    require(unauthenticated.status_code == 401, "Fixture task accepted unauthenticated access")
    return {
        "task_id": task.id,
        "generation": task.generation,
        "result": task.result,
        "checkpoint_id": checkpoint.id,
        "checkpoint_sha256": checkpoint.sha256,
        "events_sha256": hashlib.sha256(
            json_bytes([event.model_dump(mode="json") for event in events])
        ).hexdigest(),
        "evidence": EVIDENCE,
    }


async def complete(client: httpx.AsyncClient, paths: ProbePaths) -> None:
    await readiness(client, paths)
    task = await submit(client, "smoke-complete")
    task = await wait_for_task(client, task.id, TaskState.SUCCEEDED)
    atomic_json(paths.record("complete"), await completed_evidence(client, paths, task))
    print("PASS: HTTP task, durable events, checkpoint, and one write (fake providers)", flush=True)


async def verify_complete(client: httpx.AsyncClient, paths: ProbePaths) -> None:
    await readiness(client, paths)
    saved = read_json(paths.record("complete"))
    task = await wait_for_task(client, segment(saved["task_id"]), TaskState.SUCCEEDED)
    require(
        await completed_evidence(client, paths, task) == saved,
        "Completed task changed after restart",
    )
    print(
        "PASS: completed task and one effect survived API/worker restart (fake providers)",
        flush=True,
    )


async def interrupt(client: httpx.AsyncClient, paths: ProbePaths) -> None:
    with contextlib.suppress(FileNotFoundError):
        paths.ready.unlink()
    task = await submit(client, "smoke-interrupt")
    atomic_json(paths.record("interrupt"), {"task_id": task.id, "evidence": EVIDENCE})
    try:
        async with asyncio.timeout(WAIT_SECONDS):
            while True:
                if paths.ready.exists():
                    ready = read_json(paths.ready)
                    if ready == {"task_id": task.id, "call_id": call_for(task).id}:
                        check_one_effect(paths, task)
                        print(
                            "PASS: durable effect reached interruption boundary (fake providers)",
                            flush=True,
                        )
                        return
                current = Task.model_validate(
                    await request_json(client, "GET", f"/v1/tasks/{task.id}")
                )
                require(
                    current.state in {TaskState.QUEUED, TaskState.RUNNING},
                    "Interrupted fixture stopped before its ready marker",
                )
                await asyncio.sleep(POLL_SECONDS)
    except TimeoutError:
        raise ProbeFailure("Timed out waiting for durable interruption boundary") from None


async def verify_paused(client: httpx.AsyncClient, paths: ProbePaths) -> None:
    await readiness(client, paths)
    saved = read_json(paths.record("interrupt"))
    task = await wait_for_task(client, segment(saved["task_id"]), TaskState.PAUSED)
    require(task.spec.prompt == "smoke-interrupt", "Interrupted record referenced another fixture")
    require(task.generation >= 2, "Worker did not reclaim the expired interrupted lease")
    require(
        task.result is None and "uncertain" in (task.error or ""),
        "Recovery did not report uncertain intent",
    )
    check_one_effect(paths, task)
    events = await task_events(client, task.id)
    kinds = [event.kind for event in events]
    require(
        kinds.count("tool.prepared") == kinds.count("tool.started") == 1,
        "Interrupted intent was replayed",
    )
    require("tool.finished" not in kinds, "Interrupted tool unexpectedly committed a result")
    require(
        any(
            event.kind == "task.claimed" and event.data.get("reclaimed") is True for event in events
        ),
        "No durable reclaim event was recorded",
    )
    print(
        "PASS: expired lease reclaimed and uncertain write paused without replay (fake providers)",
        flush=True,
    )
    paused_checkpoint = task.checkpoint_id
    note = "Trusted smoke operator inspected the durable effect ledger; do not replay the write."
    call = call_for(task)
    await request_json(
        client,
        "POST",
        f"/v1/tasks/{task.id}/tools/{call.id}/resolve",
        json={"note": note},
    )
    task = await wait_for_task(client, task.id, TaskState.SUCCEEDED)
    require(task.result == RESULT, "Acknowledged fixture task did not complete")
    check_one_effect(paths, task)
    responses = [
        part["function_response"]["response"]
        for entry in task.history
        for part in entry.get("parts", [])
        if isinstance(part, dict) and part.get("function_response", {}).get("id") == call.id
    ]
    require(
        len(responses) == 1
        and responses[0].get("ok") is False
        and responses[0].get("uncertain") is False
        and note in responses[0].get("output", ""),
        "Operator acknowledgement was not preserved as a failed tool result",
    )
    require(
        read_json(paths.restores(task.id)) == {"count": 1, "checkpoint_id": paused_checkpoint},
        "Acknowledgement did not restore the last completed fixture checkpoint",
    )
    events = await task_events(client, task.id)
    require(
        sum(event.kind == "tool.operator_resolved" for event in events) == 1
        and sum(event.kind == "tool.started" for event in events) == 1,
        "Acknowledged write was replayed or its resolution event was lost",
    )
    checkpoint = Checkpoint.model_validate(
        await request_json(client, "GET", f"/v1/tasks/{task.id}/checkpoint")
    )
    snapshot = await request_json(
        client, "GET", f"/v1/tasks/{task.id}/artifacts/{checkpoint.artifact}"
    )
    require(snapshot.get("files") == {}, "Recovery trusted the uncertain in-memory workspace")
    print(
        "PASS: explicit acknowledgement restored checkpoint; no write replay (fake providers)",
        flush=True,
    )


async def readiness(client: httpx.AsyncClient, _paths: ProbePaths) -> None:
    async with asyncio.timeout(WAIT_SECONDS):
        while True:
            liveness = await client.get("/healthz")
            require(liveness.status_code == 200, "API liveness probe failed")
            response = await client.get("/readyz")
            if response.status_code == 200:
                status = response.json()
                require(
                    status.get("ready") is True
                    and status.get("database") == "ready"
                    and status.get("schema") == "ready"
                    and status.get("worker_ready") is True
                    and status.get("worker_count", 0) >= 1,
                    "API readiness did not verify schema and worker presence",
                )
                print("PASS: API liveness and migrated database/worker readiness", flush=True)
                return
            require(response.status_code == 503, "Unexpected readiness response")
            await asyncio.sleep(POLL_SECONDS)


async def run(command: str) -> None:
    settings = Settings(
        _env_file=None,
        artifact_dir=Path(os.environ.get("MERLIN_ARTIFACT_DIR", "/data/artifacts")),
        api_url=os.environ.get("MERLIN_API_URL", "http://api:8000"),
    )
    paths = ProbePaths(
        settings.artifact_dir, Path(os.environ.get("MERLIN_PROBE_DIR", "/data/probe"))
    )
    ProbeSandbox(paths)  # Reject overlapping fixture state even for verification commands.
    if command == "worker":
        require(bool(os.environ.get("MERLIN_DATABASE_URL")), "MERLIN_DATABASE_URL is required")
        store = SQLTaskStore(settings.database_url)
        try:
            print("READY: real worker/store with trusted fake model and fake sandbox", flush=True)
            await serve_worker(store, paths)
        finally:
            store.close()
        return
    token = settings.owner_token.get_secret_value()
    require(len(token) >= 32, "MERLIN_OWNER_TOKEN must contain at least 32 characters")
    commands = {
        "ready": readiness,
        "complete": complete,
        "verify-complete": verify_complete,
        "interrupt": interrupt,
        "verify-paused": verify_paused,
    }
    async with (
        httpx.AsyncClient(
            base_url=settings.api_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
            trust_env=False,
        ) as client,
        asyncio.timeout(WAIT_SECONDS),
    ):
        await commands[command](client, paths)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=["worker", "ready", "complete", "verify-complete", "interrupt", "verify-paused"],
    )
    arguments = parser.parse_args(argv)
    try:
        asyncio.run(run(arguments.command))
    except ProbeFailure as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        print(
            f"FAIL: runtime fixture check failed ({type(error).__name__})",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
