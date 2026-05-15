from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from db.memory_service import ContextMemory, MemoryCreateRequest, MemorySearchRequest, build_task_context, save_memory, search_memories
from db.models import Memory, Task, User, Workspace
from db.repositories import create_task


pytestmark = pytest.mark.asyncio


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@pytest_asyncio.fixture
async def task(session: AsyncSession, workspace: Workspace, user: User) -> Task:
    return await create_task(
        session,
        workspace_id=workspace.id,
        title="Investigate deployment failure",
        description="Review the blue green rollout and the restart policy for the API service.",
        created_by_user_id=user.id,
        metadata_json={"tags": ["deploy", "api"]},
    )


async def test_save_memory_persists_structured_fields(
    session: AsyncSession,
    workspace: Workspace,
    user: User,
    task: Task,
) -> None:
    memory = await save_memory(
        session,
        MemoryCreateRequest(
            workspace_id=workspace.id,
            user_id=user.id,
            task_id=task.id,
            memory_type="task_summary",
            scope="task",
            source="telegram",
            confidence=0.9,
            content="Deployment investigation opened after the API rollout failed health checks.",
            tags=("deploy", "incident"),
            embedding_vector=(0.12, 0.34, 0.56),
            metadata_json={"channel": "telegram"},
        ),
    )

    assert memory.memory_type == "task_summary"
    assert memory.scope == "task"
    assert memory.source == "telegram"
    assert memory.confidence == pytest.approx(0.9)
    assert memory.tags_json == ["deploy", "incident"]
    assert memory.embedding_vector == [0.12, 0.34, 0.56]
    assert memory.last_used_at is not None
    assert memory.created_at is not None


async def test_search_memories_filters_by_workspace_type_text_and_tags(
    session: AsyncSession,
    workspace: Workspace,
    user: User,
) -> None:
    other_user = User(username="memory-other-user", display_name="Other User")
    session.add(other_user)
    await session.commit()
    await session.refresh(other_user)

    other_workspace = Workspace(name="Other Workspace", slug="other-memory-workspace", owner_user_id=other_user.id)
    session.add(other_workspace)
    await session.commit()
    await session.refresh(other_workspace)

    await save_memory(
        session,
        MemoryCreateRequest(
            workspace_id=workspace.id,
            memory_type="warning",
            scope="workspace",
            source="system",
            confidence=0.95,
            content="Do not restart the database during a deployment window.",
            tags=("deploy", "database", "warning"),
        ),
    )
    await save_memory(
        session,
        MemoryCreateRequest(
            workspace_id=workspace.id,
            memory_type="user_preference",
            scope="user",
            source="manual",
            confidence=0.7,
            content="The user prefers concise status updates.",
            tags=("communication",),
        ),
    )
    await save_memory(
        session,
        MemoryCreateRequest(
            workspace_id=other_workspace.id,
            memory_type="warning",
            scope="workspace",
            source="system",
            confidence=0.8,
            content="Deployments in the other workspace use canaries.",
            tags=("deploy",),
        ),
    )

    memories = await search_memories(
        session,
        MemorySearchRequest(
            workspace_id=workspace.id,
            memory_types=("warning",),
            text_query="database deploy",
            tags=("warning", "deploy"),
            limit=10,
        ),
    )

    assert len(memories) == 1
    assert memories[0].memory_type == "warning"
    assert memories[0].workspace_id == workspace.id
    assert "database" in memories[0].content.lower()


async def test_build_task_context_returns_relevant_memories_and_updates_last_used_at(
    session: AsyncSession,
    workspace: Workspace,
    task: Task,
) -> None:
    warning_memory = await save_memory(
        session,
        MemoryCreateRequest(
            workspace_id=workspace.id,
            task_id=task.id,
            memory_type="warning",
            scope="task",
            source="system",
            confidence=0.95,
            content="Never restart the API database during a blue green deployment.",
            tags=("deploy", "api", "database"),
        ),
    )
    project_fact = await save_memory(
        session,
        MemoryCreateRequest(
            workspace_id=workspace.id,
            memory_type="project_fact",
            scope="workspace",
            source="runbook",
            confidence=0.8,
            content="Blue green rollouts require a warm standby API service before traffic cutover.",
            tags=("deploy", "api"),
        ),
    )
    await save_memory(
        session,
        MemoryCreateRequest(
            workspace_id=workspace.id,
            memory_type="user_preference",
            scope="user",
            source="manual",
            confidence=1.0,
            content="The user prefers bullet-point updates.",
            tags=("communication",),
        ),
    )

    before_last_used = warning_memory.last_used_at
    context = await build_task_context(session, task_id=task.id, limit=2)

    assert len(context) == 2
    assert all(isinstance(item, ContextMemory) for item in context)
    returned_ids = {item.memory_id for item in context}
    assert warning_memory.id in returned_ids
    assert project_fact.id in returned_ids

    refreshed_warning = await session.get(Memory, warning_memory.id)
    assert refreshed_warning is not None
    assert _as_utc(refreshed_warning.last_used_at) >= _as_utc(before_last_used)
    assert _as_utc(refreshed_warning.last_used_at) <= datetime.now(timezone.utc)