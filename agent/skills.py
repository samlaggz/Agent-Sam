from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from db.skill_service import search_relevant_skills, sync_skills_from_directory


@dataclass(frozen=True)
class SkillContext:
    name: str
    version: int
    source: str | None
    description: str
    excerpt: str
    score: float
    triggers: tuple[str, ...]
    tools_allowed: tuple[str, ...]


async def load_skill_context(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    query_text: str,
    skills_root: Path,
    limit: int = 4,
) -> list[SkillContext]:
    if skills_root.exists():
        await sync_skills_from_directory(session, workspace_id=workspace_id, skills_root=skills_root)

    matches = await search_relevant_skills(
        session,
        workspace_id=workspace_id,
        query_text=query_text,
        limit=limit,
    )
    return [
        SkillContext(
            name=match.name,
            version=match.version,
            source=match.source,
            description=match.description,
            excerpt=match.excerpt,
            score=match.score,
            triggers=match.triggers,
            tools_allowed=match.tools_allowed,
        )
        for match in matches
    ]