"""Synchronous SQLAlchemy store with lease-fenced mutations.

SQLite uses BEGIN IMMEDIATE for deterministic development behavior. PostgreSQL
uses row locks and per-session advisory locks; no process-local lock is relied on
for production correctness.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, create_engine, event, func, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, load_only
from sqlalchemy.pool import StaticPool

from merlin.contracts import (
    Approval,
    Budget,
    BudgetExceeded,
    Checkpoint,
    Event,
    Invocation,
    Lease,
    StaleLease,
    Task,
    TaskSpec,
    TaskState,
    ToolCall,
    ToolResult,
    new_id,
    now,
)
from merlin.persistence.models import (
    ApprovalRow,
    Base,
    CheckpointRow,
    ContextRow,
    EventRow,
    InvocationRow,
    ReservationRow,
    TaskRow,
    UsageRow,
    WorkerHeartbeatRow,
)
from merlin.presentation import SessionPage, SessionSummary, TaskPage, TaskSummary

TERMINAL = {TaskState.SUCCEEDED.value, TaskState.FAILED.value, TaskState.CANCELLED.value}
ACTIVE = {
    TaskState.RUNNING.value,
    TaskState.WAITING_FOR_APPROVAL.value,
    TaskState.WAITING_FOR_CHILDREN.value,
    TaskState.PAUSED.value,
}
EDITABLE = {"state", "history", "sandbox_id", "checkpoint_id", "error", "result"}


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


class SQLTaskStore:
    def __init__(
        self,
        database_url: str,
        max_running_roots: int = 1,
        max_running_children: int = 2,
        max_queued_tasks: int = 100,
    ) -> None:
        if max_running_roots < 1 or max_running_children < 1 or max_queued_tasks < 1:
            raise ValueError("Concurrency limits must be positive")
        self.max_running_roots = max_running_roots
        self.max_running_children = max_running_children
        self.max_queued_tasks = max_queued_tasks
        if database_url.startswith("postgresql://"):
            database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
        options: dict[str, Any] = {"pool_pre_ping": True}
        if database_url.startswith("sqlite"):
            options["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if ":memory:" in database_url or database_url in {"sqlite://", "sqlite+pysqlite://"}:
                options["poolclass"] = StaticPool
        self.engine: Engine = create_engine(database_url, **options)
        self.sqlite = self.engine.dialect.name == "sqlite"
        self._sqlite_lock = threading.RLock()
        if self.sqlite:
            event.listen(self.engine, "connect", self._configure_sqlite)

    @staticmethod
    def _configure_sqlite(connection: Any, _: Any) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    def create_schema(self) -> None:
        """Explicit development helper. Deployments must use Alembic migrations."""
        Base.metadata.create_all(self.engine)

    def close(self) -> None:
        self.engine.dispose()

    @contextmanager
    def _write(self) -> Iterator[Session]:
        # The in-memory SQLite pool shares one connection. Serialize local
        # operations, while database transactions serialize separate instances.
        if self.sqlite:
            self._sqlite_lock.acquire()
        with Session(self.engine, expire_on_commit=False) as session:
            try:
                if self.sqlite:
                    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise
            finally:
                if self.sqlite:
                    self._sqlite_lock.release()

    @contextmanager
    def _read(self) -> Iterator[Session]:
        if self.sqlite:
            self._sqlite_lock.acquire()
        try:
            with Session(self.engine) as session:
                yield session
        finally:
            if self.sqlite:
                self._sqlite_lock.release()

    @staticmethod
    def _row(
        session: Session, task_id: str, lock: bool = False, *, include_history: bool = True
    ) -> TaskRow:
        query = select(TaskRow).where(TaskRow.id == task_id)
        if not include_history:
            query = query.options(SQLTaskStore._metadata_columns())
        row = session.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise KeyError(f"Unknown task: {task_id}")
        return row

    def _leased(self, session: Session, lease: Lease, new_work: bool = False) -> TaskRow:
        row = self._row(session, lease.task_id, lock=True, include_history=False)
        if (
            row.state != TaskState.RUNNING.value
            or row.worker_id != lease.worker_id
            or row.generation != lease.generation
            or row.lease_until is None
            or utc(row.lease_until) <= now()
        ):
            raise StaleLease(f"Lease no longer owns task {lease.task_id}")
        if new_work and row.cancel_requested:
            raise StaleLease("Cancellation prevents starting new work")
        return row

    @staticmethod
    def _task(row: TaskRow, *, include_history: bool = True) -> Task:
        return Task(
            id=row.id,
            spec=TaskSpec.model_validate(row.spec),
            root_id=row.root_id,
            state=TaskState(row.state),
            generation=row.generation,
            worker_id=row.worker_id,
            lease_until=utc(row.lease_until) if row.lease_until else None,
            created_at=utc(row.created_at),
            updated_at=utc(row.updated_at),
            history=row.history if include_history else [],
            sandbox_id=row.sandbox_id,
            checkpoint_id=row.checkpoint_id,
            error=row.error,
            result=row.result,
            cancel_requested=row.cancel_requested,
        )

    @staticmethod
    def _event(session: Session, row: TaskRow, kind: str, data: dict[str, Any]) -> Event:
        row.event_sequence += 1
        event_value = Event(task_id=row.id, sequence=row.event_sequence, kind=kind, data=data)
        session.add(EventRow(**event_value.model_dump()))
        return event_value

    def create_task(
        self,
        spec: TaskSpec,
        idempotency_key: str | None = None,
        *,
        parent_lease: Lease | None = None,
    ) -> Task:
        request_digest = digest(spec.model_dump(mode="json"))
        if idempotency_key is not None and not 1 <= len(idempotency_key) <= 200:
            raise ValueError("Idempotency key must contain 1 to 200 characters")
        try:
            with self._write() as session:
                if parent_lease is not None:
                    if spec.parent_id != parent_lease.task_id:
                        raise StaleLease("Child parent does not match its creating lease")
                    self._leased(session, parent_lease, new_work=True)
                if idempotency_key:
                    existing = session.scalar(
                        select(TaskRow).where(TaskRow.idempotency_key == idempotency_key)
                    )
                    if existing:
                        if existing.request_digest != request_digest:
                            raise ValueError("Idempotency key reused with different task content")
                        return self._task(existing)
                # A transaction-scoped admission lock makes the cap work across
                # independent API processes, while idempotent retries still pass.
                if not self.sqlite:
                    session.execute(select(func.pg_advisory_xact_lock(-701923418266)))
                # Requests sharing an idempotency key can both pass the first
                # lookup before one waits for admission. Recheck after the lock
                # so retrying accepted work is never rejected by the queue cap.
                if idempotency_key:
                    existing = session.scalar(
                        select(TaskRow).where(TaskRow.idempotency_key == idempotency_key)
                    )
                    if existing is not None:
                        if existing.request_digest != request_digest:
                            raise ValueError("Idempotency key reused with different task content")
                        return self._task(existing)
                queued = (
                    session.scalar(
                        select(func.count())
                        .select_from(TaskRow)
                        .where(TaskRow.state == TaskState.QUEUED.value)
                    )
                    or 0
                )
                if queued >= self.max_queued_tasks:
                    raise ValueError("Queued task limit reached; wait or cancel queued work")
                if spec.parent_id is None:
                    # Repository identity belongs to the session transaction, not
                    # an API preflight that concurrent submissions can both pass.
                    if not self.sqlite:
                        lock_key = int.from_bytes(
                            hashlib.sha256(spec.session_id.encode()).digest()[:8],
                            "big",
                            signed=True,
                        )
                        session.execute(select(func.pg_advisory_xact_lock(lock_key)))
                    existing_session = session.execute(
                        select(TaskRow.spec["repository"].as_string())
                        .where(TaskRow.session_id == spec.session_id, TaskRow.parent_id.is_(None))
                        .limit(1)
                    ).first()
                    if existing_session is not None and existing_session[0] != spec.repository:
                        raise ValueError("A session cannot switch repositories")
                task = Task(spec=spec)
                task.root_id = task.id
                if spec.parent_id:
                    parent = self._row(session, spec.parent_id, lock=True)
                    parent_spec = TaskSpec.model_validate(parent.spec)
                    if parent.parent_id is not None:
                        raise ValueError("Only one delegation level is supported")
                    if parent.cancel_requested or parent.state not in {
                        TaskState.RUNNING.value,
                        TaskState.WAITING_FOR_CHILDREN.value,
                    }:
                        raise ValueError("Parent is not accepting child work")
                    if spec.repository != parent_spec.repository:
                        raise ValueError("Child must use its parent's repository")
                    if not set(spec.permissions).issubset(parent_spec.permissions):
                        raise ValueError("Child cannot expand permissions")
                    if not set(spec.allowed_domains).issubset(parent_spec.allowed_domains):
                        raise ValueError("Child cannot expand network access")
                    task.root_id = parent.root_id
                if spec.base_checkpoint_id:
                    checkpoint = session.get(CheckpointRow, spec.base_checkpoint_id)
                    if checkpoint is None:
                        raise ValueError("Unknown base checkpoint")
                    owner = self._row(session, checkpoint.task_id)
                    same_session = owner.session_id == spec.session_id
                    same_root = bool(spec.parent_id and owner.root_id == task.root_id)
                    if (
                        not (same_session or same_root)
                        or owner.spec["repository"] != spec.repository
                    ):
                        raise ValueError("Base checkpoint is outside this task's workspace")
                row = TaskRow(
                    id=task.id,
                    session_id=spec.session_id,
                    root_id=task.root_id,
                    parent_id=spec.parent_id,
                    state=task.state.value,
                    spec=spec.model_dump(mode="json"),
                    generation=0,
                    worker_id=None,
                    lease_until=None,
                    created_at=task.created_at,
                    updated_at=task.updated_at,
                    history=[],
                    sandbox_id=None,
                    checkpoint_id=spec.base_checkpoint_id,
                    error=None,
                    result=None,
                    cancel_requested=False,
                    event_sequence=0,
                    idempotency_key=idempotency_key,
                    request_digest=request_digest,
                )
                session.add(row)
                session.flush()
                if not spec.parent_id:
                    session.add(
                        UsageRow(
                            root_id=row.id,
                            calls=0,
                            sandbox_seconds=0,
                            cost_usd=0,
                            reserved_cost_usd=0,
                        )
                    )
                self._event(session, row, "task.created", {"state": row.state})
                return self._task(row)
        except IntegrityError:
            # A concurrent request may have committed the same idempotency key.
            if not idempotency_key:
                raise
            with self._read() as session:
                existing = session.scalar(
                    select(TaskRow).where(TaskRow.idempotency_key == idempotency_key)
                )
                if existing is None:
                    raise
                if existing.request_digest != request_digest:
                    raise ValueError("Idempotency key reused with different task content") from None
                return self._task(existing)

    def get_task(self, task_id: str) -> Task:
        with self._read() as session:
            return self._task(self._row(session, task_id))

    def get_task_metadata(self, task_id: str) -> Task:
        """Return task identity, policy and lifecycle without native model history."""
        with self._read() as session:
            return self._task(
                self._row(session, task_id, include_history=False), include_history=False
            )

    def list_tasks(self, session_id: str | None = None) -> list[Task]:
        with self._read() as session:
            query = select(TaskRow).order_by(TaskRow.created_at, TaskRow.id)
            if session_id:
                query = query.where(TaskRow.session_id == session_id)
            return [self._task(row) for row in session.scalars(query)]

    @staticmethod
    def _metadata_columns() -> Any:
        return load_only(
            *(
                getattr(TaskRow, field)
                for field in TaskRow.__table__.columns.keys()
                if field != "history"
            )
        )

    def task_control(self, task_id: str) -> dict[str, Any]:
        """Read control-plane state without loading native model history."""
        with self._read() as session:
            row = session.execute(
                select(
                    TaskRow.id,
                    TaskRow.root_id,
                    TaskRow.parent_id,
                    TaskRow.state,
                    TaskRow.cancel_requested,
                    TaskRow.generation,
                    TaskRow.worker_id,
                    TaskRow.lease_until,
                    TaskRow.event_sequence,
                    TaskRow.checkpoint_id,
                    TaskRow.updated_at,
                ).where(TaskRow.id == task_id)
            ).first()
            if row is None:
                raise KeyError("Task not found")
            result = dict(row._mapping)
            result["state"] = TaskState(result["state"])
            for key in ("lease_until", "updated_at"):
                if result[key] is not None:
                    result[key] = utc(result[key])
            return result

    def latest_event_sequence(self, task_id: str) -> int:
        with self._read() as session:
            value = session.scalar(select(TaskRow.event_sequence).where(TaskRow.id == task_id))
            if value is None:
                raise KeyError("Task not found")
            return int(value)

    def state_counts(self) -> dict[str, int]:
        with self._read() as session:
            counts = {state.value: 0 for state in TaskState}
            counts.update(
                dict(
                    session.execute(
                        select(TaskRow.state, func.count()).group_by(TaskRow.state)
                    ).all()
                )
            )
            return counts

    def children(self, task_id: str) -> list[Task]:
        with self._read() as session:
            if session.scalar(select(TaskRow.id).where(TaskRow.id == task_id)) is None:
                raise KeyError("Task not found")
            rows = session.scalars(
                select(TaskRow)
                .options(self._metadata_columns())
                .where(TaskRow.parent_id == task_id)
                .order_by(TaskRow.created_at, TaskRow.id)
            )
            return [self._task(row, include_history=False) for row in rows]

    def predecessors(
        self, task: Task, *, limit: int = 20, include_history: bool = False
    ) -> list[Task]:
        if not 1 <= limit <= 100:
            raise ValueError("Predecessor limit must be between 1 and 100")
        with self._read() as session:
            query = (
                select(TaskRow)
                .where(
                    TaskRow.session_id == task.spec.session_id,
                    TaskRow.parent_id.is_(None),
                    TaskRow.id != task.id,
                    TaskRow.created_at < task.created_at,
                    TaskRow.spec["repository"].as_string() == task.spec.repository
                    if task.spec.repository is not None
                    else TaskRow.spec["repository"].as_string().is_(None),
                )
                .order_by(TaskRow.created_at.desc(), TaskRow.id.desc())
                .limit(limit)
            )
            if not include_history:
                query = query.options(self._metadata_columns())
            return list(
                reversed(
                    [
                        self._task(row, include_history=include_history)
                        for row in session.scalars(query)
                    ]
                )
            )

    def context_predecessors(self, task_id: str, limit: int = 20) -> list[Task]:
        with self._read() as session:
            row = session.scalar(
                select(TaskRow).options(self._metadata_columns()).where(TaskRow.id == task_id)
            )
            if row is None:
                raise KeyError("Task not found")
            task = self._task(row, include_history=False)
        return self.predecessors(task, limit=limit)

    def latest_session_checkpoint(self, task: Task) -> Checkpoint | None:
        """Find the last completed workspace without scanning chat-only turns."""
        with self._read() as session:
            row = session.scalar(
                select(CheckpointRow)
                .join(TaskRow, TaskRow.checkpoint_id == CheckpointRow.id)
                .where(
                    TaskRow.session_id == task.spec.session_id,
                    TaskRow.parent_id.is_(None),
                    TaskRow.state == TaskState.SUCCEEDED.value,
                    TaskRow.created_at < task.created_at,
                    TaskRow.spec["repository"].as_string() == task.spec.repository
                    if task.spec.repository is not None
                    else TaskRow.spec["repository"].as_string().is_(None),
                )
                .order_by(TaskRow.created_at.desc(), TaskRow.id.desc())
                .limit(1)
            )
            if row is None:
                return None
            return Checkpoint(
                id=row.id,
                task_id=row.task_id,
                artifact=row.artifact,
                sha256=row.sha256,
                base_sha=row.base_sha,
                created_at=utc(row.created_at),
            )

    def history_page(
        self, task_id: str, cursor: int = 0, limit: int = 20, max_chars: int = 64000
    ) -> dict[str, Any]:
        if cursor < 0 or not 1 <= limit <= 100 or not 1000 <= max_chars <= 128000:
            raise ValueError("Invalid history page bounds")
        with self._read() as session:
            history = session.scalar(select(TaskRow.history).where(TaskRow.id == task_id))
            if history is None:
                raise KeyError("Task not found")
            selected: list[dict[str, Any]] = []
            consumed = cursor
            size = 0
            for entry in history[cursor : cursor + limit]:
                length = len(json.dumps(entry, default=str))
                if size + length > max_chars:
                    if selected:
                        break
                    # Native signed entries cannot be clipped and replayed. Make
                    # an oversized entry explicit and advance the browsing cursor.
                    selected.append({"omitted": True, "entry": consumed, "characters": length})
                else:
                    selected.append(entry)
                consumed += 1
                size += length
            return {
                "items": selected,
                "cursor": cursor,
                "next_cursor": consumed if consumed < len(history) else None,
                "total_entries": len(history),
            }

    def context_checkpoint(self, task_id: str) -> dict[str, Any] | None:
        with self._read() as session:
            row = session.get(ContextRow, task_id)
            return json.loads(json.dumps(row.payload)) if row is not None else None

    def save_context_checkpoint(
        self, lease: Lease, payload: dict[str, Any], history_sequence: int
    ) -> None:
        encoded = json.dumps(payload, allow_nan=False)
        if len(encoded.encode("utf-8")) > 65536 or history_sequence < 0:
            raise ValueError("Context checkpoint exceeds bounds")
        with self._write() as session:
            task = self._leased(session, lease)
            if history_sequence > task.event_sequence:
                raise ValueError("Context checkpoint references future events")
            row = session.get(ContextRow, task.id)
            if row is None:
                session.add(
                    ContextRow(
                        task_id=task.id,
                        payload=json.loads(encoded),
                        history_sequence=history_sequence,
                        updated_at=now(),
                    )
                )
            elif history_sequence >= row.history_sequence:
                row.payload, row.history_sequence, row.updated_at = (
                    json.loads(encoded),
                    history_sequence,
                    now(),
                )

    def worker_heartbeat(self, worker_id: str) -> None:
        if not worker_id or len(worker_id) > 200:
            raise ValueError("Invalid worker identity")
        with self._write() as session:
            row = session.get(WorkerHeartbeatRow, worker_id)
            if row is None:
                session.add(WorkerHeartbeatRow(worker_id=worker_id, updated_at=now()))
            else:
                row.updated_at = now()

    def readiness(self, worker_max_age_seconds: int = 30) -> dict[str, Any]:
        with self._read() as session:
            session.execute(select(1))
            # Reading these columns fails closed until the current migration is applied.
            session.execute(select(ReservationRow.status).limit(1))
            session.execute(select(ContextRow.task_id).limit(1))
            workers = (
                session.scalar(
                    select(func.count())
                    .select_from(WorkerHeartbeatRow)
                    .where(
                        WorkerHeartbeatRow.updated_at
                        >= now() - timedelta(seconds=worker_max_age_seconds)
                    )
                )
                or 0
            )
            return {
                "database": "ready",
                "schema": "ready",
                "worker_ready": bool(workers),
                "worker_count": workers,
                "ready": bool(workers),
            }

    @staticmethod
    def _summary_columns() -> Any:
        return load_only(
            TaskRow.id,
            TaskRow.spec,
            TaskRow.session_id,
            TaskRow.root_id,
            TaskRow.parent_id,
            TaskRow.state,
            TaskRow.generation,
            TaskRow.checkpoint_id,
            TaskRow.result,
            TaskRow.error,
            TaskRow.cancel_requested,
            TaskRow.created_at,
            TaskRow.updated_at,
        )

    @staticmethod
    def _summary(row: TaskRow) -> TaskSummary:
        return TaskSummary(
            id=row.id,
            session_id=row.session_id,
            repository=row.spec["repository"],
            prompt=row.spec["prompt"],
            model=row.spec["model"],
            state=TaskState(row.state),
            root_id=row.root_id,
            parent_id=row.parent_id,
            generation=row.generation,
            checkpoint_id=row.checkpoint_id,
            result=row.result,
            error=row.error,
            cancel_requested=row.cancel_requested,
            created_at=utc(row.created_at),
            updated_at=utc(row.updated_at),
            budget=row.spec.get("budget", {}),
            allowed_domains=row.spec.get("allowed_domains", []),
        )

    def task_summary(self, task_id: str) -> TaskSummary:
        with self._read() as session:
            row = session.scalar(
                select(TaskRow).options(self._summary_columns()).where(TaskRow.id == task_id)
            )
            if row is None:
                raise KeyError("Task not found")
            return self._summary(row)

    def session_tasks(
        self, session_id: str, cursor: int = 0, limit: int = 20, *, before: str | None = None
    ) -> TaskPage:
        if cursor < 0 or not 1 <= limit <= 100:
            raise ValueError("Invalid task page bounds")
        with self._read() as session:
            query = (
                select(TaskRow)
                .options(self._summary_columns())
                .where(TaskRow.session_id == session_id, TaskRow.parent_id.is_(None))
            )
            if before is not None:
                anchor = session.execute(
                    select(TaskRow.created_at, TaskRow.id).where(
                        TaskRow.id == before,
                        TaskRow.session_id == session_id,
                        TaskRow.parent_id.is_(None),
                    )
                ).first()
                if anchor is None:
                    raise ValueError("Invalid task page anchor")
                query = query.where(
                    or_(
                        TaskRow.created_at < anchor.created_at,
                        and_(TaskRow.created_at == anchor.created_at, TaskRow.id < anchor.id),
                    )
                )
            rows = list(
                session.scalars(
                    query.order_by(TaskRow.created_at.desc(), TaskRow.id.desc())
                    .offset(cursor if before is None else 0)
                    .limit(limit + 1)
                )
            )
            return TaskPage(
                items=[self._summary(row) for row in rows[:limit]],
                next_cursor=cursor + limit if len(rows) > limit else None,
                next_key=rows[limit - 1].id if len(rows) > limit else None,
            )

    def session_summaries(
        self, repository: str | None = None, cursor: int = 0, limit: int = 50
    ) -> SessionPage:
        if cursor < 0 or not 1 <= limit <= 100:
            raise ValueError("Invalid session page bounds")
        with self._read() as session:
            query = select(
                TaskRow.session_id,
                func.max(TaskRow.updated_at).label("updated"),
                func.count(TaskRow.id).label("count"),
            ).where(TaskRow.parent_id.is_(None))
            if repository:
                query = query.where(TaskRow.spec["repository"].as_string() == repository)
            query = (
                query.group_by(TaskRow.session_id)
                .order_by(func.max(TaskRow.updated_at).desc(), TaskRow.session_id.desc())
                .offset(cursor)
                .limit(limit + 1)
            )
            groups = list(session.execute(query))
            identifiers = [group.session_id for group in groups[:limit]]
            if not identifiers:
                return SessionPage(items=[])
            ranks = (
                select(
                    TaskRow.id,
                    TaskRow.session_id,
                    func.row_number()
                    .over(
                        partition_by=TaskRow.session_id, order_by=(TaskRow.created_at, TaskRow.id)
                    )
                    .label("first_rank"),
                    func.row_number()
                    .over(
                        partition_by=TaskRow.session_id,
                        order_by=(TaskRow.created_at.desc(), TaskRow.id.desc()),
                    )
                    .label("last_rank"),
                )
                .where(TaskRow.session_id.in_(identifiers), TaskRow.parent_id.is_(None))
                .subquery()
            )
            boundary_rows = list(
                session.execute(
                    select(ranks).where(or_(ranks.c.first_rank == 1, ranks.c.last_rank == 1))
                )
            )
            records = {
                row.id: row
                for row in session.scalars(
                    select(TaskRow)
                    .options(self._summary_columns())
                    .where(TaskRow.id.in_([row.id for row in boundary_rows]))
                )
            }
            first_ids = {row.session_id: row.id for row in boundary_rows if row.first_rank == 1}
            last_ids = {row.session_id: row.id for row in boundary_rows if row.last_rank == 1}
            items = []
            for group in groups[:limit]:
                first = records[first_ids[group.session_id]]
                latest = records[last_ids[group.session_id]]
                items.append(
                    SessionSummary(
                        session_id=group.session_id,
                        repository=first.spec["repository"],
                        title=first.spec["prompt"][:100],
                        latest_task_id=latest.id,
                        state=TaskState(latest.state),
                        updated_at=utc(group.updated),
                        task_count=group._mapping["count"],
                    )
                )
            return SessionPage(
                items=items, next_cursor=cursor + limit if len(groups) > limit else None
            )

    def get_approval(self, approval_id: str) -> Approval:
        with self._read() as session:
            row = session.get(ApprovalRow, approval_id)
            if row is None:
                raise KeyError("Approval not found")
            return self._approval(row)

    def approval_summaries(self, task_id: str) -> list[dict[str, Any]]:
        with self._read() as session:
            if session.scalar(select(TaskRow.id).where(TaskRow.id == task_id)) is None:
                raise KeyError("Task not found")
            rows = session.execute(
                select(
                    ApprovalRow.id,
                    ApprovalRow.task_id,
                    ApprovalRow.status,
                    ApprovalRow.digest,
                    ApprovalRow.call["name"].as_string().label("name"),
                    ApprovalRow.created_at,
                )
                .where(ApprovalRow.task_id == task_id)
                .order_by(ApprovalRow.created_at, ApprovalRow.id)
            )
            return [dict(row._mapping) for row in rows]

    def claim(self, worker_id: str, lease_seconds: int = 60) -> Lease | None:
        if not worker_id or lease_seconds < 1:
            raise ValueError("Worker ID and positive lease duration are required")
        with self._write() as session:
            timestamp = now()
            if not self.sqlite and not session.scalar(
                select(func.pg_try_advisory_xact_lock(-701923418265))
            ):
                return None
            query = (
                select(TaskRow)
                .options(self._metadata_columns())
                .where(
                    or_(
                        TaskRow.state == TaskState.QUEUED.value,
                        and_(
                            TaskRow.state == TaskState.RUNNING.value,
                            TaskRow.lease_until < timestamp,
                        ),
                    )
                )
                .order_by(TaskRow.created_at, TaskRow.id)
                .with_for_update(skip_locked=True)
            )
            for row in session.scalars(query):
                if row.parent_id is None:
                    running_roots = (
                        session.scalar(
                            select(func.count())
                            .select_from(TaskRow)
                            .where(
                                TaskRow.parent_id.is_(None),
                                TaskRow.state == TaskState.RUNNING.value,
                                TaskRow.lease_until > timestamp,
                                TaskRow.id != row.id,
                            )
                        )
                        or 0
                    )
                    if running_roots >= self.max_running_roots:
                        continue
                    # A row lock alone cannot serialize two different queued tasks
                    # in one session. Transaction-scoped advisory locks close that race.
                    if not self.sqlite:
                        lock_key = int.from_bytes(
                            hashlib.sha256(row.session_id.encode()).digest()[:8],
                            "big",
                            signed=True,
                        )
                        if not session.scalar(select(func.pg_try_advisory_xact_lock(lock_key))):
                            continue
                    blocker = session.scalar(
                        select(TaskRow.id)
                        .where(
                            TaskRow.session_id == row.session_id,
                            TaskRow.parent_id.is_(None),
                            TaskRow.id != row.id,
                            TaskRow.state.in_(ACTIVE),
                        )
                        .limit(1)
                    )
                    if blocker:
                        continue
                else:
                    parent = self._row(session, row.parent_id)
                    if parent.cancel_requested or parent.state in TERMINAL:
                        row.state = TaskState.CANCELLED.value
                        row.cancel_requested = True
                        self._event(session, row, "task.cancelled", {"reason": "parent stopped"})
                        continue
                    running_children = (
                        session.scalar(
                            select(func.count())
                            .select_from(TaskRow)
                            .where(
                                TaskRow.root_id == row.root_id,
                                TaskRow.parent_id.is_not(None),
                                TaskRow.state == TaskState.RUNNING.value,
                                TaskRow.lease_until > timestamp,
                                TaskRow.id != row.id,
                            )
                        )
                        or 0
                    )
                    if running_children >= self.max_running_children:
                        continue
                reclaimed = row.state == TaskState.RUNNING.value
                row.state = TaskState.RUNNING.value
                row.generation += 1
                row.worker_id = worker_id
                row.lease_until = timestamp + timedelta(seconds=lease_seconds)
                row.updated_at = timestamp
                self._event(
                    session,
                    row,
                    "task.claimed",
                    {"worker_id": worker_id, "generation": row.generation, "reclaimed": reclaimed},
                )
                return Lease(task_id=row.id, worker_id=worker_id, generation=row.generation)
        return None

    def heartbeat(self, lease: Lease, lease_seconds: int = 60) -> None:
        if lease_seconds < 1:
            raise ValueError("Lease duration must be positive")
        with self._write() as session:
            row = self._leased(session, lease)
            row.lease_until = now() + timedelta(seconds=lease_seconds)
            row.updated_at = now()

    def update_task(self, lease: Lease, **changes: Any) -> Task:
        unknown = set(changes) - EDITABLE
        if unknown:
            raise ValueError(f"Immutable or unknown task fields: {', '.join(sorted(unknown))}")
        with self._write() as session:
            row = self._leased(session, lease)
            candidate = self._task(row).model_dump()
            candidate.update(changes)
            value = Task.model_validate(candidate)
            if value.cancel_requested and value.state not in {
                TaskState.RUNNING,
                TaskState.CANCELLED,
            }:
                raise StaleLease("Cancelled task cannot change to another lifecycle state")
            for name in changes:
                setattr(row, name, value.state.value if name == "state" else getattr(value, name))
            row.updated_at = now()
            if row.state != TaskState.RUNNING.value:
                row.worker_id = None
                row.lease_until = None
            if row.parent_id is None and row.state in TERMINAL:
                # Stop already-running children too; their heartbeat loop performs
                # cleanup while retaining a valid lease for that final transition.
                children = session.scalars(
                    select(TaskRow)
                    .where(
                        TaskRow.root_id == row.id,
                        TaskRow.parent_id.is_not(None),
                        TaskRow.state.not_in(TERMINAL),
                    )
                    .with_for_update()
                ).all()
                for child in children:
                    child.cancel_requested = True
                    if child.state != TaskState.RUNNING.value:
                        child.state = TaskState.CANCELLED.value
                        child.worker_id = None
                        child.lease_until = None
                    child.updated_at = now()
                    self._event(
                        session, child, "task.cancel_requested", {"reason": "parent stopped"}
                    )
            self._event(
                session, row, "task.updated", {"fields": sorted(changes), "state": row.state}
            )
            return self._task(row)

    def append_event(
        self,
        task_id: str,
        kind: str,
        data: dict[str, Any],
        lease: Lease | None = None,
    ) -> Event:
        if lease and lease.task_id != task_id:
            raise StaleLease("Event task does not match lease")
        with self._write() as session:
            row = self._leased(session, lease) if lease else self._row(session, task_id, lock=True)
            return self._event(session, row, kind, data)

    def events(
        self,
        task_id: str,
        after: int = 0,
        *,
        limit: int | None = None,
        max_bytes: int | None = None,
    ) -> list[Event]:
        if after < 0 or limit is not None and not 1 <= limit <= 10000:
            raise ValueError("Invalid event page bounds")
        if max_bytes is not None and not 1024 <= max_bytes <= 32 * 1024 * 1024:
            raise ValueError("Invalid event byte limit")
        with self._read() as session:
            if session.scalar(select(TaskRow.id).where(TaskRow.id == task_id)) is None:
                raise KeyError("Task not found")
            query = (
                select(EventRow)
                .where(
                    EventRow.task_id == task_id,
                    EventRow.sequence > after,
                )
                .order_by(EventRow.sequence)
            )
            if limit is not None:
                query = query.limit(limit)
            values: list[Event] = []
            size = 0
            for row in session.scalars(query):
                value = Event(
                    task_id=row.task_id,
                    sequence=row.sequence,
                    kind=row.kind,
                    data=row.data,
                    created_at=utc(row.created_at),
                )
                length = len(value.model_dump_json().encode("utf-8"))
                # Always return at least one complete event so a legacy large
                # event cannot trap a replay cursor permanently.
                if values and max_bytes is not None and size + length > max_bytes:
                    break
                values.append(value)
                size += length
            return values

    @staticmethod
    def _invocation(row: InvocationRow) -> Invocation:
        return Invocation(
            task_id=row.task_id,
            call=ToolCall.model_validate(row.call),
            state=row.state,
            result=ToolResult.model_validate(row.result) if row.result is not None else None,
        )

    def _begin(self, session: Session, task: TaskRow, call: ToolCall) -> Invocation:
        fingerprint = digest(call.model_dump(mode="json"))
        existing = session.get(InvocationRow, (task.id, call.id))
        if existing:
            if existing.digest != fingerprint:
                raise ValueError("Tool call ID reused with different arguments")
            return self._invocation(existing)
        row = InvocationRow(
            task_id=task.id,
            call_id=call.id,
            call=call.model_dump(mode="json"),
            digest=fingerprint,
            state="prepared",
            result=None,
            created_at=now(),
        )
        session.add(row)
        self._event(session, task, "tool.prepared", {"call_id": call.id, "name": call.name})
        return self._invocation(row)

    def begin_tool(self, lease: Lease, call: ToolCall) -> Invocation:
        with self._write() as session:
            return self._begin(session, self._leased(session, lease, new_work=True), call)

    def finish_tool(
        self,
        lease: Lease,
        result: ToolResult,
        *,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        with self._write() as session:
            task = self._leased(session, lease)
            row = session.get(InvocationRow, (task.id, result.call_id))
            if row is None:
                raise KeyError(f"Unknown invocation: {result.call_id}")
            encoded = result.model_dump(mode="json")
            if row.result is not None:
                if row.result != encoded:
                    raise ValueError("Completed invocation result is immutable")
                return
            if checkpoint is not None:
                self._save_checkpoint(session, task, checkpoint)
            row.result = encoded
            row.state = "uncertain" if result.uncertain else "succeeded" if result.ok else "failed"
            self._event(
                session,
                task,
                "tool.finished",
                {
                    "call_id": result.call_id,
                    "ok": result.ok,
                    "uncertain": result.uncertain,
                    "result": encoded,
                },
            )

    def invocations(self, task_id: str) -> list[Invocation]:
        with self._read() as session:
            if session.scalar(select(TaskRow.id).where(TaskRow.id == task_id)) is None:
                raise KeyError("Task not found")
            return [
                self._invocation(row)
                for row in session.scalars(
                    select(InvocationRow)
                    .where(InvocationRow.task_id == task_id)
                    .order_by(InvocationRow.created_at, InvocationRow.call_id)
                )
            ]

    def _save_checkpoint(
        self,
        session: Session,
        row: TaskRow,
        checkpoint: Checkpoint,
    ) -> None:
        if checkpoint.task_id != row.id or not re.fullmatch(r"[0-9a-f]{64}", checkpoint.sha256):
            raise ValueError("Checkpoint task and SHA-256 checksum must be valid")
        existing = session.get(CheckpointRow, checkpoint.id)
        if existing:
            if (
                existing.task_id != checkpoint.task_id
                or existing.artifact != checkpoint.artifact
                or existing.sha256 != checkpoint.sha256
                or existing.base_sha != checkpoint.base_sha
            ):
                raise ValueError("Checkpoint identity is immutable")
        else:
            session.add(CheckpointRow(**checkpoint.model_dump()))
        row.checkpoint_id = checkpoint.id
        row.updated_at = now()
        self._event(session, row, "checkpoint.saved", {"checkpoint_id": checkpoint.id})

    def save_checkpoint(self, lease: Lease, checkpoint: Checkpoint) -> None:
        with self._write() as session:
            self._save_checkpoint(session, self._leased(session, lease), checkpoint)

    def latest_checkpoint(self, task_id: str) -> Checkpoint | None:
        with self._read() as session:
            task = self._row(session, task_id, include_history=False)
            if not task.checkpoint_id:
                return None
            row = session.get(CheckpointRow, task.checkpoint_id)
            if row is None:
                return None
            # A delegated task may intentionally start at its parent's checkpoint.
            owner = self._row(session, row.task_id, include_history=False)
            if (
                owner.root_id != task.root_id
                and owner.session_id != task.session_id
                or owner.spec["repository"] != task.spec["repository"]
            ):
                raise ValueError("Checkpoint belongs to another root task")
            return Checkpoint(
                id=row.id,
                task_id=row.task_id,
                artifact=row.artifact,
                sha256=row.sha256,
                base_sha=row.base_sha,
                created_at=utc(row.created_at),
            )

    def get_checkpoint(self, checkpoint_id: str) -> Checkpoint:
        with self._read() as session:
            row = session.get(CheckpointRow, checkpoint_id)
            if row is None:
                raise KeyError(f"Unknown checkpoint: {checkpoint_id}")
            return Checkpoint(
                id=row.id,
                task_id=row.task_id,
                artifact=row.artifact,
                sha256=row.sha256,
                base_sha=row.base_sha,
                created_at=utc(row.created_at),
            )

    @staticmethod
    def _approval(row: ApprovalRow) -> Approval:
        return Approval(
            id=row.id,
            task_id=row.task_id,
            call=ToolCall.model_validate(row.call),
            digest=row.digest,
            status=row.status,
            created_at=utc(row.created_at),
        )

    @staticmethod
    def _approval_digest(task: TaskRow, call: ToolCall) -> str:
        return digest(
            {
                "repository": task.spec["repository"],
                "checkpoint_id": task.checkpoint_id,
                "call": call.model_dump(mode="json"),
            }
        )

    def request_approval(self, lease: Lease, call: ToolCall) -> Approval:
        with self._write() as session:
            task = self._leased(session, lease, new_work=True)
            fingerprint = self._approval_digest(task, call)
            existing = session.scalar(
                select(ApprovalRow)
                .where(
                    ApprovalRow.task_id == task.id,
                    ApprovalRow.digest == fingerprint,
                    ApprovalRow.status.in_(["pending", "approved"]),
                    ApprovalRow.expires_at > now(),
                )
                .order_by(ApprovalRow.created_at.desc())
            )
            if existing:
                return self._approval(existing)
            timestamp = now()
            row = ApprovalRow(
                id=new_id(),
                task_id=task.id,
                call=call.model_dump(mode="json"),
                digest=fingerprint,
                status="pending",
                created_at=timestamp,
                expires_at=timestamp + timedelta(hours=24),
            )
            session.add(row)
            self._event(session, task, "approval.requested", {"approval_id": row.id})
            return self._approval(row)

    def approvals(self, task_id: str) -> list[Approval]:
        with self._read() as session:
            self._row(session, task_id)
            return [
                self._approval(row)
                for row in session.scalars(
                    select(ApprovalRow)
                    .where(ApprovalRow.task_id == task_id)
                    .order_by(ApprovalRow.created_at, ApprovalRow.id)
                )
            ]

    def decide_approval(self, approval_id: str, approved: bool) -> Approval:
        with self._write() as session:
            # Read task identity first, then consistently lock task before approval.
            initial = session.get(ApprovalRow, approval_id)
            if initial is None:
                raise KeyError(f"Unknown approval: {approval_id}")
            task = self._row(session, initial.task_id, lock=True)
            row = session.scalar(
                select(ApprovalRow).where(ApprovalRow.id == approval_id).with_for_update()
            )
            assert row is not None
            if row.status != "pending":
                raise ValueError("Approval has already been decided")
            if task.cancel_requested or task.state in TERMINAL:
                raise ValueError("Task no longer accepts approvals")
            if utc(row.expires_at) <= now():
                raise ValueError("Approval has expired")
            if row.digest != self._approval_digest(task, ToolCall.model_validate(row.call)):
                raise ValueError("Reviewed content has changed; a new approval is required")
            row.status = "approved" if approved else "rejected"
            if task.state == TaskState.WAITING_FOR_APPROVAL.value:
                task.state = TaskState.QUEUED.value if approved else TaskState.PAUSED.value
                task.error = None if approved else "Publishing approval rejected"
                task.updated_at = now()
            self._event(
                session, task, "approval.decided", {"approval_id": row.id, "status": row.status}
            )
            return self._approval(row)

    def request_approval_changes(self, approval_id: str, note: str) -> Approval:
        """Reject an unexecuted proposal and return owner feedback to the model atomically."""
        note = note.strip()
        if not note or len(note) > 4000:
            raise ValueError("Review feedback must contain 1 to 4000 characters")
        with self._write() as session:
            initial = session.get(ApprovalRow, approval_id)
            if initial is None:
                raise KeyError("Approval not found")
            task = self._row(session, initial.task_id, lock=True)
            row = session.scalar(
                select(ApprovalRow).where(ApprovalRow.id == approval_id).with_for_update()
            )
            assert row is not None
            if (
                row.status != "pending"
                or task.state != TaskState.WAITING_FOR_APPROVAL.value
                or task.cancel_requested
            ):
                raise ValueError("Only a pending approval on a waiting task can be revised")
            call = ToolCall.model_validate(row.call)
            if call.name not in {
                "github_publish_branch",
                "github_create_pr",
                "github_create_issue",
            }:
                raise ValueError("Only publishing proposals support review feedback")
            if row.digest != self._approval_digest(task, call):
                raise ValueError("Reviewed content has changed; a new approval is required")
            if session.get(InvocationRow, (task.id, call.id)) is not None:
                raise ValueError("An operation with an execution intent cannot be revised")
            pending = [
                ToolCall.model_validate(item)
                for entry in task.history
                for item in entry.get("_merlin_calls", [])
                if item.get("id") == call.id
            ]
            answered = any(
                part.get("function_response", part.get("functionResponse", {})).get("id") == call.id
                for entry in task.history
                for part in entry.get("parts", [])
            )
            if not pending or any(item != call for item in pending) or answered:
                raise ValueError("Approval does not match an unanswered model tool call")
            result = ToolResult(
                call_id=call.id,
                ok=False,
                output=(
                    "Owner requested changes before publishing. This tool call was not executed. "
                    "Revise the proposal and request fresh approval. Owner feedback: " + note
                ),
                data={"approval_id": row.id, "revision_requested": True},
            )
            # Preserve the native signed model request. Answer it with a rejected
            # tool result, so revision produces a new call and a new approval.
            self._begin(session, task, call)
            session.flush()
            invocation = session.get(InvocationRow, (task.id, call.id))
            assert invocation is not None
            encoded = result.model_dump(mode="json")
            invocation.result = encoded
            invocation.state = "failed"
            history = json.loads(json.dumps(task.history))
            response = {
                "function_response": {"id": call.id, "name": call.name, "response": encoded}
            }
            if (
                history
                and history[-1].get("role") == "user"
                and history[-1].get("parts")
                and all("function_response" in part for part in history[-1]["parts"])
            ):
                history[-1]["parts"].append(response)
            else:
                history.append({"role": "user", "parts": [response]})
            row.status = "rejected"
            task.history = history
            task.state = TaskState.QUEUED.value
            task.error = None
            task.worker_id = None
            task.lease_until = None
            task.sandbox_id = None
            task.updated_at = now()
            self._event(
                session,
                task,
                "tool.finished",
                {"call_id": call.id, "ok": False, "uncertain": False, "result": encoded},
            )
            self._event(
                session,
                task,
                "approval.changes_requested",
                {"approval_id": row.id, "call_id": call.id, "note": note},
            )
            return self._approval(row)

    def consume_approval(self, lease: Lease, call: ToolCall) -> bool:
        with self._write() as session:
            task = self._leased(session, lease, new_work=True)
            row = session.scalar(
                select(ApprovalRow)
                .where(
                    ApprovalRow.task_id == task.id,
                    ApprovalRow.digest == self._approval_digest(task, call),
                    ApprovalRow.status == "approved",
                    ApprovalRow.expires_at > now(),
                )
                .with_for_update()
            )
            if row is None:
                return False
            row.status = "consumed"
            self._begin(session, task, call)
            self._event(
                session, task, "approval.consumed", {"approval_id": row.id, "call_id": call.id}
            )
            return True

    def reserve_usage(
        self,
        lease: Lease,
        cost_usd: float = 0,
        calls: int = 0,
        sandbox_seconds: int = 0,
    ) -> str:
        if not math.isfinite(cost_usd) or cost_usd < 0 or calls < 0 or sandbox_seconds < 0:
            raise ValueError("Usage amounts must be finite and nonnegative")
        if not isinstance(calls, int) or not isinstance(sandbox_seconds, int):
            raise ValueError("Usage counters must be integers")
        with self._write() as session:
            task = self._leased(session, lease, new_work=True)
            root = self._row(session, task.root_id, include_history=False)
            budget = TaskSpec.model_validate(root.spec).budget
            usage = session.scalar(
                select(UsageRow).where(UsageRow.root_id == root.id).with_for_update()
            )
            assert usage is not None
            if (
                usage.calls + calls > budget.max_calls
                or usage.sandbox_seconds + sandbox_seconds > budget.max_sandbox_seconds
                or usage.cost_usd + usage.reserved_cost_usd + cost_usd > budget.max_cost_usd + 1e-9
            ):
                raise BudgetExceeded("Root task budget exhausted")
            usage.calls += calls
            usage.sandbox_seconds += sandbox_seconds
            usage.reserved_cost_usd += cost_usd
            reservation_id = new_id()
            session.add(
                ReservationRow(
                    id=reservation_id,
                    task_id=task.id,
                    root_id=root.id,
                    estimated_cost_usd=cost_usd,
                    actual_cost_usd=None,
                    calls=calls,
                    sandbox_seconds=sandbox_seconds,
                    status="reserved" if calls or cost_usd else "settled",
                )
            )
            self._event(
                session,
                task,
                "usage.reserved",
                {
                    "reservation": reservation_id,
                    "cost_usd": cost_usd,
                    "calls": calls,
                    "sandbox_seconds": sandbox_seconds,
                },
            )
            return reservation_id

    def mark_usage_dispatched(self, lease: Lease, reservation: str) -> None:
        """Persist the billing-uncertainty boundary before contacting a provider."""
        with self._write() as session:
            task = self._leased(session, lease, new_work=True)
            row = session.scalar(
                select(ReservationRow).where(ReservationRow.id == reservation).with_for_update()
            )
            if row is None or row.task_id != task.id:
                raise KeyError("Unknown reservation for this task")
            if row.status == "dispatched":
                return
            if row.status != "reserved" or row.actual_cost_usd is not None:
                raise ValueError("Only an unused reservation can be dispatched")
            row.status, row.dispatched_at = "dispatched", now()
            self._event(session, task, "usage.dispatched", {"reservation": reservation})

    def release_usage(self, lease: Lease, reservation: str, *, undispatched: bool = False) -> None:
        """Refund unused usage, optionally with trusted adapter proof of no request.

        The override is an internal worker boundary, never an HTTP/model argument.
        A provider adapter may reject during local validation after the worker
        marked dispatch intent but before sending a generation request.
        """
        with self._write() as session:
            task = self._leased(session, lease)
            row = session.scalar(
                select(ReservationRow).where(ReservationRow.id == reservation).with_for_update()
            )
            if row is None or row.task_id != task.id:
                raise KeyError("Unknown reservation for this task")
            if row.status == "released":
                return
            eligible = row.status == "reserved" or undispatched and row.status == "dispatched"
            if not eligible or row.actual_cost_usd is not None:
                raise ValueError("Dispatched, settled or unknown usage cannot be released")
            usage = session.scalar(
                select(UsageRow).where(UsageRow.root_id == task.root_id).with_for_update()
            )
            assert usage is not None
            usage.calls = max(0, usage.calls - row.calls)
            usage.sandbox_seconds = max(0, usage.sandbox_seconds - row.sandbox_seconds)
            usage.reserved_cost_usd = max(0, usage.reserved_cost_usd - row.estimated_cost_usd)
            row.status, row.actual_cost_usd = "released", 0
            self._event(
                session,
                task,
                "usage.released",
                {
                    "reservation": reservation,
                    "calls": row.calls,
                    "cost_usd": row.estimated_cost_usd,
                    "adapter_proved_no_request": undispatched,
                },
            )

    def amend_budget(self, task_id: str, budget: Budget, note: str) -> Task:
        note = note.strip()
        if not note or len(note) > 4000:
            raise ValueError("Budget changes require an operator note")
        with self._write() as session:
            task = self._row(session, task_id, lock=True)
            if (
                task.parent_id is not None
                or task.state != TaskState.PAUSED.value
                or task.cancel_requested
            ):
                raise ValueError("Only a paused root task can change its budget")
            usage = session.scalar(
                select(UsageRow).where(UsageRow.root_id == task.id).with_for_update()
            )
            assert usage is not None
            if (
                budget.max_calls < usage.calls
                or budget.max_sandbox_seconds < usage.sandbox_seconds
                or budget.max_cost_usd < usage.cost_usd + usage.reserved_cost_usd
            ):
                raise ValueError("Budget cannot be lower than recorded and reserved usage")
            spec = TaskSpec.model_validate(task.spec)
            before = spec.budget.model_dump(mode="json")
            if (
                budget.max_calls < spec.budget.max_calls
                or budget.max_sandbox_seconds < spec.budget.max_sandbox_seconds
                or budget.max_cost_usd < spec.budget.max_cost_usd
            ):
                # Children may still hold independent leases. Increases commute
                # with their reservations; shrinking would require quiescing the
                # whole root to avoid a read-before-amend accounting race.
                raise ValueError("Budget amendments may only increase limits")
            task.spec = spec.model_copy(update={"budget": budget}).model_dump(mode="json")
            task.updated_at = now()
            self._event(
                session,
                task,
                "budget.amended",
                {"before": before, "after": budget.model_dump(mode="json"), "note": note},
            )
            return self._task(task)

    def reservations(self, task_id: str) -> list[dict[str, Any]]:
        with self._read() as session:
            if session.scalar(select(TaskRow.id).where(TaskRow.id == task_id)) is None:
                raise KeyError("Task not found")
            rows = session.execute(
                select(
                    ReservationRow.id,
                    ReservationRow.status,
                    ReservationRow.calls,
                    ReservationRow.estimated_cost_usd,
                    ReservationRow.actual_cost_usd,
                    ReservationRow.dispatched_at,
                    ReservationRow.resolution_note,
                )
                .where(
                    ReservationRow.task_id == task_id,
                    ReservationRow.status.in_(["reserved", "dispatched", "unknown"]),
                )
                .order_by(ReservationRow.id)
                .limit(100)
            )
            return [dict(row._mapping) for row in rows]

    def reconcile_reservation(
        self, task_id: str, reservation: str, actual_cost_usd: float, note: str
    ) -> None:
        note = note.strip()
        if (
            not math.isfinite(actual_cost_usd)
            or actual_cost_usd < 0
            or not note
            or len(note) > 4000
        ):
            raise ValueError("Reconciliation needs finite nonnegative cost and an operator note")
        with self._write() as session:
            task = self._row(session, task_id, lock=True)
            row = session.scalar(
                select(ReservationRow).where(ReservationRow.id == reservation).with_for_update()
            )
            if row is None or row.task_id != task.id:
                raise KeyError("Unknown reservation for this task")
            if task.state not in TERMINAL | {TaskState.PAUSED.value}:
                raise ValueError("Stop or pause a task before reconciling usage")
            if row.actual_cost_usd is not None or row.status in {"settled", "released"}:
                raise ValueError("Resolved usage is immutable")
            usage = session.scalar(
                select(UsageRow).where(UsageRow.root_id == task.root_id).with_for_update()
            )
            assert usage is not None
            usage.reserved_cost_usd = max(0, usage.reserved_cost_usd - row.estimated_cost_usd)
            usage.cost_usd += actual_cost_usd
            row.status, row.actual_cost_usd, row.resolution_note = "settled", actual_cost_usd, note
            self._event(
                session,
                task,
                "usage.reconciled",
                {"reservation": reservation, "cost_usd": actual_cost_usd, "note": note},
            )

    def settle_usage(self, lease: Lease, reservation: str, cost_usd: float) -> None:
        if not math.isfinite(cost_usd) or cost_usd < 0:
            raise ValueError("Actual cost must be finite and nonnegative")
        with self._write() as session:
            task = self._leased(session, lease)
            reserved = session.scalar(
                select(ReservationRow).where(ReservationRow.id == reservation).with_for_update()
            )
            if reserved is None or reserved.task_id != task.id:
                raise KeyError("Unknown reservation for this task")
            if reserved.status == "released":
                raise ValueError("Released usage cannot be settled")
            if reserved.actual_cost_usd is not None:
                if reserved.actual_cost_usd != cost_usd:
                    raise ValueError("Settled usage is immutable")
                return
            usage = session.scalar(
                select(UsageRow).where(UsageRow.root_id == task.root_id).with_for_update()
            )
            assert usage is not None
            usage.reserved_cost_usd = max(0, usage.reserved_cost_usd - reserved.estimated_cost_usd)
            usage.cost_usd += cost_usd
            reserved.actual_cost_usd = cost_usd
            reserved.status = "settled"
            self._event(
                session, task, "usage.settled", {"reservation": reservation, "cost_usd": cost_usd}
            )

    def usage(self, task_id: str) -> dict[str, Any]:
        with self._read() as session:
            root_id = session.scalar(select(TaskRow.root_id).where(TaskRow.id == task_id))
            if root_id is None:
                raise KeyError("Task not found")
            usage = session.get(UsageRow, root_id)
            assert usage is not None
            return {
                "root_id": root_id,
                "calls": usage.calls,
                "sandbox_seconds": usage.sandbox_seconds,
                "cost_usd": usage.cost_usd,
                "reserved_cost_usd": usage.reserved_cost_usd,
            }

    def cancel(self, task_id: str) -> Task:
        with self._write() as session:
            task = self._row(session, task_id, lock=True)
            rows = [task]
            # Delegation depth is one, but root_id also covers future deeper trees.
            if task.parent_id is None:
                rows.extend(
                    session.scalars(
                        select(TaskRow)
                        .where(TaskRow.root_id == task.id, TaskRow.id != task.id)
                        .order_by(TaskRow.id)
                        .with_for_update()
                    )
                )
            for row in rows:
                if row.state in TERMINAL:
                    continue
                row.cancel_requested = True
                row.updated_at = now()
                if row.state != TaskState.RUNNING.value:
                    row.state = TaskState.CANCELLED.value
                    row.worker_id = None
                    row.lease_until = None
                self._event(session, row, "task.cancel_requested", {"state": row.state})
            return self._task(task)

    def resume(self, task_id: str) -> Task:
        with self._write() as session:
            row = self._row(session, task_id, lock=True)
            if row.state != TaskState.PAUSED.value:
                raise ValueError("Only paused tasks can be resumed")
            if row.cancel_requested:
                raise ValueError("Cancelled task cannot be resumed")
            from merlin.agent import SAFE_REPLAY_TOOLS

            invocations = session.scalars(
                select(InvocationRow).where(InvocationRow.task_id == task_id)
            )
            if any(
                invocation.result is not None
                and invocation.result.get("uncertain")
                or invocation.result is None
                and invocation.call["name"] not in SAFE_REPLAY_TOOLS
                for invocation in invocations
            ):
                raise ValueError("Uncertain tool outcomes must be inspected and resolved first")
            row.state = TaskState.QUEUED.value
            row.worker_id = None
            row.lease_until = None
            row.updated_at = now()
            row.error = None
            self._event(session, row, "task.resumed", {})
            return self._task(row)

    def resolve_tool(self, task_id: str, call_id: str, note: str) -> Task:
        """Acknowledge an unknown outcome without replaying or claiming success."""
        note = note.strip()
        if not note or len(note) > 4000:
            raise ValueError("Resolution requires an operator note of 1 to 4000 characters")
        with self._write() as session:
            task = self._row(session, task_id, lock=True)
            if task.state != TaskState.PAUSED.value or task.cancel_requested:
                raise ValueError("Only paused tasks can resolve an uncertain tool")
            invocation = session.get(InvocationRow, (task.id, call_id))
            if invocation is None:
                raise KeyError(f"Unknown invocation: {call_id}")
            if invocation.result is not None and not invocation.result.get("uncertain"):
                raise ValueError("Known tool results are immutable")
            result = ToolResult(
                call_id=call_id,
                ok=False,
                output=(
                    "Operator acknowledged uncertain outcome; restored last completed checkpoint. "
                    "External effects are not rolled back. " + note
                ),
                uncertain=False,
            )
            invocation.result = result.model_dump(mode="json")
            invocation.state = "failed"
            history = json.loads(json.dumps(task.history))
            for entry in history:
                for part in entry.get("parts", []):
                    if not isinstance(part, dict):
                        continue
                    response = part.get("function_response") or part.get("functionResponse")
                    if isinstance(response, dict) and response.get("id") == call_id:
                        response["response"] = result.model_dump(mode="json")
            task.history = history
            task.state = TaskState.QUEUED.value
            task.error = None
            task.worker_id = None
            task.lease_until = None
            # Acknowledgement restores the completed filesystem snapshot in a
            # new sandbox rather than trusting an uncertain surviving process.
            task.sandbox_id = None
            task.updated_at = now()
            self._event(session, task, "tool.operator_resolved", {"call_id": call_id, "note": note})
            return self._task(task)
