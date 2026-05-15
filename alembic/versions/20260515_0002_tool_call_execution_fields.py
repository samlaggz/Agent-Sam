"""Add shell command execution fields to tool calls.

Revision ID: 20260515_0002
Revises: 20260515_0001
Create Date: 2026-05-15 00:02:00
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260515_0002"
down_revision: str | None = "20260515_0001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tool_calls", sa.Column("stderr_text", sa.Text(), nullable=True))
    op.add_column("tool_calls", sa.Column("duration_ms", sa.Integer(), nullable=True))
    op.add_column(
        "tool_calls",
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'pending'")),
    )
    op.alter_column(
        "tool_calls",
        "risk_level",
        existing_type=sa.String(length=32),
        server_default=sa.text("'safe'"),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "tool_calls",
        "risk_level",
        existing_type=sa.String(length=32),
        server_default=sa.text("'low'"),
        existing_nullable=False,
    )
    op.drop_column("tool_calls", "status")
    op.drop_column("tool_calls", "duration_ms")
    op.drop_column("tool_calls", "stderr_text")