from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Task, ToolCall
from db.task_queue import create_task as create_task_record
from db.task_queue import list_task_queue as list_task_queue_entries
from db.task_queue import PENDING_TASK_STATUSES
from db.task_queue import update_task_status as set_task_status


async def create_task(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    title: str,
    description: str | None = None,
    priority: str = "normal",
    status: str = "pending",
    parent_task_id: UUID | None = None,
    assigned_worker: str | None = None,
    created_by_user_id: UUID | None = None,
    metadata_json: dict[str, Any] | None = None,
) -> Task:
    return await create_task_record(
        session,
        workspace_id=workspace_id,
        title=title,
        description=description,
        priority=priority,
        status=status,
        parent_task_id=parent_task_id,
        assigned_worker=assigned_worker,
        created_by_user_id=created_by_user_id,
        metadata_json=metadata_json,
    )


async def list_task_queue(
    session: AsyncSession,
    *,
    workspace_id: UUID | None = None,
    statuses: Sequence[str] = PENDING_TASK_STATUSES,
    limit: int = 100,
) -> list[Task]:
    return await list_task_queue_entries(
        session,
        workspace_id=workspace_id,
        statuses=statuses,
        limit=limit,
    )


async def update_task_status(
    session: AsyncSession,
    *,
    task_id: UUID,
    status: str,
    assigned_worker: str | None = None,
) -> Task | None:
    return await set_task_status(
        session,
        task_id=task_id,
        status=status,
        assigned_worker=assigned_worker,
    )


async def log_tool_call(
    session: AsyncSession,
    *,
    tool_name: str,
    input_payload: dict[str, Any] | None = None,
    output_text: str | None = None,
    stderr_text: str | None = None,
    duration_ms: int | None = None,
    status: str = "pending",
    risk_level: str = "safe",
    approved_by_user: bool = False,
    exit_code: int | None = None,
    task_id: UUID | None = None,
    task_run_id: UUID | None = None,
    message_id: UUID | None = None,
) -> ToolCall:
    tool_call = ToolCall(
        tool_name=tool_name,
        input_payload=input_payload or {},
        output_text=output_text,
        stderr_text=stderr_text,
        duration_ms=duration_ms,
        status=status,
        risk_level=risk_level,
        approved_by_user=approved_by_user,
        exit_code=exit_code,
        task_id=task_id,
        task_run_id=task_run_id,
        message_id=message_id,
    )
    session.add(tool_call)
    await session.commit()
    await session.refresh(tool_call)
    return tool_call