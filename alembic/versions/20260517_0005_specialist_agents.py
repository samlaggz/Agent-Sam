"""specialist agent operating system

Revision ID: 20260517_0005
Revises: 20260515_0004
Create Date: 2026-05-17 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260517_0005"
down_revision = "20260515_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("skill_proposals", sa.Column("agent_slug", sa.String(length=128), nullable=True))
    op.add_column("skill_proposals", sa.Column("evidence_text", sa.Text(), nullable=True))
    op.add_column("model_calls", sa.Column("estimated_cost_usd", sa.Float(), nullable=True))
    op.add_column("model_calls", sa.Column("actual_cost_usd", sa.Float(), nullable=True))
    op.add_column("model_calls", sa.Column("error_text", sa.Text(), nullable=True))

    op.create_table(
        "agent_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("slug", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("profile_yaml", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", "version", name="uq_agent_profiles_slug_version"),
    )
    op.create_index("ix_agent_profiles_slug", "agent_profiles", ["slug"], unique=False)

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("task_run_id", sa.Uuid(), nullable=True),
        sa.Column("agent_slug", sa.String(length=128), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="started", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_summary", sa.Text(), nullable=True),
        sa.Column("output_summary", sa.Text(), nullable=True),
        sa.Column("cost_estimate", sa.Float(), nullable=True),
        sa.Column("actual_cost", sa.Float(), nullable=True),
        sa.Column("metadata", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["task_run_id"], ["task_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_runs_task_agent_created", "agent_runs", ["task_id", "agent_slug", "created_at"], unique=False)
    op.create_index("ix_agent_runs_status_created", "agent_runs", ["status", "created_at"], unique=False)

    op.create_table(
        "agent_run_steps",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=False),
        sa.Column("step_type", sa.String(length=64), server_default="note", nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="completed", nullable=False),
        sa.Column("metadata", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_run_steps_agent_run_created", "agent_run_steps", ["agent_run_id", "created_at"], unique=False)

    op.create_table(
        "agent_evaluations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=False),
        sa.Column("evaluator_slug", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("checklist", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_evaluations_agent_run_created", "agent_evaluations", ["agent_run_id", "created_at"], unique=False)

    op.create_table(
        "sub_agent_proposals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("parent_agent_slug", sa.String(length=128), nullable=False),
        sa.Column("proposed_slug", sa.String(length=128), nullable=False),
        sa.Column("proposed_name", sa.String(length=255), nullable=False),
        sa.Column("proposed_profile_yaml", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("expected_savings", sa.Text(), nullable=True),
        sa.Column("expected_quality_gain", sa.Text(), nullable=True),
        sa.Column("required_tools", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("prompt_markdown", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("reviewed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("proposed_slug"),
    )
    op.create_index("ix_sub_agent_proposals_status_created", "sub_agent_proposals", ["status", "created_at"], unique=False)
    op.create_index("ix_sub_agent_proposals_parent_agent_slug", "sub_agent_proposals", ["parent_agent_slug"], unique=False)

    op.create_table(
        "learning_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("agent_slug", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), server_default="0.5", nullable=False),
        sa.Column("approved", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("metadata", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_learning_events_agent_created", "learning_events", ["agent_slug", "created_at"], unique=False)

    op.create_table(
        "model_budgets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("scope_key", sa.String(length=255), nullable=False),
        sa.Column("budget_usd", sa.Float(), nullable=False),
        sa.Column("spent_usd", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_model_budgets_scope_key", "model_budgets", ["scope_key"], unique=False)
    op.create_index("ix_model_budgets_scope_period", "model_budgets", ["scope", "period_start", "period_end"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_model_budgets_scope_period", table_name="model_budgets")
    op.drop_index("ix_model_budgets_scope_key", table_name="model_budgets")
    op.drop_table("model_budgets")
    op.drop_index("ix_learning_events_agent_created", table_name="learning_events")
    op.drop_table("learning_events")
    op.drop_index("ix_sub_agent_proposals_parent_agent_slug", table_name="sub_agent_proposals")
    op.drop_index("ix_sub_agent_proposals_status_created", table_name="sub_agent_proposals")
    op.drop_table("sub_agent_proposals")
    op.drop_index("ix_agent_evaluations_agent_run_created", table_name="agent_evaluations")
    op.drop_table("agent_evaluations")
    op.drop_index("ix_agent_run_steps_agent_run_created", table_name="agent_run_steps")
    op.drop_table("agent_run_steps")
    op.drop_index("ix_agent_runs_status_created", table_name="agent_runs")
    op.drop_index("ix_agent_runs_task_agent_created", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_index("ix_agent_profiles_slug", table_name="agent_profiles")
    op.drop_table("agent_profiles")
    op.drop_column("model_calls", "error_text")
    op.drop_column("model_calls", "actual_cost_usd")
    op.drop_column("model_calls", "estimated_cost_usd")
    op.drop_column("skill_proposals", "evidence_text")
    op.drop_column("skill_proposals", "agent_slug")