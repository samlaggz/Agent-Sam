from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from db.types import EMPTY_JSON_ARRAY, EMPTY_JSON_OBJECT, JSON_VARIANT


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    display_name: Mapped[str] = mapped_column(String(255))
    telegram_user_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    preferences: Mapped[dict[str, Any]] = mapped_column(
        JSON_VARIANT,
        default=dict,
        server_default=EMPTY_JSON_OBJECT,
    )

    workspaces: Mapped[list["Workspace"]] = relationship(back_populates="owner")
    messages: Mapped[list["Message"]] = relationship(back_populates="user")
    created_tasks: Mapped[list["Task"]] = relationship(back_populates="created_by")
    memories: Mapped[list["Memory"]] = relationship(back_populates="user")
    approval_requests: Mapped[list["Approval"]] = relationship(
        back_populates="requested_by",
        foreign_keys="Approval.requested_by_user_id",
    )
    approvals_reviewed: Mapped[list["Approval"]] = relationship(
        back_populates="reviewed_by",
        foreign_keys="Approval.reviewed_by_user_id",
    )
    approved_skills: Mapped[list["Skill"]] = relationship(
        back_populates="approved_by",
        foreign_keys="Skill.approved_by_user_id",
    )
    skill_proposals_requested: Mapped[list["SkillProposal"]] = relationship(
        back_populates="requested_by",
        foreign_keys="SkillProposal.requested_by_user_id",
    )
    skill_proposals_reviewed: Mapped[list["SkillProposal"]] = relationship(
        back_populates="reviewed_by",
        foreign_keys="SkillProposal.reviewed_by_user_id",
    )


class Workspace(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "workspaces"

    owner_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSON_VARIANT,
        default=dict,
        server_default=EMPTY_JSON_OBJECT,
    )

    owner: Mapped[User | None] = relationship(back_populates="workspaces")
    messages: Mapped[list["Message"]] = relationship(back_populates="workspace")
    tasks: Mapped[list["Task"]] = relationship(back_populates="workspace")
    memories: Mapped[list["Memory"]] = relationship(back_populates="workspace")
    skills: Mapped[list["Skill"]] = relationship(back_populates="workspace")
    skill_proposals: Mapped[list["SkillProposal"]] = relationship(back_populates="workspace")
    approvals: Mapped[list["Approval"]] = relationship(back_populates="workspace")


