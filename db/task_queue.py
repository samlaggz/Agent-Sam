from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Task, TaskRun


TASK_PRIORITY_RANK = {
    "urgent": 0,
    "high": 1,
    "normal": 2,
    "low": 3,
    "backlog": 4,
}
TASK_PRIORITIES = tuple(TASK_PRIORITY_RANK)
PENDING_TASK_STATUSES = ("pending",)
ACTIVE_TASK_STATUSES = frozenset({"running"})
TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "cancelled"})
ALLOWED_TASK_STATUSES = frozenset(
    set(PENDING_TASK_STATUSES)
    | ACTIVE_TASK_STATUSES
    | {"paused"}
    | TERMINAL_TASK_STATUSES
)


def validate_priority(priority: str) -> str:
    if priority not in TASK_PRIORITIES:
        raise ValueError(f"Invalid task priority: {priority}")
    return priority


def validate_status(status: str) -> str:
    if status not in ALLOWED_TASK_STATUSES:
        raise ValueError(f"Invalid task status: {status}")
    return status


def priority_ordering():
    return case(TASK_PRIORITY_RANK, value=Task.priority, else_=len(TASK_PRIORITY_RANK))


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
    validate_priority(priority)
    validate_status(status)

    if parent_task_id is not None:
        parent_task = await session.get(Task, parent_task_id)
        if parent_task is None:
            raise ValueError("Parent task does not exist.")
        if parent_task.workspace_id != workspace_id:
            raise ValueError("Subtasks must belong to the same workspace as their parent task.")

    task = Task(
        workspace_id=workspace_id,
        title=title,
        description=description,
        priority=priority,
        status=status,
        parent_task_id=parent_task_id,
        assigned_worker=assigned_worker,
        created_by_user_id=created_by_user_id,
        metadata_json=metadata_json or {},
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def create_subtask(
    session: AsyncSession,
    *,
    parent_task_id: UUID,
    title: str,
    description: str | None = None,
    priority: str = "normal",
    status: str = "pending",
    assigned_worker: str | None = None,
    created_by_user_id: UUID | None = None,
    metadata_json: dict[str, Any] | None = None,
) -> Task:
    parent_task = await session.get(Task, parent_task_id)
    if parent_task is None:
        raise ValueError("Parent task does not exist.")

    return await create_task(
        session,
        workspace_id=parent_task.workspace_id,
        title=title,
        description=description,
        priority=priority,
        status=status,
        parent_task_id=parent_task.id,
        assigned_worker=assigned_worker,
        created_by_user_id=created_by_user_id,
        metadata_json=metadata_json,
    )


async def list_subtasks(session: AsyncSession, *, parent_task_id: UUID) -> list[Task]:
    statement = (
        select(Task)
        .where(Task.parent_task_id == parent_task_id)
        .order_by(priority_ordering(), Task.created_at.asc())
    )
    result = await session.execute(statement)
    return list(result.scalars().all())


async def list_task_queue(
    session: AsyncSession,
    *,
    workspace_id: UUID | None = None,
    statuses: Sequence[str] = PENDING_TASK_STATUSES,
    limit: int = 100,
) -> list[Task]:
    for status in statuses:
        validate_status(status)

    statement = select(Task).where(Task.status.in_(tuple(statuses)))
    if workspace_id is not None:
        statement = statement.where(Task.workspace_id == workspace_id)

    statement = statement.order_by(priority_ordering(), Task.created_at.asc()).limit(limit)
    result = await session.execute(statement)
    return list(result.scalars().all())


async def _next_attempt_number(session: AsyncSession, *, task_id: UUID) -> int:
    statement = select(func.coalesce(func.max(TaskRun.attempt_number), 0)).where(TaskRun.task_id == task_id)
    max_attempt_number = await session.scalar(statement)
    return int(max_attempt_number or 0) + 1


async def _latest_task_run(session: AsyncSession, *, task_id: UUID) -> TaskRun | None:
    statement = (
        select(TaskRun)
        .where(TaskRun.task_id == task_id)
        .order_by(TaskRun.attempt_number.desc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(statement)
    return result.scalar_one_or_none()


async def claim_next_task(
    session: AsyncSession,
    *,
    worker_name: str,
    workspace_id: UUID | None = None,
) -> Task | None:
    statement = select(Task).where(Task.status.in_(PENDING_TASK_STATUSES))
    if workspace_id is not None:
        statement = statement.where(Task.workspace_id == workspace_id)

    statement = (
        statement.order_by(priority_ordering(), Task.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(statement)
    task = result.scalar_one_or_none()
    if task is None:
        return None

    now = datetime.now(timezone.utc)
    task.status = "running"
    task.assigned_worker = worker_name
    if task.started_at is None:
        task.started_at = now
    task.completed_at = None

    session.add(
        TaskRun(
            task_id=task.id,
            worker_name=worker_name,
            attempt_number=await _next_attempt_number(session, task_id=task.id),
            status="running",
            started_at=now,
        )
    )

    await session.commit()
    await session.refresh(task)
    return task


async def update_task_status(
    session: AsyncSession,
    *,
    task_id: UUID,
    status: str,
    assigned_worker: str | None = None,
    error_text: str | None = None,
) -> Task | None:
    validate_status(status)

    statement = select(Task).where(Task.id == task_id).with_for_update(skip_locked=True)
    result = await session.execute(statement)
    task = result.scalar_one_or_none()
    if task is None:
        return None

    now = datetime.now(timezone.utc)
    task.status = status

    if status == "running":
        task.assigned_worker = assigned_worker or task.assigned_worker
        if task.started_at is None:
            task.started_at = now
        task.completed_at = None
    elif status in PENDING_TASK_STATUSES or status == "paused":
        task.assigned_worker = None
        task.completed_at = None
    else:
        task.assigned_worker = None
        task.completed_at = now

    task_run = await _latest_task_run(session, task_id=task.id)
    if task_run is not None:
        task_run.status = status
        if status in ACTIVE_TASK_STATUSES and task_run.started_at is None:
            task_run.started_at = now
        if status in TERMINAL_TASK_STATUSES or status == "paused":
            task_run.completed_at = now
        if error_text is not None:
            task_run.error_text = error_text

    await session.commit()
    await session.refresh(task)
    return task


async def reprioritize_task(session: AsyncSession, *, task_id: UUID, priority: str) -> Task | None:
    validate_priority(priority)

    statement = select(Task).where(Task.id == task_id).with_for_update(skip_locked=True)
    result = await session.execute(statement)
    task = result.scalar_one_or_none()
    if task is None:
        return None

    task.priority = priority
    await session.commit()
    await session.refresh(task)
    return task


async def pause_task(session: AsyncSession, *, task_id: UUID) -> Task | None:
    return await update_task_status(session, task_id=task_id, status="paused")


async def resume_task(session: AsyncSession, *, task_id: UUID) -> Task | None:
    return await update_task_status(session, task_id=task_id, status="pending")


async def cancel_task(session: AsyncSession, *, task_id: UUID) -> Task | None:
    return await update_task_status(session, task_id=task_id, status="cancelled")


async def complete_task(session: AsyncSession, *, task_id: UUID) -> Task | None:
    return await update_task_status(session, task_id=task_id, status="completed")


async def fail_task(session: AsyncSession, *, task_id: UUID, error_text: str | None = None) -> Task | None:
    return await update_task_status(session, task_id=task_id, status="failed", error_text=error_text)


async def get_queue_status(session: AsyncSession, *, workspace_id: UUID | None = None) -> dict[str, Any]:
    filters = []
    if workspace_id is not None:
        filters.append(Task.workspace_id == workspace_id)

    status_statement = select(Task.status, func.count(Task.id)).group_by(Task.status)
    priority_statement = select(Task.priority, func.count(Task.id)).group_by(Task.priority)
    total_statement = select(func.count(Task.id))

    if filters:
        status_statement = status_statement.where(*filters)
        priority_statement = priority_statement.where(*filters)
        total_statement = total_statement.where(*filters)

    total = await session.scalar(total_statement)
    status_rows = await session.execute(status_statement)
    priority_rows = await session.execute(priority_statement)

    by_status = {status: count for status, count in status_rows.all()}
    by_priority = {priority: count for priority, count in priority_rows.all()}

    return {
        "total": int(total or 0),
        "pending": by_status.get("pending", 0),
        "running": by_status.get("running", 0),
        "paused": by_status.get("paused", 0),
        "by_status": by_status,
        "by_priority": by_priority,
    }