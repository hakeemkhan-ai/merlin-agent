"""Portable schema; PostgreSQL provides production row-locking semantics."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TaskRow(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(100), index=True)
    root_id: Mapped[str] = mapped_column(String(64), index=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"))
    state: Mapped[str] = mapped_column(String(40), index=True)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    worker_id: Mapped[str | None] = mapped_column(String(200))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    sandbox_id: Mapped[str | None] = mapped_column(String(200))
    checkpoint_id: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    event_sequence: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    request_digest: Mapped[str] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_tasks_claim", "state", "created_at"),
        Index("ix_tasks_session_created", "session_id", "created_at", "id"),
        Index("ix_tasks_parent_state", "parent_id", "state"),
    )


class EventRow(Base):
    __tablename__ = "task_events"

    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(100))
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class InvocationRow(Base):
    __tablename__ = "tool_invocations"

    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    call_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    call: Mapped[dict[str, Any]] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(30))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CheckpointRow(Base):
    __tablename__ = "checkpoints"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    artifact: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    base_sha: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ApprovalRow(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    call: Mapped[dict[str, Any]] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UsageRow(Base):
    __tablename__ = "root_usage"

    root_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    calls: Mapped[int] = mapped_column(Integer, default=0)
    sandbox_seconds: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0)
    reserved_cost_usd: Mapped[float] = mapped_column(Float, default=0)


class ReservationRow(Base):
    __tablename__ = "usage_reservations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    root_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    estimated_cost_usd: Mapped[float] = mapped_column(Float)
    actual_cost_usd: Mapped[float | None] = mapped_column(Float)
    calls: Mapped[int] = mapped_column(Integer, default=0)
    sandbox_seconds: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="reserved")
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_note: Mapped[str | None] = mapped_column(Text)


class ContextRow(Base):
    __tablename__ = "context_checkpoints"

    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    history_sequence: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WorkerHeartbeatRow(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
