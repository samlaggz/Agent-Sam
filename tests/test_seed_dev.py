import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.models import User, Workspace
from scripts.seed_dev import DEFAULT_DEV_USERNAME, DEFAULT_DEV_WORKSPACE_SLUG, seed_dev_data


pytestmark = pytest.mark.asyncio


async def test_seed_dev_data_is_idempotent(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first_result = await seed_dev_data(session)
    second_result = await seed_dev_data(session)

    assert first_result.user_id == second_result.user_id
    assert first_result.workspace_id == second_result.workspace_id
    assert first_result.created_user is True
    assert first_result.created_workspace is True
    assert second_result.created_user is False
    assert second_result.created_workspace is False

    async with session_factory() as verification_session:
        user_count = await verification_session.scalar(
            select(func.count()).select_from(User).where(User.username == DEFAULT_DEV_USERNAME)
        )
        workspace_count = await verification_session.scalar(
            select(func.count()).select_from(Workspace).where(Workspace.slug == DEFAULT_DEV_WORKSPACE_SLUG)
        )
        workspace = await verification_session.scalar(
            select(Workspace).where(Workspace.slug == DEFAULT_DEV_WORKSPACE_SLUG).limit(1)
        )

    assert user_count == 1
    assert workspace_count == 1
    assert workspace is not None
    assert workspace.owner_user_id == first_result.user_id