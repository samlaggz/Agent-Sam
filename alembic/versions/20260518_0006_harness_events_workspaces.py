"""Add harness events and runtime workspace tables.

Revision ID: 20260518_0006
Revises: 20260517_0005
Create Date: 2026-05-18 00:06:00
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260518_0006"
down_revision: str | None = "20260517_0005"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_events",
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("agent_run_id", sa.Uuid(), nullable=True),
        sa.Column("agent_slug", sa.String(length=128), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("parent_event_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["parent_event_id"], ["agent_events.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_events")),
    )
    op.create_index("ix_agent_events_task_sequence", "agent_events", ["task_id", "sequence"], unique=False)
    op.create_index("ix_agent_events_run_sequence", "agent_events", ["agent_run_id", "sequence"], unique=False)

    op.create_table(
        "workspaces_runtime",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'active'")),
        sa.Column("last_used_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspaces_runtime")),
    )
    op.create_index(
        "ix_workspaces_runtime_task_status_created",
        "workspaces_runtime",
        ["task_id", "status", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_workspaces_runtime_task_status_created", table_name="workspaces_runtime")
    op.drop_table("workspaces_runtime")
    op.drop_index("ix_agent_events_run_sequence", table_name="agent_events")
    op.drop_index("ix_agent_events_task_sequence", table_name="agent_events")
    op.drop_table("agent_events")