class Task(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_parent_status_created", "parent_task_id", "status", "created_at"),
        Index("ix_tasks_workspace_status_created", "workspace_id", "status", "created_at"),
        Index("ix_tasks_status_priority_created", "status", "priority", "created_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    parent_task_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[str] = mapped_column(String(32), default="normal", server_default=text("'normal'"))
    status: Mapped[str] = mapped_column(String(32), default="pending", server_default=text("'pending'"))
    assigned_worker: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSON_VARIANT,
        default=dict,
        server_default=EMPTY_JSON_OBJECT,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    workspace: Mapped[Workspace] = relationship(back_populates="tasks")
    created_by: Mapped[User | None] = relationship(back_populates="created_tasks")
    parent_task: Mapped[Task | None] = relationship(remote_side="Task.id", back_populates="child_tasks")
    child_tasks: Mapped[list["Task"]] = relationship(back_populates="parent_task")
    messages: Mapped[list["Message"]] = relationship(back_populates="task")
    steps: Mapped[list["TaskStep"]] = relationship(back_populates="task")
    runs: Mapped[list["TaskRun"]] = relationship(back_populates="task")
    tool_calls: Mapped[list["ToolCall"]] = relationship(back_populates="task")
    memories: Mapped[list["Memory"]] = relationship(back_populates="task")
    approvals: Mapped[list["Approval"]] = relationship(back_populates="task")
    model_calls: Mapped[list["ModelCall"]] = relationship(back_populates="task")


class Message(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_workspace_created", "workspace_id", "created_at"),)

    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    parent_message_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    role: Mapped[str] = mapped_column(String(32), default="user", server_default=text("'user'"))
    content: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSON_VARIANT,
        default=dict,
        server_default=EMPTY_JSON_OBJECT,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    workspace: Mapped[Workspace] = relationship(back_populates="messages")
    user: Mapped[User | None] = relationship(back_populates="messages")
    task: Mapped[Task | None] = relationship(back_populates="messages")
    parent_message: Mapped[Message | None] = relationship(
        remote_side="Message.id",
        back_populates="child_messages",
    )
    child_messages: Mapped[list["Message"]] = relationship(back_populates="parent_message")
    tool_calls: Mapped[list["ToolCall"]] = relationship(back_populates="message")
    model_calls: Mapped[list["ModelCall"]] = relationship(back_populates="message")


class TaskStep(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "task_steps"
    __table_args__ = (
        UniqueConstraint("task_id", "position", name="uq_task_steps_task_position"),
        Index("ix_task_steps_task_status_created", "task_id", "status", "created_at"),
    )

    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", server_default=text("'pending'"))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSON_VARIANT,
        default=dict,
        server_default=EMPTY_JSON_OBJECT,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    task: Mapped[Task] = relationship(back_populates="steps")


class TaskRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "task_runs"
    __table_args__ = (
        UniqueConstraint("task_id", "attempt_number", name="uq_task_runs_task_attempt"),
        Index("ix_task_runs_task_status_created", "task_id", "status", "created_at"),
    )

    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    worker_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    status: Mapped[str] = mapped_column(String(32), default="pending", server_default=text("'pending'"))
    context_json: Mapped[dict[str, Any]] = mapped_column(
        "context",
        JSON_VARIANT,
        default=dict,
        server_default=EMPTY_JSON_OBJECT,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    task: Mapped[Task] = relationship(back_populates="runs")
    tool_calls: Mapped[list["ToolCall"]] = relationship(back_populates="task_run")
    model_calls: Mapped[list["ModelCall"]] = relationship(back_populates="task_run")


class ToolCall(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "tool_calls"
    __table_args__ = (
        Index("ix_tool_calls_task_created", "task_id", "created_at"),
        Index("ix_tool_calls_tool_risk_created", "tool_name", "risk_level", "created_at"),
    )

    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    task_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("task_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    message_id: Mapped[UUID | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)
    tool_name: Mapped[str] = mapped_column(String(128))
    input_payload: Mapped[dict[str, Any]] = mapped_column(
        "input",
        JSON_VARIANT,
        default=dict,
        server_default=EMPTY_JSON_OBJECT,
    )
    output_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", server_default=text("'pending'"))
    risk_level: Mapped[str] = mapped_column(String(32), default="safe", server_default=text("'safe'"))
    approved_by_user: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task: Mapped[Task | None] = relationship(back_populates="tool_calls")
    task_run: Mapped[TaskRun | None] = relationship(back_populates="tool_calls")
    message: Mapped[Message | None] = relationship(back_populates="tool_calls")
    approvals: Mapped[list["Approval"]] = relationship(back_populates="tool_call")


class Memory(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "memories"
    __table_args__ = (
        Index("ix_memories_workspace_scope_key", "workspace_id", "scope", "key"),
        Index("ix_memories_workspace_type_last_used", "workspace_id", "memory_type", "last_used_at"),
        Index("ix_memories_workspace_scope_last_used", "workspace_id", "scope", "last_used_at"),
    )

    workspace_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
    )
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    scope: Mapped[str] = mapped_column(String(32), default="workspace", server_default=text("'workspace'"))
    memory_type: Mapped[str] = mapped_column(
        String(32),
        default="project_fact",
        server_default=text("'project_fact'"),
    )
    key: Mapped[str] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(255), default="system", server_default=text("'system'"))
    confidence: Mapped[float] = mapped_column(Float, default=0.5, server_default=text("0.5"))
    content: Mapped[str] = mapped_column(Text)
    tags_json: Mapped[list[str]] = mapped_column(
        "tags",
        JSON_VARIANT,
        default=list,
        server_default=EMPTY_JSON_ARRAY,
    )
    embedding_vector: Mapped[list[float] | None] = mapped_column(JSON_VARIANT, nullable=True)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSON_VARIANT,
        default=dict,
        server_default=EMPTY_JSON_OBJECT,
    )

    workspace: Mapped[Workspace | None] = relationship(back_populates="memories")
    user: Mapped[User | None] = relationship(back_populates="memories")
    task: Mapped[Task | None] = relationship(back_populates="memories")


class Skill(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "skills"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", "version", name="uq_skills_workspace_name_version"),
        Index("ix_skills_workspace_status_name", "workspace_id", "status", "name"),
    )

    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    yaml_definition: Mapped[str] = mapped_column(Text)
    source_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    triggers_json: Mapped[list[str]] = mapped_column(
        "triggers",
        JSON_VARIANT,
        default=list,
        server_default=EMPTY_JSON_ARRAY,
    )
    inputs_json: Mapped[list[dict[str, Any]]] = mapped_column(
        "inputs",
        JSON_VARIANT,
        default=list,
        server_default=EMPTY_JSON_ARRAY,
    )
    procedure_json: Mapped[list[Any]] = mapped_column(
        "procedure",
        JSON_VARIANT,
        default=list,
        server_default=EMPTY_JSON_ARRAY,
    )
    tools_allowed_json: Mapped[list[str]] = mapped_column(
        "tools_allowed",
        JSON_VARIANT,
        default=list,
        server_default=EMPTY_JSON_ARRAY,
    )
    risk_notes_json: Mapped[list[str]] = mapped_column(
        "risk_notes",
        JSON_VARIANT,
        default=list,
        server_default=EMPTY_JSON_ARRAY,
    )
    failure_modes_json: Mapped[list[str]] = mapped_column(
        "failure_modes",
        JSON_VARIANT,
        default=list,
        server_default=EMPTY_JSON_ARRAY,
    )
    evaluation_checklist_json: Mapped[list[str]] = mapped_column(
        "evaluation_checklist",
        JSON_VARIANT,
        default=list,
        server_default=EMPTY_JSON_ARRAY,
    )
    status: Mapped[str] = mapped_column(String(32), default="active", server_default=text("'active'"))
    approved_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSON_VARIANT,
        default=dict,
        server_default=EMPTY_JSON_OBJECT,
    )

    workspace: Mapped[Workspace] = relationship(back_populates="skills")
    approved_by: Mapped[User | None] = relationship(
        back_populates="approved_skills",
        foreign_keys=[approved_by_user_id],
    )


class SkillProposal(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "skill_proposals"
    __table_args__ = (
        Index("ix_skill_proposals_workspace_status_created", "workspace_id", "status", "created_at"),
        UniqueConstraint(
            "workspace_id",
            "name",
            "version",
            "content_hash",
            name="uq_skill_proposals_workspace_name_version_hash",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    base_skill_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("skills.id", ondelete="SET NULL"),
        nullable=True,
    )
    applied_skill_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("skills.id", ondelete="SET NULL"),
        nullable=True,
    )
    requested_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reviewed_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(255))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    change_type: Mapped[str] = mapped_column(String(32), default="create", server_default=text("'create'"))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    yaml_definition: Mapped[str] = mapped_column(Text)
    source_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    triggers_json: Mapped[list[str]] = mapped_column(
        "triggers",
        JSON_VARIANT,
        default=list,
        server_default=EMPTY_JSON_ARRAY,
    )
    inputs_json: Mapped[list[dict[str, Any]]] = mapped_column(
        "inputs",
        JSON_VARIANT,
        default=list,
        server_default=EMPTY_JSON_ARRAY,
    )
    procedure_json: Mapped[list[Any]] = mapped_column(
        "procedure",
        JSON_VARIANT,
        default=list,
        server_default=EMPTY_JSON_ARRAY,
    )
    tools_allowed_json: Mapped[list[str]] = mapped_column(
        "tools_allowed",
        JSON_VARIANT,
        default=list,
        server_default=EMPTY_JSON_ARRAY,
    )
    risk_notes_json: Mapped[list[str]] = mapped_column(
        "risk_notes",
        JSON_VARIANT,
        default=list,
        server_default=EMPTY_JSON_ARRAY,
    )
    failure_modes_json: Mapped[list[str]] = mapped_column(
        "failure_modes",
        JSON_VARIANT,
        default=list,
        server_default=EMPTY_JSON_ARRAY,
    )
    evaluation_checklist_json: Mapped[list[str]] = mapped_column(
        "evaluation_checklist",
        JSON_VARIANT,
        default=list,
        server_default=EMPTY_JSON_ARRAY,
    )
    status: Mapped[str] = mapped_column(String(32), default="pending", server_default=text("'pending'"))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSON_VARIANT,
        default=dict,
        server_default=EMPTY_JSON_OBJECT,
    )

    workspace: Mapped[Workspace] = relationship(back_populates="skill_proposals")
    base_skill: Mapped[Skill | None] = relationship(foreign_keys=[base_skill_id])
    applied_skill: Mapped[Skill | None] = relationship(foreign_keys=[applied_skill_id])
    requested_by: Mapped[User | None] = relationship(
        back_populates="skill_proposals_requested",
        foreign_keys=[requested_by_user_id],
    )
    reviewed_by: Mapped[User | None] = relationship(
        back_populates="skill_proposals_reviewed",
        foreign_keys=[reviewed_by_user_id],
    )
    approvals: Mapped[list["Approval"]] = relationship(back_populates="skill_proposal")


class Approval(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "approvals"
    __table_args__ = (Index("ix_approvals_workspace_status_created", "workspace_id", "status", "created_at"),)

    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    skill_proposal_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("skill_proposals.id", ondelete="SET NULL"),
        nullable=True,
    )
    tool_call_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tool_calls.id", ondelete="SET NULL"),
        nullable=True,
    )
    requested_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reviewed_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(32), default="pending", server_default=text("'pending'"))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSON_VARIANT,
        default=dict,
        server_default=EMPTY_JSON_OBJECT,
    )

    workspace: Mapped[Workspace] = relationship(back_populates="approvals")
    task: Mapped[Task | None] = relationship(back_populates="approvals")
    skill_proposal: Mapped[SkillProposal | None] = relationship(back_populates="approvals")
    tool_call: Mapped[ToolCall | None] = relationship(back_populates="approvals")
    requested_by: Mapped[User | None] = relationship(
        back_populates="approval_requests",
        foreign_keys=[requested_by_user_id],
    )
    reviewed_by: Mapped[User | None] = relationship(
        back_populates="approvals_reviewed",
        foreign_keys=[reviewed_by_user_id],
    )


class ModelCall(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "model_calls"
    __table_args__ = (
        Index("ix_model_calls_task_run_created", "task_run_id", "created_at"),
        Index("ix_model_calls_provider_model_created", "provider", "model_name", "created_at"),
    )

    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    task_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("task_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    message_id: Mapped[UUID | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)
    provider: Mapped[str] = mapped_column(String(64))
    model_name: Mapped[str] = mapped_column(String(128))
    request_json: Mapped[dict[str, Any]] = mapped_column(
        "request",
        JSON_VARIANT,
        default=dict,
        server_default=EMPTY_JSON_OBJECT,
    )
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="started", server_default=text("'started'"))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task: Mapped[Task | None] = relationship(back_populates="model_calls")
    task_run: Mapped[TaskRun | None] = relationship(back_populates="model_calls")
    message: Mapped[Message | None] = relationship(back_populates="model_calls")
