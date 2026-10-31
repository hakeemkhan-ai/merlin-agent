#!/usr/bin/env python3
"""Measure synthetic runtime costs inside E2B, without provider credentials.

Results describe the test machine and generated fixtures only. They are not a
model-quality benchmark or a hosted-service latency promise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import statistics
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import event

from merlin.context import RepositoryContext, provider_history
from merlin.context.compaction import compact
from merlin.contracts import (
    Checkpoint,
    ModelReply,
    Task,
    TaskSpec,
    TaskState,
    ToolCall,
    ToolResult,
    Workspace,
)
from merlin.persistence import SQLTaskStore
from merlin.worker import Worker


async def measure(iterations: int, operation: Callable[[], Awaitable[Any]]) -> dict[str, float]:
    samples = []
    for _ in range(iterations):
        start = time.perf_counter()
        await operation()
        samples.append(time.perf_counter() - start)
    ordered = sorted(samples)
    return {
        "p50_seconds": statistics.median(samples),
        "p95_seconds": ordered[math.ceil(len(ordered) * 0.95) - 1],
        "min_seconds": min(samples),
        "samples": len(samples),
    }


class Sandbox:
    """Synthetic I/O latency; never executes a repository command."""

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0

    async def create(self, task: Task) -> Workspace:
        return Workspace(id=task.id)

    async def connect(self, sandbox_id: str) -> Workspace:
        return Workspace(id=sandbox_id)

    async def execute(self, workspace: Workspace, call: ToolCall) -> ToolResult:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.01)
            return ToolResult(call_id=call.id, output="fixture")
        finally:
            self.active -= 1

    async def checkpoint(self, workspace: Workspace, task_id: str) -> Checkpoint:
        return Checkpoint(task_id=task_id, artifact="fixture.tar", sha256="0" * 64)

    async def restore(self, workspace: Workspace, checkpoint: Checkpoint) -> None:
        pass

    async def close(self, workspace: Workspace) -> None:
        pass


class Model:
    async def generate(self, task: Task, history: Any, tools: Any) -> ModelReply:
        if len(history) == 1:
            return ModelReply(
                calls=[ToolCall(name="read_file", arguments={"path": f"{n}.py"}) for n in range(4)]
            )
        return ModelReply(text="Fixture complete")


class Executor:
    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox

    def definitions(self) -> list[dict[str, Any]]:
        return []

    async def execute(self, task: Task, workspace: Workspace, call: ToolCall) -> ToolResult:
        return await self.sandbox.execute(workspace, call)


class InstructionSandbox(Sandbox):
    """Count fixture instruction RPCs without depending on a network clock."""

    def __init__(self) -> None:
        self.reads = 0
        self.active = 0
        self.peak = 0

    async def execute(self, workspace: Workspace, call: ToolCall) -> ToolResult:
        if call.name != "read_file":
            raise RuntimeError("Instruction fixture accepts reads only")
        self.reads += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.002)
            content = {"MERLIN.md": "Preserve the public API.", "src/MERLIN.md": "Use types."}
            path = call.arguments["path"]
            return ToolResult(
                call_id=call.id, ok=path in content, output=content.get(path, "missing")
            )
        finally:
            self.active -= 1


class ContextStore:
    """An empty conversation, so context timing isolates instruction assembly."""

    def list_tasks(self, session_id: str | None = None) -> list[Task]:
        return []

    def latest_event_sequence(self, task_id: str) -> int:
        return 0

    def context_checkpoint(self, task_id: str) -> None:
        return None


async def instruction_cache(iterations: int) -> dict[str, Any]:
    sandbox = InstructionSandbox()
    task = Task(
        spec=TaskSpec(prompt="Inspect the source.", repository="fixture/repo"),
        history=[
            {"role": "user", "parts": [{"text": "Inspect the source."}]},
            {
                "role": "model",
                "parts": [],
                "_merlin_calls": [
                    {"id": "source", "name": "read_file", "arguments": {"path": "src/app.py"}}
                ],
            },
            {
                "role": "user",
                "parts": [{"function_response": {"id": "source", "response": {"ok": True}}}],
            },
        ],
    )
    context = RepositoryContext(sandbox, ContextStore(), instruction_ttl_seconds=3600)
    workspace = Workspace(id="instruction-fixture")

    async def cold() -> Any:
        context.release(workspace.id)
        return await context.build(task, workspace)

    cold_timing = await measure(iterations, cold)
    cold_rpcs = sandbox.reads

    async def warm() -> Any:
        return await context.build(task, workspace)

    warm_timing = await measure(iterations, warm)
    warm_rpcs = sandbox.reads - cold_rpcs
    if cold_rpcs != iterations * 3 or warm_rpcs != 0 or sandbox.peak != 3:
        raise RuntimeError("Instruction fixture did not exercise cached parallel scope reads")
    return {
        "cold": cold_timing,
        "warm": warm_timing,
        "cold_instruction_rpcs": cold_rpcs,
        "warm_instruction_rpcs": warm_rpcs,
        "applicable_paths": 3,
        "maximum_concurrent_rpcs": sandbox.peak,
        "synthetic_rpc_delay_seconds": 0.002,
        "notice": "Read-only builds use the cache. Mutation boundaries revalidate instructions.",
    }


def projection_counters() -> dict[str, Any]:
    resource = [
        {"path": f"src/module_{index}.py", "line": index + 1, "detail": "evidence " * 20}
        for index in range(100)
    ]
    native = {
        "function_call": {"id": "signed", "name": "search", "args": {"query": "evidence"}},
        "thought_signature": "opaque-fixture-signature",
    }
    history = [
        {"role": "model", "parts": [native]},
        {
            "role": "user",
            "parts": [
                {
                    "function_response": {
                        "id": "signed",
                        "response": {
                            "output": json.dumps(resource),
                            "data": {"resource": resource, "sha256": "fixture"},
                        },
                    }
                }
            ],
        },
    ]
    original = deepcopy(history)
    projected = provider_history(history)
    before, after = (len(json.dumps(value).encode("utf-8")) for value in (history, projected))
    signature_preserved = projected[0]["parts"][0] == native
    source_unchanged = history == original
    if after >= before or not signature_preserved or not source_unchanged:
        raise RuntimeError("Projection fixture lost integrity or did not remove exact duplication")
    return {
        "canonical_bytes": before,
        "provider_bytes": after,
        "saved_bytes": before - after,
        "saved_fraction": (before - after) / before,
        "native_signature_preserved": signature_preserved,
        "canonical_history_unchanged": source_unchanged,
        "notice": "Serialized fixture bytes, not provider tokens or billed cost.",
    }


async def benchmark(iterations: int) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    metrics["instruction_context_cache"] = await instruction_cache(iterations)
    metrics["exact_output_projection"] = projection_counters()
    for count in (50, 500, 2000):
        task = Task(
            spec=TaskSpec(prompt="Preserve this exact objective and constraint."),
            history=[
                {
                    "role": "user" if index % 2 == 0 else "model",
                    "parts": [{"text": "synthetic evidence " * 100}],
                }
                for index in range(count)
            ],
        )

        async def context(task: Task = task) -> Any:
            return compact(task, budget=150000, event_sequence=1)

        metrics[f"compact_{count}_entries"] = await measure(iterations, context)

    store = SQLTaskStore("sqlite:///:memory:", max_queued_tasks=10000)
    store.create_schema()
    try:
        cardinality: dict[str, Any] = {}

        def select_stats() -> dict[str, Any]:
            captured: list[str] = []

            def capture(_connection: Any, _cursor: Any, statement: str, *_args: Any) -> None:
                if statement.startswith("SELECT"):
                    captured.append(statement)

            event.listen(store.engine, "before_cursor_execute", capture)
            try:
                page = store.session_summaries(limit=50)
            finally:
                event.remove(store.engine, "before_cursor_execute", capture)
            result = {
                "select_queries": len(captured),
                "returned_sessions": len(page.items),
                "selected_native_history": any(
                    "tasks.history" in statement for statement in captured
                ),
            }
            if result["select_queries"] != 3 or result["selected_native_history"]:
                raise RuntimeError("Session-list fixture regressed its bounded metadata projection")
            return result

        for index in range(200):
            record = store.create_task(
                TaskSpec(prompt="Synthetic session", session_id=f"session-{index}")
            )
            lease = store.claim("benchmark-fixture")
            if lease is None or lease.task_id != record.id:
                raise RuntimeError("Could not lease the session fixture")
            store.update_task(
                lease,
                history=[{"role": "model", "parts": [{"text": "x" * 64000}]}],
                state=TaskState.SUCCEEDED,
                result="Synthetic result",
            )
            if index + 1 in {10, 100}:
                cardinality[str(index + 1)] = select_stats()
        metrics["session_list_cardinality"] = cardinality
        metrics["session_fixture_history_bytes_per_task"] = 64000
        statements: list[str] = []

        def query(_connection: Any, _cursor: Any, statement: str, *_args: Any) -> None:
            if statement.startswith("SELECT"):
                statements.append(statement)

        event.listen(store.engine, "before_cursor_execute", query)
        store.session_summaries(limit=50)
        event.remove(store.engine, "before_cursor_execute", query)
        metrics["session_list_select_queries"] = len(statements)

        async def sessions() -> Any:
            return await asyncio.to_thread(store.session_summaries, limit=50)

        metrics["session_list_200_sessions"] = await measure(iterations, sessions)
        selected = store.list_tasks()[0]
        for _ in range(1000):
            store.append_event(selected.id, "fixture.event", {"size": "x" * 100})

        async def events() -> Any:
            return await asyncio.to_thread(store.events, selected.id, 0, limit=100, max_bytes=64000)

        metrics["event_page_from_1000_events"] = await measure(iterations, events)
        metrics["event_page_items"] = len(await events())
    finally:
        store.close()

    for concurrency in (1, 4):
        peaks: list[int] = []

        async def reads(concurrency: int = concurrency, peaks: list[int] = peaks) -> None:
            database = SQLTaskStore("sqlite:///:memory:")
            database.create_schema()
            try:
                task = database.create_task(
                    TaskSpec(prompt="Synthetic read task", repository="fixture/repo")
                )
                sandbox = Sandbox()
                await Worker(
                    database, Model(), sandbox, Executor(sandbox), parallel_read_tools=concurrency
                ).run_once()
                if (
                    database.get_task(task.id).state != TaskState.SUCCEEDED
                    or len(database.invocations(task.id)) != 4
                ):
                    raise RuntimeError("Benchmark task did not complete all four fixture reads")
                peaks.append(sandbox.peak)
                if sandbox.peak != concurrency:
                    raise RuntimeError("Read concurrency escaped its configured fixture bound")
            finally:
                database.close()

        metrics[f"four_reads_concurrency_{concurrency}"] = await measure(iterations, reads)
        metrics[f"four_reads_peak_concurrency_{concurrency}"] = max(peaks)
    return {
        "schema_version": 1,
        "mode": "synthetic_runtime",
        "started_at": datetime.now(UTC).isoformat(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "database": "SQLite memory",
            "providers": "fake, 10ms per read",
        },
        "metrics": metrics,
        "limitations": "No real model quality, network, PostgreSQL, or production SLO is measured.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--output", type=Path, default=Path(".merlin/reports/performance.json"))
    args = parser.parse_args()
    if not 3 <= args.iterations <= 100:
        parser.error("iterations must be between3 and100")
    report = asyncio.run(benchmark(args.iterations))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
