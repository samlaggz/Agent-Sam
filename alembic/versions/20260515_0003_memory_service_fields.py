"""Add structured memory service fields.

Revision ID: 20260515_0003
Revises: 20260515_0002
Create Date: 2026-05-15 00:03:00
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260515_0003"
down_revision: str | None = "20260515_0002"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memories",
        sa.Column("memory_type", sa.String(length=32), nullable=False, server_default=sa.text("'project_fact'")),
    )
    op.add_column(
        "memories",
        sa.Column("source", sa.String(length=255), nullable=False, server_default=sa.text("'system'")),
    )
    op.add_column(
        "memories",
        sa.Column("confidence", sa.Float(), nullable=False, server_default=sa.text("0.5")),
    )
    op.add_column(
        "memories",
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "memories",
        sa.Column("embedding_vector", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "memories",
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_memories_workspace_type_last_used",
        "memories",
        ["workspace_id", "memory_type", "last_used_at"],
        unique=False,
    )
    op.create_index(
        "ix_memories_workspace_scope_last_used",
        "memories",
        ["workspace_id", "scope", "last_used_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_memories_workspace_scope_last_used", table_name="memories")
    op.drop_index("ix_memories_workspace_type_last_used", table_name="memories")
    op.drop_column("memories", "last_used_at")
    op.drop_column("memories", "embedding_vector")
    op.drop_column("memories", "tags")
    op.drop_column("memories", "confidence")
    op.drop_column("memories", "source")
    op.drop_column("memories", "memory_type")