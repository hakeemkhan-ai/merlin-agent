"""Durable tasks, events, tool intents, approvals, checkpoints, and budgets."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("session_id", sa.String(100), nullable=False),
        sa.Column("root_id", sa.String(64), nullable=False),
        sa.Column("parent_id", sa.String(64), sa.ForeignKey("tasks.id")),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(200)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("history", sa.JSON(), nullable=False),
        sa.Column("sandbox_id", sa.String(200)),
        sa.Column("checkpoint_id", sa.String(64)),
        sa.Column("error", sa.Text()),
        sa.Column("result", sa.Text()),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("event_sequence", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(200), unique=True),
        sa.Column("request_digest", sa.String(64), nullable=False),
    )
    for column in ("session_id", "root_id", "state"):
        op.create_index(f"ix_tasks_{column}", "tasks", [column])
    op.create_index("ix_tasks_claim", "tasks", ["state", "created_at"])
    op.create_table(
        "task_events",
        sa.Column("task_id", sa.String(64), sa.ForeignKey("tasks.id"), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(100), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "tool_invocations",
        sa.Column("task_id", sa.String(64), sa.ForeignKey("tasks.id"), primary_key=True),
        sa.Column("call_id", sa.String(200), primary_key=True),
        sa.Column("call", sa.JSON(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("result", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "checkpoints",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("task_id", sa.String(64), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("artifact", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("base_sha", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_checkpoints_task_id", "checkpoints", ["task_id"])
    op.create_table(
        "approvals",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("task_id", sa.String(64), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("call", sa.JSON(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_approvals_task_id", "approvals", ["task_id"])
    op.create_index("ix_approvals_digest", "approvals", ["digest"])
    op.create_table(
        "root_usage",
        sa.Column("root_id", sa.String(64), sa.ForeignKey("tasks.id"), primary_key=True),
        sa.Column("calls", sa.Integer(), nullable=False),
        sa.Column("sandbox_seconds", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("reserved_cost_usd", sa.Float(), nullable=False),
    )
    op.create_table(
        "usage_reservations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("task_id", sa.String(64), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("root_id", sa.String(64), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=False),
        sa.Column("actual_cost_usd", sa.Float()),
    )
    op.create_index("ix_usage_reservations_task_id", "usage_reservations", ["task_id"])


def downgrade() -> None:
    for table in (
        "usage_reservations",
        "root_usage",
        "approvals",
        "checkpoints",
        "tool_invocations",
        "task_events",
        "tasks",
    ):
        op.drop_table(table)
