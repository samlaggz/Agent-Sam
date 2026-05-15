import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import TaskRun, Workspace
from db.repositories import create_task
from db.task_queue import claim_next_task, complete_task, pause_task, reprioritize_task


pytestmark = pytest.mark.asyncio


async def test_create_task(session: AsyncSession, workspace: Workspace) -> None:
    task = await create_task(
        session,
        workspace_id=workspace.id,
        title="Draft plan",
        description="Create a first-pass plan for the workspace.",
    )

    assert task.title == "Draft plan"
    assert task.description == "Create a first-pass plan for the workspace."
    assert task.priority == "normal"
    assert task.status == "pending"
    assert task.parent_task_id is None


async def test_pick_highest_priority_task(session: AsyncSession, workspace: Workspace) -> None:
    await create_task(session, workspace_id=workspace.id, title="Low task", priority="low")
    await create_task(session, workspace_id=workspace.id, title="High task", priority="high")
    urgent_task = await create_task(session, workspace_id=workspace.id, title="Urgent task", priority="urgent")

    claimed_task = await claim_next_task(session, worker_name="worker-a")

    assert claimed_task is not None
    assert claimed_task.id == urgent_task.id
    assert claimed_task.status == "running"
    assert claimed_task.assigned_worker == "worker-a"

    task_run = await session.scalar(
        select(TaskRun).where(TaskRun.task_id == claimed_task.id).order_by(TaskRun.attempt_number.desc())
    )
    assert task_run is not None
    assert task_run.status == "running"
    assert task_run.worker_name == "worker-a"


async def test_reprioritize_task(session: AsyncSession, workspace: Workspace) -> None:
    task = await create_task(session, workspace_id=workspace.id, title="Backlog task", priority="backlog")

    updated_task = await reprioritize_task(session, task_id=task.id, priority="urgent")

    assert updated_task is not None
    assert updated_task.priority == "urgent"


async def test_pause_task(session: AsyncSession, workspace: Workspace) -> None:
    task = await create_task(session, workspace_id=workspace.id, title="Pause me", priority="high")
    await claim_next_task(session, worker_name="worker-pause")

    paused_task = await pause_task(session, task_id=task.id)

    assert paused_task is not None
    assert paused_task.status == "paused"
    assert paused_task.assigned_worker is None
    assert paused_task.completed_at is None

    task_run = await session.scalar(
        select(TaskRun).where(TaskRun.task_id == task.id).order_by(TaskRun.attempt_number.desc())
    )
    assert task_run is not None
    assert task_run.status == "paused"
    assert task_run.completed_at is not None


async def test_complete_task(session: AsyncSession, workspace: Workspace) -> None:
    task = await create_task(session, workspace_id=workspace.id, title="Complete me")
    await claim_next_task(session, worker_name="worker-complete")

    completed_task = await complete_task(session, task_id=task.id)

    assert completed_task is not None
    assert completed_task.status == "completed"
    assert completed_task.completed_at is not None
    assert completed_task.assigned_worker is None

    task_run = await session.scalar(
        select(TaskRun).where(TaskRun.task_id == task.id).order_by(TaskRun.attempt_number.desc())
    )
    assert task_run is not None
    assert task_run.status == "completed"
    assert task_run.completed_at is not None