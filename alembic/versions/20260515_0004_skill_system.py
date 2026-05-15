"""Add structured skill system tables and fields.

Revision ID: 20260515_0004
Revises: 20260515_0003
Create Date: 2026-05-15 12:30:00
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260515_0004"
down_revision: str | None = "20260515_0003"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


EMPTY_JSONB = sa.text("'{}'::jsonb")
EMPTY_JSON_ARRAY = sa.text("'[]'::jsonb")


def upgrade() -> None:
    op.create_table(
        "skill_proposals",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("base_skill_id", sa.Uuid(), nullable=True),
        sa.Column("applied_skill_id", sa.Uuid(), nullable=True),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("reviewed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("change_type", sa.String(length=32), nullable=False, server_default=sa.text("'create'")),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("yaml_definition", sa.Text(), nullable=False),
        sa.Column("source_path", sa.String(length=255), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("triggers", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=EMPTY_JSON_ARRAY),
        sa.Column("inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=EMPTY_JSON_ARRAY),
        sa.Column("procedure", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=EMPTY_JSON_ARRAY),
        sa.Column("tools_allowed", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=EMPTY_JSON_ARRAY),
        sa.Column("risk_notes", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=EMPTY_JSON_ARRAY),
        sa.Column("failure_modes", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=EMPTY_JSON_ARRAY),
        sa.Column("evaluation_checklist", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=EMPTY_JSON_ARRAY),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=EMPTY_JSONB),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["applied_skill_id"], ["skills.id"], name=op.f("fk_skill_proposals_applied_skill_id_skills"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["base_skill_id"], ["skills.id"], name=op.f("fk_skill_proposals_base_skill_id_skills"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"], name=op.f("fk_skill_proposals_requested_by_user_id_users"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["users.id"], name=op.f("fk_skill_proposals_reviewed_by_user_id_users"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name=op.f("fk_skill_proposals_workspace_id_workspaces"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skill_proposals")),
        sa.UniqueConstraint(
            "workspace_id",
            "name",
            "version",
            "content_hash",
            name="uq_skill_proposals_workspace_name_version_hash",
        ),
    )
    op.create_index(
        "ix_skill_proposals_workspace_status_created",
        "skill_proposals",
        ["workspace_id", "status", "created_at"],
        unique=False,
    )

    op.add_column("skills", sa.Column("source_path", sa.String(length=255), nullable=True))
    op.add_column("skills", sa.Column("content_hash", sa.String(length=64), nullable=True))
    op.add_column(
        "skills",
        sa.Column("triggers", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=EMPTY_JSON_ARRAY),
    )
    op.add_column(
        "skills",
        sa.Column("inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=EMPTY_JSON_ARRAY),
    )
    op.add_column(
        "skills",
        sa.Column("procedure", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=EMPTY_JSON_ARRAY),
    )
    op.add_column(
        "skills",
        sa.Column("tools_allowed", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=EMPTY_JSON_ARRAY),
    )
    op.add_column(
        "skills",
        sa.Column("risk_notes", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=EMPTY_JSON_ARRAY),
    )
    op.add_column(
        "skills",
        sa.Column("failure_modes", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=EMPTY_JSON_ARRAY),
    )
    op.add_column(
        "skills",
        sa.Column("evaluation_checklist", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=EMPTY_JSON_ARRAY),
    )
    op.add_column("skills", sa.Column("approved_by_user_id", sa.Uuid(), nullable=True))
    op.add_column("skills", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        op.f("fk_skills_approved_by_user_id_users"),
        "skills",
        "users",
        ["approved_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.alter_column("skills", "status", server_default=sa.text("'active'"))
    op.execute("UPDATE skills SET status = 'active' WHERE status = 'draft'")
    op.create_index("ix_skills_workspace_status_name", "skills", ["workspace_id", "status", "name"], unique=False)

    op.add_column("approvals", sa.Column("skill_proposal_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_approvals_skill_proposal_id_skill_proposals"),
        "approvals",
        "skill_proposals",
        ["skill_proposal_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_approvals_skill_proposal_id_skill_proposals"), "approvals", type_="foreignkey")
    op.drop_column("approvals", "skill_proposal_id")

    op.drop_index("ix_skills_workspace_status_name", table_name="skills")
    op.alter_column("skills", "status", server_default=sa.text("'draft'"))
    op.drop_constraint(op.f("fk_skills_approved_by_user_id_users"), "skills", type_="foreignkey")
    op.drop_column("skills", "approved_at")
    op.drop_column("skills", "approved_by_user_id")
    op.drop_column("skills", "evaluation_checklist")
    op.drop_column("skills", "failure_modes")
    op.drop_column("skills", "risk_notes")
    op.drop_column("skills", "tools_allowed")
    op.drop_column("skills", "procedure")
    op.drop_column("skills", "inputs")
    op.drop_column("skills", "triggers")
    op.drop_column("skills", "content_hash")
    op.drop_column("skills", "source_path")

    op.drop_index("ix_skill_proposals_workspace_status_created", table_name="skill_proposals")
    op.drop_table("skill_proposals")