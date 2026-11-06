"""Runtime persistence, admission and API contracts; no external credentials."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import event, text

from merlin.api import create_app
from merlin.api.limits import RequestLimits
from merlin.api.views import SnapshotCache
from merlin.config import Settings
from merlin.contracts import (
    Budget,
    Checkpoint,
    StaleLease,
    TaskSpec,
    TaskState,
    ToolCall,
    ToolResult,
    now,
)
from merlin.persistence import SQLTaskStore
from merlin.sandbox._remote import MANIFEST, validate_archive

TOKEN = "production-test-owner-credential-00000000"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def store() -> Iterator[SQLTaskStore]:
    value = SQLTaskStore("sqlite:///:memory:")
    value.create_schema()
    try:
        yield value
    finally:
        value.close()


def test_control_queries_do_not_select_native_history(store: SQLTaskStore) -> None:
    task = store.create_task(TaskSpec(prompt="explain"))
    lease = store.claim("worker")
    assert lease is not None
    store.update_task(lease, history=[{"role": "model", "parts": [{"text": "x" * 100000}]}])
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(store.engine, "before_cursor_execute", capture)
    try:
        assert store.task_control(task.id)["state"] == TaskState.RUNNING
        assert store.latest_event_sequence(task.id) > 0
        assert store.state_counts()["running"] == 1
        assert store.usage(task.id)["calls"] == 0
        store.heartbeat(lease)
        store.append_event(task.id, "test", {}, lease)
    finally:
        event.remove(store.engine, "before_cursor_execute", capture)
    assert statements
    assert all("tasks.history" not in statement for statement in statements)


def test_claim_and_context_metadata_do_not_select_native_history(store: SQLTaskStore) -> None:
    task = store.create_task(TaskSpec(prompt="metadata only"))
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(store.engine, "before_cursor_execute", capture)
    try:
        assert store.claim("worker") is not None
        assert store.get_task_metadata(task.id).history == []
    finally:
        event.remove(store.engine, "before_cursor_execute", capture)
    assert statements
    assert all("tasks.history" not in statement for statement in statements)


def test_upgrade_preserves_unknown_legacy_reservations(tmp_path: Path) -> None:
    from alembic import command
    from alembic.config import Config

    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    value = SQLTaskStore(url)
    try:
        with patch.dict(os.environ, {"MERLIN_DATABASE_URL": url}):
            command.upgrade(config, "0001")
            task = value.create_task(TaskSpec(prompt="legacy accounting"))
            with value.engine.begin() as connection:
                for identifier, actual in (("unknown-call", None), ("settled-call", 0.2)):
                    connection.execute(
                        text(
                            "INSERT INTO usage_reservations "
                            "(id,task_id,root_id,estimated_cost_usd,actual_cost_usd) "
                            "VALUES (:id,:task,:task,0.5,:actual)"
                        ),
                        {"id": identifier, "task": task.id, "actual": actual},
                    )
            command.upgrade(config, "head")
            with value.engine.connect() as connection:
                statuses = dict(
                    connection.execute(text("SELECT id,status FROM usage_reservations")).all()
                )
            assert statuses == {"unknown-call": "unknown", "settled-call": "settled"}
            lease = value.claim("worker")
            assert lease is not None
            with pytest.raises(ValueError, match="unknown usage"):
                value.release_usage(lease, "unknown-call", undispatched=True)
            value.worker_heartbeat("worker")
            assert value.readiness()["ready"] is True
    finally:
        value.close()


def test_session_summaries_have_constant_query_count(store: SQLTaskStore) -> None:
    for index in range(12):
        store.create_task(TaskSpec(prompt=f"session {index}", session_id=f"session-{index}"))
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(store.engine, "before_cursor_execute", capture)
    try:
        page = store.session_summaries()
    finally:
        event.remove(store.engine, "before_cursor_execute", capture)
    assert len(page.items) == 12
    assert len([statement for statement in statements if statement.startswith("SELECT")]) == 3


def test_keyset_pagination_is_stable_when_new_tasks_arrive(store: SQLTaskStore) -> None:
    for index in range(4):
        store.create_task(TaskSpec(prompt=f"message {index}", session_id="session"))
    first = store.session_tasks("session", limit=2)
    assert first.next_key is not None
    store.create_task(TaskSpec(prompt="new arrival", session_id="session"))
    second = store.session_tasks("session", limit=2, before=first.next_key)
    assert len(second.items) == 2
    assert not {value.id for value in first.items} & {value.id for value in second.items}


def test_bounded_events_preserve_full_order_and_make_large_event_progress(
    store: SQLTaskStore,
) -> None:
    task = store.create_task(TaskSpec(prompt="events"))
    for index in range(11):
        store.append_event(task.id, "test", {"index": index, "text": "x" * 5000})
    values = []
    cursor = 0
    while page := store.events(task.id, cursor, limit=3, max_bytes=1024):
        assert len(page) <= 3
        values.extend(page)
        cursor = page[-1].sequence
    assert [value.sequence for value in values] == list(range(1, 13))


@pytest.mark.parametrize("unfinished_mutation", [False, True])
def test_resolving_multiple_uncertain_tools_only_requeues_after_the_last_resolution(
    store: SQLTaskStore, unfinished_mutation: bool
) -> None:
    task = store.create_task(TaskSpec(prompt="inspect", repository="owner/repo"))
    lease = store.claim("worker")
    assert lease is not None
    calls = [
        ToolCall(name="read_file"),
        ToolCall(name="bash" if unfinished_mutation else "read_file"),
    ]
    for index, call in enumerate(calls):
        store.begin_tool(lease, call)
        if index == 0 or not unfinished_mutation:
            store.finish_tool(lease, ToolResult(call_id=call.id, ok=False, uncertain=True))
    store.update_task(lease, state=TaskState.PAUSED)

    first = store.resolve_tool(task.id, calls[0].id, "Inspected first operation")
    assert first.state == TaskState.PAUSED
    assert store.claim("unexpected-worker") is None
    with pytest.raises(ValueError, match="Uncertain"):
        store.resume(task.id)

    last = store.resolve_tool(task.id, calls[1].id, "Inspected second operation")
    assert last.state == TaskState.QUEUED
    assert store.claim("next-worker") is not None


def test_resolution_allows_an_unfinished_safely_replayable_read(store: SQLTaskStore) -> None:
    task = store.create_task(TaskSpec(prompt="inspect", repository="owner/repo"))
    lease = store.claim("worker")
    assert lease is not None
    interrupted_read = ToolCall(name="read_file")
    uncertain_write = ToolCall(name="write_file")
    store.begin_tool(lease, interrupted_read)
    store.begin_tool(lease, uncertain_write)
    store.finish_tool(lease, ToolResult(call_id=uncertain_write.id, ok=False, uncertain=True))
    store.update_task(lease, state=TaskState.PAUSED)

    resolved = store.resolve_tool(task.id, uncertain_write.id, "Inspected write effects")
    assert resolved.state == TaskState.QUEUED
    assert (
        next(
            item for item in store.invocations(task.id) if item.call.id == interrupted_read.id
        ).result
        is None
    )


def test_usage_refunds_only_proven_undispatched_requests(store: SQLTaskStore) -> None:
    task = store.create_task(TaskSpec(prompt="budget"))
    lease = store.claim("worker")
    assert lease is not None
    reservation = store.reserve_usage(lease, calls=1, cost_usd=0.5)
    store.release_usage(lease, reservation)
    store.release_usage(lease, reservation)
    assert store.usage(task.id)["calls"] == 0
    assert store.usage(task.id)["reserved_cost_usd"] == 0
    dispatched = store.reserve_usage(lease, calls=1, cost_usd=0.5)
    store.mark_usage_dispatched(lease, dispatched)
    with pytest.raises(ValueError):
        store.release_usage(lease, dispatched)
    assert store.usage(task.id)["reserved_cost_usd"] == 0.5
    store.release_usage(lease, dispatched, undispatched=True)
    assert store.usage(task.id)["calls"] == 0
    assert store.events(task.id)[-1].data["adapter_proved_no_request"] is True
    with pytest.raises(ValueError):
        store.settle_usage(lease, reservation, 1)


def test_operator_reconciliation_and_budget_changes_are_scoped_and_audited(
    store: SQLTaskStore,
) -> None:
    task = store.create_task(TaskSpec(prompt="budget"))
    lease = store.claim("worker")
    assert lease is not None
    reservation = store.reserve_usage(lease, calls=1, cost_usd=0.5)
    store.mark_usage_dispatched(lease, reservation)
    with pytest.raises(ValueError):
        store.reconcile_reservation(task.id, reservation, 0.2, "reviewed")
    store.update_task(lease, state=TaskState.PAUSED)
    other = store.create_task(TaskSpec(prompt="other"))
    with pytest.raises(KeyError):
        store.reconcile_reservation(other.id, reservation, 0.2, "wrong owner")
    store.reconcile_reservation(task.id, reservation, 0.2, "Provider usage inspected")
    assert store.usage(task.id)["cost_usd"] == 0.2
    assert store.usage(task.id)["reserved_cost_usd"] == 0
    changed = store.amend_budget(task.id, Budget(max_cost_usd=10), "Longer task approved")
    assert changed.spec.budget.max_cost_usd == 10
    assert changed.state == TaskState.PAUSED
    with pytest.raises(ValueError):
        store.reconcile_reservation(task.id, reservation, 0, "repeat")
    assert store.events(task.id)[-1].kind == "budget.amended"


def test_context_checkpoint_is_bounded_fenced_and_does_not_rewrite_history(
    store: SQLTaskStore,
) -> None:
    task = store.create_task(TaskSpec(prompt="context"))
    lease = store.claim("worker")
    assert lease is not None
    native = [{"role": "model", "parts": [{"thought_signature": "opaque"}]}]
    store.update_task(lease, history=native)
    sequence = store.latest_event_sequence(task.id)
    store.save_context_checkpoint(lease, {"cut": 1, "summary": "kept"}, sequence)
    assert store.context_checkpoint(task.id) == {"cut": 1, "summary": "kept"}
    assert store.get_task(task.id).history == native
    with pytest.raises(ValueError):
        store.save_context_checkpoint(lease, {"summary": "x" * 70000}, sequence)
    store.update_task(lease, state=TaskState.PAUSED)
    with pytest.raises(StaleLease):
        store.save_context_checkpoint(lease, {"summary": "stale"}, sequence)


def test_predecessors_are_scoped_and_do_not_load_history(store: SQLTaskStore) -> None:
    previous = store.create_task(TaskSpec(prompt="old constraint", session_id="conversation"))
    lease = store.claim("worker")
    assert lease is not None
    store.update_task(lease, state=TaskState.FAILED, history=[{"text": "old native data"}])
    store.create_task(TaskSpec(prompt="other conversation"))
    current = store.create_task(TaskSpec(prompt="continue", session_id="conversation"))
    values = store.predecessors(current)
    assert [value.id for value in values] == [previous.id]
    assert values[0].history == []
    assert values[0].state == TaskState.FAILED


def test_session_workspace_survives_many_intervening_chat_turns(store: SQLTaskStore) -> None:
    first = store.create_task(
        TaskSpec(prompt="edit", repository="owner/repo", session_id="session")
    )
    lease = store.claim("worker")
    assert lease is not None
    checkpoint = Checkpoint(task_id=first.id, artifact="snapshot.tar", sha256="a" * 64)
    store.save_checkpoint(lease, checkpoint)
    store.update_task(lease, state=TaskState.SUCCEEDED)
    for index in range(25):
        store.create_task(
            TaskSpec(prompt=f"chat {index}", repository="owner/repo", session_id="session")
        )
        lease = store.claim("worker")
        assert lease is not None
        store.update_task(lease, state=TaskState.SUCCEEDED)
    current = store.create_task(
        TaskSpec(prompt="continue", repository="owner/repo", session_id="session")
    )
    assert all(previous.checkpoint_id is None for previous in store.predecessors(current))
    assert store.latest_session_checkpoint(current) == checkpoint


def test_queue_cap_keeps_idempotent_retry_available() -> None:
    value = SQLTaskStore("sqlite:///:memory:", max_queued_tasks=1)
    value.create_schema()
    try:
        spec = TaskSpec(prompt="queued")
        first = value.create_task(spec, "same-request")
        assert value.create_task(spec, "same-request").id == first.id
        with pytest.raises(ValueError, match="Queued task limit"):
            value.create_task(TaskSpec(prompt="too many"))
    finally:
        value.close()


def test_queue_cap_rechecks_idempotency_after_waiting_for_admission() -> None:
    from sqlalchemy.orm import Session

    value = SQLTaskStore("sqlite:///:memory:", max_queued_tasks=1)
    value.create_schema()
    try:
        spec = TaskSpec(prompt="queued")
        first = value.create_task(spec, "same-request")
        original = Session.scalar
        missed = False

        def simulate_racing_first_read(session, statement, *args, **kwargs):
            nonlocal missed
            if not missed and "idempotency_key =" in str(statement):
                # Reproduce a request's stale pre-admission observation. The
                # already accepted row becomes visible after waiting for the
                # transaction-scoped admission lock in PostgreSQL.
                missed = True
                return None
            return original(session, statement, *args, **kwargs)

        with patch.object(Session, "scalar", simulate_racing_first_read):
            assert value.create_task(spec, "same-request").id == first.id
        assert missed
    finally:
        value.close()


def snapshot(content: bytes) -> bytes:
    manifest = {"version": 1, "files": {"app.py": hashlib.sha256(content).hexdigest()}}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in {"app.py": content, MANIFEST: json.dumps(manifest).encode()}.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


def test_snapshot_cache_reuses_verified_manifest_but_rejects_changed_archive(
    tmp_path: Path,
) -> None:
    data = snapshot(b"value = 42\n")
    path = tmp_path / "snapshot.tar"
    path.write_bytes(data)
    checksum = hashlib.sha256(data).hexdigest()
    cache = SnapshotCache()
    with patch("merlin.api.views.validate_archive", wraps=validate_archive) as validate:
        assert cache.read(tmp_path, path.name, checksum)[0] == data
        assert cache.read(tmp_path, path.name, checksum)[0] == data
        assert validate.call_count == 1
        path.write_bytes(snapshot(b"changed\n"))
        with pytest.raises(ValueError):
            cache.read(tmp_path, path.name, checksum)


async def direct_io(function, *args):
    return function(*args)


async def test_readiness_history_and_usage_api_keep_owner_controls(store: SQLTaskStore) -> None:
    app = create_app(
        Settings(_env_file=None, owner_token=SecretStr(TOKEN)), store, run_sync=direct_io
    )
    task = store.create_task(TaskSpec(prompt="task"))
    lease = store.claim("worker")
    assert lease is not None
    reservation = store.reserve_usage(lease, calls=1, cost_usd=0.5)
    store.mark_usage_dispatched(lease, reservation)
    store.update_task(lease, state=TaskState.PAUSED)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/readyz")).status_code == 503
        store.worker_heartbeat("worker")
        assert (await client.get("/readyz")).status_code == 200
        assert (await client.get(f"/v1/tasks/{task.id}/history")).status_code == 401
        assert (await client.get(f"/v1/tasks/{task.id}/history", headers=AUTH)).status_code == 200
        response = await client.post(
            f"/v1/tasks/{task.id}/reservations/{reservation}/resolve",
            json={"actual_cost_usd": 0.1, "note": "usage verified"},
            headers=AUTH,
        )
        assert response.status_code == 200
        response = await client.post(
            f"/v1/tasks/{task.id}/budget",
            json={"budget": {"max_cost_usd": 10}, "note": "approved more work"},
            headers=AUTH,
        )
        assert response.status_code == 200
        assert response.json()["state"] == "paused"


async def test_terminal_sse_drains_all_bounded_batches(store: SQLTaskStore) -> None:
    task = store.create_task(TaskSpec(prompt="events"))
    lease = store.claim("worker")
    assert lease is not None
    for index in range(600):
        store.append_event(task.id, "test", {"index": index}, lease)
    store.update_task(lease, state=TaskState.SUCCEEDED, result="complete")
    app = create_app(
        Settings(_env_file=None, owner_token=SecretStr(TOKEN)), store, run_sync=direct_io
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(f"/v1/tasks/{task.id}/events", headers=AUTH)
    values = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    assert [value["sequence"] for value in values] == list(
        range(1, store.latest_event_sequence(task.id) + 1)
    )


async def test_request_limit_counts_chunked_bytes_without_trusting_headers() -> None:
    from starlette.responses import JSONResponse

    async def downstream(scope, receive, send):
        await JSONResponse({"accepted": True})(scope, receive, send)

    app = RequestLimits(downstream, max_body_bytes=16)

    async def chunks():
        yield b"x" * 12
        yield b"y" * 12

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post("/", content=chunks())).status_code == 413


async def test_request_limit_preserves_many_tiny_body_chunks() -> None:
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    async def downstream(scope, receive, send):
        data = await Request(scope, receive).body()
        await JSONResponse({"body": data.decode()})(scope, receive, send)

    app = RequestLimits(downstream, max_body_bytes=1024)

    async def chunks():
        for _ in range(1000):
            yield b"x"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/", content=chunks())
    assert response.status_code == 200
    assert response.json()["body"] == "x" * 1000


async def test_stream_admission_releases_slot_after_disconnect() -> None:
    import asyncio

    from starlette.responses import JSONResponse

    admitted = asyncio.Event()
    disconnected = asyncio.Event()

    async def downstream(scope, receive, send):
        admitted.set()
        await disconnected.wait()
        await JSONResponse({"complete": True})(scope, receive, send)

    app = RequestLimits(downstream, max_streams=1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = asyncio.create_task(client.get("/v1/tasks/task/events"))
        await asyncio.wait_for(admitted.wait(), timeout=2)
        rejected = await client.get("/v1/tasks/task/events")
        assert rejected.status_code == 429
        assert rejected.headers["Retry-After"] == "2"
        disconnected.set()
        assert (await first).status_code == 200
        assert (await client.get("/v1/tasks/task/events")).status_code == 200


def test_stale_worker_readiness_expires(store: SQLTaskStore) -> None:
    from merlin.persistence.models import WorkerHeartbeatRow

    store.worker_heartbeat("worker")
    assert store.readiness()["ready"] is True
    with store._write() as session:
        row = session.get(WorkerHeartbeatRow, "worker")
        assert row is not None
        row.updated_at = now() - timedelta(minutes=2)
    assert store.readiness()["ready"] is False
