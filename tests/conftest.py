from collections.abc import AsyncIterator
from uuid import uuid4

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from db.base import Base
from db.models import User, Workspace


@pytest_asyncio.fixture
async def engine(tmp_path) -> AsyncIterator[AsyncEngine]:
    database_path = tmp_path / "task-queue.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", future=True)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    yield engine

    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def user(session: AsyncSession) -> User:
    user = User(username=f"user-{uuid4().hex[:8]}", display_name="Test User")
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@pytest_asyncio.fixture
async def workspace(session: AsyncSession, user: User) -> Workspace:
    workspace = Workspace(
        name="Queue Test Workspace",
        slug=f"queue-test-{uuid4()}",
        owner_user_id=user.id,
    )
    session.add(workspace)
    await session.commit()
    await session.refresh(workspace)
    return workspace