"""Reservation dispatch accounting, context summaries and bounded-query indexes."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "usage_reservations", sa.Column("calls", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "usage_reservations",
        sa.Column("sandbox_seconds", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "usage_reservations",
        sa.Column("status", sa.String(20), nullable=False, server_default="unknown"),
    )
    op.add_column("usage_reservations", sa.Column("dispatched_at", sa.DateTime(timezone=True)))
    op.add_column("usage_reservations", sa.Column("resolution_note", sa.Text()))
    # Historical unsettled requests may already have been billed. Never infer that
    # they were undispatched merely because the old schema lacks a marker.
    op.execute("UPDATE usage_reservations SET status = 'settled' WHERE actual_cost_usd IS NOT NULL")
    op.create_table(
        "context_checkpoints",
        sa.Column("task_id", sa.String(64), sa.ForeignKey("tasks.id"), primary_key=True),
        sa.Column("history_sequence", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tasks_session_created", "tasks", ["session_id", "created_at", "id"])
    op.create_index("ix_tasks_parent_state", "tasks", ["parent_id", "state"])
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(200), primary_key=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_worker_heartbeats_updated_at", "worker_heartbeats", ["updated_at"])


def downgrade() -> None:
    op.drop_table("worker_heartbeats")
    op.drop_index("ix_tasks_parent_state", table_name="tasks")
    op.drop_index("ix_tasks_session_created", table_name="tasks")
    op.drop_table("context_checkpoints")
    for column in ("resolution_note", "dispatched_at", "status", "sandbox_seconds", "calls"):
        op.drop_column("usage_reservations", column)
