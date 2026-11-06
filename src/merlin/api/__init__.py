"""Owner-authenticated task API and resumable server-sent events."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from merlin.config import Settings
from merlin.contracts import (
    Approval,
    Budget,
    Checkpoint,
    Event,
    Task,
    TaskSpec,
    TaskState,
    TaskStore,
)
from merlin.network import validate_domains as validate_domains

PERMISSIONS = {
    "publish_branch",
    "create_pr",
    "create_issue",
    "github_publish_branch",
    "github_create_pr",
    "github_create_issue",
}
STOP_STREAM = {
    TaskState.SUCCEEDED,
    TaskState.FAILED,
    TaskState.CANCELLED,
    TaskState.PAUSED,
    TaskState.WAITING_FOR_APPROVAL,
}


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=100000)
    permissions: list[str] = Field(default_factory=list)


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: bool


class ToolResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(min_length=1, max_length=4000)


class BudgetAmendment(ToolResolution):
    budget: Budget


class UsageResolution(ToolResolution):
    actual_cost_usd: float = Field(ge=0, allow_inf_nan=False)


def validate_spec(spec: TaskSpec) -> TaskSpec:
    if spec.parent_id is not None or spec.base_checkpoint_id is not None:
        raise ValueError("Parent tasks and base checkpoints are managed by Merlin")
    if set(spec.permissions) - PERMISSIONS:
        raise ValueError("Unknown task publishing permission")
    if len(spec.permissions) > len(PERMISSIONS):
        raise ValueError("Too many task permissions")
    if spec.repository is None and spec.permissions:
        raise ValueError("Publishing permissions require a selected repository")
    return spec.model_copy(
        update={
            "permissions": list(dict.fromkeys(spec.permissions)),
            "allowed_domains": validate_domains(spec.allowed_domains),
        }
    )


def encode_event(event: Event) -> str:
    return f"id: {event.sequence}\nevent: {event.kind}\ndata: {event.model_dump_json()}\n\n"


def create_app(
    settings: Settings | None = None,
    store: TaskStore | None = None,
    github: Any = None,
    poll_interval: float = 0.25,
    run_sync: Callable[..., Awaitable[Any]] = asyncio.to_thread,
) -> FastAPI:
    settings = settings or Settings()
    owner_token = settings.owner_token.get_secret_value()
    if len(owner_token) < 32:
        raise ValueError("MERLIN_OWNER_TOKEN must contain at least 32 characters")
    owns_store, owns_github = store is None, github is None
    if store is None:
        from merlin.application import make_store

        store = make_store(settings, initialize=False)
    task_store = store

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            try:
                if owns_github and github is not None:
                    await github.aclose()
            finally:
                if owns_store:
                    await run_sync(task_store.close)

    app = FastAPI(title="Merlin", version="0.1.0", docs_url=None, redoc_url=None, lifespan=lifespan)
    from merlin.api.limits import RequestLimits

    app.add_middleware(
        RequestLimits,
        owner_token=owner_token,
        max_body_bytes=getattr(settings, "max_request_bytes", 1048576),
        max_streams=getattr(settings, "max_event_streams", 32),
    )
    app.state.store = task_store
    app.state.settings = settings
    app.state.github = github
    if github is None and settings.github_app_id:
        from merlin.integrations.github import GitHubService

        github = GitHubService(settings, store=task_store)
        app.state.github = github

    async def authenticated(authorization: Annotated[str | None, Header()] = None) -> None:
        supplied = ""
        if authorization and authorization.startswith("Bearer "):
            supplied = authorization[7:]
        if not hmac.compare_digest(supplied.encode(), owner_token.encode()):
            raise HTTPException(
                401, "Owner authentication required", headers={"WWW-Authenticate": "Bearer"}
            )

    protected = [Depends(authenticated)]

    from merlin.api.views import view_router

    app.include_router(view_router(settings, task_store, github, run_sync), dependencies=protected)

    def require_task(task_id: str) -> Task:
        try:
            return task_store.get_task(task_id)
        except KeyError as error:
            raise HTTPException(404, "Task not found") from error

    def control(task_id: str) -> dict[str, Any]:
        try:
            method = getattr(task_store, "task_control", None)
            if method is not None:
                return dict(method(task_id))
            value = task_store.get_task(task_id)
            return {"state": value.state, "cancel_requested": value.cancel_requested}
        except KeyError as error:
            raise HTTPException(404, "Task not found") from error

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def ready() -> JSONResponse:
        try:
            result = await run_sync(
                task_store.readiness, getattr(settings, "readiness_worker_max_age_seconds", 30)
            )
        except Exception:
            result = {
                "ready": False,
                "database": "unavailable",
                "schema": "unavailable",
                "worker_ready": False,
            }
        return JSONResponse(result, status_code=200 if result["ready"] else 503)

    @app.get("/metrics")
    async def metrics() -> Response:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        from merlin.telemetry import TASKS

        for state in TaskState:
            TASKS.labels(state.value)
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/v1/tasks", response_model=list[Task], dependencies=protected)
    async def list_tasks(session_id: str | None = None) -> list[Task]:
        return await run_sync(task_store.list_tasks, session_id)

    @app.post("/v1/tasks", response_model=Task, status_code=201, dependencies=protected)
    async def create_task(
        spec: TaskSpec,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> Task:
        try:
            validated = validate_spec(spec)
            if "model" not in spec.model_fields_set:
                validated = validated.model_copy(update={"model": settings.gemini_model})
            if idempotency_key and "session_id" not in spec.model_fields_set:
                validated = validated.model_copy(
                    update={
                        "session_id": hashlib.sha256(idempotency_key.encode()).hexdigest()[:32],
                    }
                )
            # Keep submission data stable across retries. The worker selects a
            # predecessor checkpoint when this queued task actually starts.
            # The store checks repository identity atomically without loading
            # every predecessor's native model/tool history into the API.
            return await run_sync(task_store.create_task, validated, idempotency_key)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.get("/v1/tasks/{task_id}", response_model=Task, dependencies=protected)
    async def get_task(task_id: str) -> Task:
        return await run_sync(require_task, task_id)

    @app.get("/v1/tasks/{task_id}/usage", dependencies=protected)
    async def usage(task_id: str) -> dict[str, Any]:
        await run_sync(control, task_id)
        return await run_sync(task_store.usage, task_id)

    @app.get("/v1/tasks/{task_id}/reservations", dependencies=protected)
    async def reservations(task_id: str) -> list[dict[str, Any]]:
        await run_sync(control, task_id)
        return list(await run_sync(task_store.reservations, task_id))

    @app.post("/v1/tasks/{task_id}/budget", response_model=Task, dependencies=protected)
    async def amend_budget(task_id: str, amendment: BudgetAmendment) -> Task:
        await run_sync(control, task_id)
        try:
            return await run_sync(
                task_store.amend_budget, task_id, amendment.budget, amendment.note
            )
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/v1/tasks/{task_id}/reservations/{reservation_id}/resolve", dependencies=protected)
    async def reconcile_usage(
        task_id: str, reservation_id: str, resolution: UsageResolution
    ) -> dict[str, str]:
        await run_sync(control, task_id)
        try:
            await run_sync(
                task_store.reconcile_reservation,
                task_id,
                reservation_id,
                resolution.actual_cost_usd,
                resolution.note,
            )
        except KeyError as error:
            raise HTTPException(404, "Reservation not found") from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return {"status": "reconciled"}

    @app.get("/v1/tasks/{task_id}/history", dependencies=protected)
    async def history_page(
        task_id: str,
        cursor: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        await run_sync(control, task_id)
        return dict(await run_sync(task_store.history_page, task_id, cursor, limit))

    @app.get(
        "/v1/tasks/{task_id}/checkpoint", response_model=Checkpoint | None, dependencies=protected
    )
    async def checkpoint_metadata(task_id: str) -> Checkpoint | None:
        await run_sync(control, task_id)
        return await run_sync(task_store.latest_checkpoint, task_id)

    @app.post(
        "/v1/tasks/{task_id}/messages", response_model=Task, status_code=201, dependencies=protected
    )
    async def follow_up(
        task_id: str,
        message: MessageRequest,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> Task:
        current = await run_sync(require_task, task_id)
        if current.spec.parent_id is not None:
            raise HTTPException(409, "Follow-up messages belong to root tasks")
        spec = current.spec.model_copy(
            update={
                "prompt": message.prompt,
                "parent_id": None,
                "base_checkpoint_id": None,
                "permissions": message.permissions,
            }
        )
        try:
            return await run_sync(task_store.create_task, validate_spec(spec), idempotency_key)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/v1/tasks/{task_id}/cancel", response_model=Task, dependencies=protected)
    async def cancel(task_id: str) -> Task:
        await run_sync(require_task, task_id)
        return await run_sync(task_store.cancel, task_id)

    @app.post("/v1/tasks/{task_id}/resume", response_model=Task, dependencies=protected)
    async def resume(task_id: str) -> Task:
        await run_sync(require_task, task_id)
        try:
            return await run_sync(task_store.resume, task_id)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.post(
        "/v1/tasks/{task_id}/tools/{call_id}/resolve", response_model=Task, dependencies=protected
    )
    async def resolve_tool(task_id: str, call_id: str, resolution: ToolResolution) -> Task:
        await run_sync(require_task, task_id)
        try:
            return await run_sync(task_store.resolve_tool, task_id, call_id, resolution.note)
        except KeyError as error:
            raise HTTPException(404, "Tool invocation not found") from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.get("/v1/tasks/{task_id}/approvals", response_model=list[Approval], dependencies=protected)
    async def approvals(task_id: str) -> list[Approval]:
        await run_sync(require_task, task_id)
        return await run_sync(task_store.approvals, task_id)

    @app.post("/v1/approvals/{approval_id}", response_model=Approval, dependencies=protected)
    async def decide(approval_id: str, decision: ApprovalDecision) -> Approval:
        try:
            return await run_sync(task_store.decide_approval, approval_id, decision.approved)
        except KeyError as error:
            raise HTTPException(404, "Approval not found") from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.post(
        "/v1/approvals/{approval_id}/changes", response_model=Approval, dependencies=protected
    )
    async def approval_changes(approval_id: str, feedback: ToolResolution) -> Approval:
        try:
            return await run_sync(task_store.request_approval_changes, approval_id, feedback.note)
        except KeyError as error:
            raise HTTPException(404, "Approval not found") from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.get("/v1/tasks/{task_id}/events", dependencies=protected)
    async def events(
        request: Request,
        task_id: str,
        after: Annotated[int, Query(ge=0)] = 0,
        last_event_id: Annotated[str | None, Header()] = None,
        compact: bool = False,
    ) -> StreamingResponse:
        await run_sync(control, task_id)
        cursor = after
        if last_event_id is not None:
            if not re.fullmatch(r"[0-9]{1,18}", last_event_id):
                raise HTTPException(400, "Last-Event-ID must be a nonnegative event sequence")
            cursor = max(cursor, int(last_event_id))

        def batch_after(sequence: int) -> list[Event]:
            return list(task_store.events(task_id, sequence, limit=256, max_bytes=262144))

        def render(item: Event) -> str:
            if compact and item.kind in {"tool.started", "tool.finished"}:
                from merlin.presentation import compact_event

                item = compact_event(item)
            return encode_event(item)

        async def stream() -> AsyncIterator[str]:
            nonlocal cursor
            quiet_polls = 0
            while not await request.is_disconnected():
                batch = await run_sync(batch_after, cursor)
                for item in batch:
                    cursor = item.sequence
                    yield render(item)
                current = await run_sync(control, task_id)
                if current["state"] in STOP_STREAM:
                    # Fetch once more after the terminal state read so an event
                    # committed between the first fetch and state read is kept.
                    tail = await run_sync(batch_after, cursor)
                    for item in tail:
                        cursor = item.sequence
                        yield render(item)
                    if not tail or cursor >= current.get("event_sequence", cursor + 1):
                        break
                    continue
                if batch and cursor < current.get("event_sequence", cursor):
                    # A byte-limited page can contain fewer than 256 events.
                    # Drain existing backlog without adding a polling delay to
                    # every page, while store awaits preserve responsiveness.
                    continue
                if len(batch) == 256:
                    continue
                quiet_polls += 1
                if quiet_polls >= 40:
                    quiet_polls = 0
                    yield ": keepalive\n\n"
                await asyncio.sleep(poll_interval)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/v1/tasks/{task_id}/artifacts/{artifact:path}", dependencies=protected)
    async def artifact(task_id: str, artifact: str) -> Response:
        await run_sync(control, task_id)
        recorded: set[str] = set()
        checkpoint = await run_sync(task_store.latest_checkpoint, task_id)
        if checkpoint:
            recorded.add(checkpoint.artifact)
        for invocation in await run_sync(task_store.invocations, task_id):
            if invocation.result:
                recorded.update(invocation.result.artifacts)
        if artifact not in recorded:
            raise HTTPException(404, "Artifact not found")
        root = settings.artifact_dir.resolve()
        target = (root / artifact).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise HTTPException(404, "Artifact not found")
        if target.stat().st_size > 64 * 1024 * 1024:
            raise HTTPException(413, "Artifact exceeds download size limit")
        return FileResponse(
            target,
            media_type="application/octet-stream",
            headers={"Cache-Control": "private, no-cache", "X-Content-Type-Options": "nosniff"},
        )

    def github_service() -> Any:
        if github is None:
            raise HTTPException(503, "GitHub App is not configured")
        return github

    @app.get("/v1/github/status", dependencies=protected)
    async def github_status() -> dict[str, Any]:
        if github is None:
            return {
                "configured": False,
                "linked": False,
                "installation_id": None,
                "user_login": None,
            }
        return dict(await run_sync(github.connection_status))

    @app.post("/v1/github/connect", dependencies=protected)
    async def github_connect() -> dict[str, Any]:
        try:
            return dict(await run_sync(github_service().connection_start))
        except (ValueError, RuntimeError) as error:
            raise HTTPException(409, str(error)) from error

    @app.get("/v1/github/callback")
    async def github_callback(code: str, state: str) -> dict[str, Any]:
        # Browser callbacks authenticate with the signed, one-use OAuth state,
        # never by putting the owner bearer token in a URL.
        try:
            return dict(await github_service().connection_callback(code, state))
        except (ValueError, RuntimeError) as error:
            raise HTTPException(400, str(error)) from error

    @app.get("/v1/github/repositories", dependencies=protected)
    async def github_repositories() -> list[dict[str, Any]]:
        try:
            return list(await github_service().repositories())
        except (ValueError, RuntimeError) as error:
            raise HTTPException(409, str(error)) from error

    return app


__all__ = ["create_app"]
