from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import User, Workspace
from db.session import AsyncSessionLocal


DEFAULT_DEV_USERNAME = "agent-sam-dev"
DEFAULT_DEV_DISPLAY_NAME = "Agent Sam Dev User"
DEFAULT_DEV_WORKSPACE_SLUG = "agent-sam-dev"
DEFAULT_DEV_WORKSPACE_NAME = "Agent Sam Dev Workspace"


@dataclass(frozen=True)
class DevSeedResult:
    user_id: UUID
    workspace_id: UUID
    created_user: bool
    created_workspace: bool


async def seed_dev_data(
    session: AsyncSession,
    *,
    username: str = DEFAULT_DEV_USERNAME,
    display_name: str = DEFAULT_DEV_DISPLAY_NAME,
    workspace_slug: str = DEFAULT_DEV_WORKSPACE_SLUG,
    workspace_name: str = DEFAULT_DEV_WORKSPACE_NAME,
) -> DevSeedResult:
    user = await session.scalar(select(User).where(User.username == username).limit(1))
    created_user = False
    if user is None:
        user = User(username=username, display_name=display_name)
        session.add(user)
        await session.flush()
        created_user = True

    workspace = await session.scalar(select(Workspace).where(Workspace.slug == workspace_slug).limit(1))
    created_workspace = False
    if workspace is None:
        workspace = Workspace(
            name=workspace_name,
            slug=workspace_slug,
            owner_user_id=user.id,
        )
        session.add(workspace)
        await session.flush()
        created_workspace = True
    elif workspace.owner_user_id is None:
        workspace.owner_user_id = user.id

    await session.commit()
    await session.refresh(user)
    await session.refresh(workspace)

    return DevSeedResult(
        user_id=user.id,
        workspace_id=workspace.id,
        created_user=created_user,
        created_workspace=created_workspace,
    )


async def main() -> None:
    async with AsyncSessionLocal() as session:
        result = await seed_dev_data(session)

    print("Development seed complete.")
    print(f"User: {'created' if result.created_user else 'reused'}")
    print(f"Workspace: {'created' if result.created_workspace else 'reused'}")
    print(f"DEFAULT_USER_ID={result.user_id}")
    print(f"DEFAULT_WORKSPACE_ID={result.workspace_id}")


if __name__ == "__main__":
    asyncio.run(main())