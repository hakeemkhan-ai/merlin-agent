"""Composition root. Repository commands are delegated only to remote workspaces."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from merlin.config import Settings
from merlin.contracts import Checkpoint, Lease, Task, ToolCall, ToolResult, Workspace, new_id
from merlin.persistence import SQLTaskStore


def make_store(settings: Settings, initialize: bool = False) -> SQLTaskStore:
    if settings.database_url.startswith("sqlite"):
        location = settings.database_url.split("///", 1)[-1]
        if location != ":memory:":
            Path(location).parent.mkdir(parents=True, exist_ok=True)
    store = SQLTaskStore(settings.database_url, max_queued_tasks=settings.max_queued_tasks)
    if initialize:
        if not settings.database_url.startswith("sqlite"):
            raise ValueError("Use Alembic migrations for PostgreSQL")
        store.create_schema()
    return store


class RepositorySandbox:
    """Clone once on fresh tasks; restored tasks use their independent checkpoint."""

    def __init__(self, client: Any, github: Any = None) -> None:
        self.client = client
        self.github = github

    async def create(self, task: Task) -> Workspace:
        if task.spec.repository is None:
            raise ValueError("Select a repository before using workspace tools")
        workspace = await self.client.create(task)
        if not task.checkpoint_id and not task.spec.base_checkpoint_id:
            try:
                token = await self.github.read_token(task.spec.repository) if self.github else ""
                await self.client.bootstrap(workspace, task.spec.repository, token)
            except BaseException:
                await self.client.close(workspace)
                raise
        return workspace

    async def connect(self, sandbox_id: str) -> Workspace:
        return await self.client.connect(sandbox_id)

    async def execute(self, workspace: Workspace, call: ToolCall) -> ToolResult:
        return await self.client.execute(workspace, call)

    async def checkpoint(self, workspace: Workspace, task_id: str) -> Checkpoint:
        return await self.client.checkpoint(workspace, task_id)

    async def restore(self, workspace: Workspace, checkpoint: Checkpoint) -> None:
        await self.client.restore(workspace, checkpoint)

    async def close(self, workspace: Workspace) -> None:
        await self.client.close(workspace)


async def run_worker(settings: Settings | None = None, once: bool = False) -> None:
    from merlin.agent import AgentToolExecutor
    from merlin.integrations.gemini import GeminiClient
    from merlin.integrations.github import GitHubService
    from merlin.sandbox import E2BSandboxClient
    from merlin.telemetry import configure
    from merlin.worker import Worker

    settings = settings or Settings()
    if not settings.gemini_api_key.get_secret_value():
        raise RuntimeError("Configure MERLIN_GEMINI_API_KEY")
    configure(settings.otlp_endpoint)
    from prometheus_client import start_http_server

    metrics_server, _metrics_thread = start_http_server(8001, addr="0.0.0.0")
    store = make_store(settings)
    github = GitHubService(settings, store=store) if settings.github_installation_id else None
    remote = RepositorySandbox(E2BSandboxClient(settings), github)

    async def stream_text(task: Task, delta: str) -> None:
        lease = Lease(task_id=task.id, worker_id=task.worker_id or "", generation=task.generation)
        await asyncio.to_thread(store.append_event, task.id, "model.delta", {"text": delta}, lease)

    model = GeminiClient(settings, on_text=stream_text)
    executor = AgentToolExecutor(remote, publisher=github, store=store)
    # Parent and child tasks share durable budgets while keeping independent workspaces.
    from merlin.context import RepositoryContext, context_tool_definitions
    from merlin.delegation import DelegatingExecutor

    delegated = DelegatingExecutor(executor, remote, store, artifact_dir=settings.artifact_dir)

    async def count_tokens(task: Task, history: list[dict[str, Any]]) -> int:
        definitions = enriched.definitions() if task.spec.repository is not None else []
        return await model.count_context_tokens(task, history, definitions)

    context = RepositoryContext(
        remote,
        store,
        token_counter=count_tokens,
        max_input_tokens=settings.max_input_tokens,
        cache_entries=settings.context_cache_entries,
        instruction_ttl_seconds=settings.instruction_ttl_seconds,
    )

    class ContextExecutor:
        def definitions(self) -> list[dict[str, Any]]:
            return [*delegated.definitions(), *context_tool_definitions()]

        async def execute(self, task: Task, workspace: Workspace, call: ToolCall) -> ToolResult:
            result = await context.execute(task, call, workspace=workspace)
            return result if result is not None else await delegated.execute(task, workspace, call)

    enriched = ContextExecutor()

    async def consume() -> None:
        worker = Worker(
            store,
            model,
            remote,
            enriched,
            context=context,
            model_reservation_usd=model.estimated_call_cost,
            workspace_setup_timeout=settings.workspace_setup_timeout_seconds,
            checkpoint_timeout=settings.checkpoint_timeout_seconds,
            cleanup_timeout=settings.cleanup_timeout_seconds,
            tool_timeout=settings.tool_timeout_seconds,
            parallel_read_tools=settings.parallel_read_tools,
        )
        while True:
            try:
                worked = await worker.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                if once:
                    raise
                # A transient database/claim failure must not cancel other consumers.
                from merlin.telemetry import ERRORS

                ERRORS.labels("worker_supervisor").inc()
                await asyncio.sleep(2)
                continue
            if once:
                return
            if not worked:
                await asyncio.sleep(1)

    try:
        if once:
            await consume()
        else:
            # A root can wait while two child tasks run. Store enforces task eligibility.
            async with asyncio.TaskGroup() as group:
                process_id = new_id()

                async def presence() -> None:
                    while True:
                        with contextlib.suppress(Exception):
                            await asyncio.to_thread(store.worker_heartbeat, process_id)
                            await asyncio.to_thread(Path("/tmp/merlin-worker-heartbeat").touch)
                        await asyncio.sleep(5)

                group.create_task(presence())
                for _ in range(3):
                    group.create_task(consume())
    finally:
        store.close()
        with contextlib.suppress(Exception):
            await model.client.aio.aclose()
        if github:
            with contextlib.suppress(Exception):
                await github.aclose()
        await asyncio.to_thread(metrics_server.shutdown)
        metrics_server.server_close()
