from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Memory, Task


MEMORY_TYPES = (
    "user_preference",
    "project_fact",
    "task_summary",
    "skill_note",
    "server_fact",
    "decision",
    "warning",
)
TOKEN_PATTERN = re.compile(r"[a-z0-9_:-]{3,}")
MEMORY_TYPE_WEIGHTS = {
    "warning": 1.8,
    "decision": 1.5,
    "task_summary": 1.4,
    "project_fact": 1.2,
    "skill_note": 1.1,
    "user_preference": 0.9,
    "server_fact": 0.8,
}
SCOPE_WEIGHTS = {
    "task": 1.4,
    "workspace": 1.0,
    "user": 0.9,
    "server": 0.8,
    "global": 0.7,
}


@dataclass(frozen=True)
class MemoryCreateRequest:
    content: str
    source: str
    confidence: float
    scope: str
    memory_type: str
    workspace_id: UUID | None
    user_id: UUID | None = None
    task_id: UUID | None = None
    key: str | None = None
    tags: tuple[str, ...] = ()
    embedding_vector: tuple[float, ...] | None = None
    metadata_json: dict[str, object] | None = None


@dataclass(frozen=True)
class MemorySearchRequest:
    workspace_id: UUID | None = None
    memory_types: tuple[str, ...] = ()
    text_query: str | None = None
    tags: tuple[str, ...] = ()
    scope: str | None = None
    limit: int = 20


@dataclass(frozen=True)
class ContextMemory:
    memory_id: UUID
    memory_type: str
    scope: str
    content: str
    source: str
    confidence: float
    tags: tuple[str, ...]
    relevance: float


def validate_memory_type(memory_type: str) -> str:
    if memory_type not in MEMORY_TYPES:
        raise ValueError(f"Invalid memory type: {memory_type}")
    return memory_type


def normalize_tags(tags: tuple[str, ...] | list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        cleaned = tag.strip().lower()
        if not cleaned or cleaned in seen:
            continue
        normalized.append(cleaned)
        seen.add(cleaned)
    return normalized


def tokenize_text(text: str) -> set[str]:
    return set(TOKEN_PATTERN.findall(text.lower()))


async def save_memory(session: AsyncSession, request: MemoryCreateRequest) -> Memory:
    validate_memory_type(request.memory_type)
    if not request.content.strip():
        raise ValueError("Memory content cannot be empty.")
    if not request.source.strip():
        raise ValueError("Memory source cannot be empty.")
    if not 0.0 <= request.confidence <= 1.0:
        raise ValueError("Memory confidence must be between 0.0 and 1.0.")

    now = datetime.now(timezone.utc)
    memory = Memory(
        workspace_id=request.workspace_id,
        user_id=request.user_id,
        task_id=request.task_id,
        scope=request.scope,
        memory_type=request.memory_type,
        key=request.key or f"{request.memory_type}:{uuid4().hex[:12]}",
        source=request.source.strip(),
        confidence=request.confidence,
        content=request.content.strip(),
        tags_json=normalize_tags(list(request.tags)),
        embedding_vector=list(request.embedding_vector) if request.embedding_vector is not None else None,
        last_used_at=now,
        metadata_json=request.metadata_json or {},
    )
    session.add(memory)
    await session.commit()
    await session.refresh(memory)
    return memory


async def search_memories(session: AsyncSession, request: MemorySearchRequest) -> list[Memory]:
    if request.limit <= 0:
        return []

    memory_types = tuple(validate_memory_type(memory_type) for memory_type in request.memory_types)
    normalized_tags = normalize_tags(list(request.tags))
    query_tokens = tokenize_text(request.text_query or "")

    statement = select(Memory)
    if request.workspace_id is not None:
        statement = statement.where(Memory.workspace_id == request.workspace_id)
    if memory_types:
        statement = statement.where(Memory.memory_type.in_(memory_types))
    if request.scope is not None:
        statement = statement.where(Memory.scope == request.scope)
    if query_tokens:
        text_predicates = [func.lower(Memory.content).like(f"%{token}%") for token in query_tokens]
        statement = statement.where(or_(*text_predicates))

    fetch_limit = request.limit * 5 if normalized_tags else request.limit
    statement = statement.order_by(Memory.last_used_at.desc(), Memory.confidence.desc(), Memory.created_at.desc())
    statement = statement.limit(fetch_limit)
    result = await session.execute(statement)
    memories = list(result.scalars().all())

    if normalized_tags:
        requested = set(normalized_tags)
        memories = [
            memory
            for memory in memories
            if requested.issubset(set(normalize_tags(memory.tags_json or [])))
        ]

    return memories[: request.limit]


async def build_task_context(
    session: AsyncSession,
    *,
    task_id: UUID,
    limit: int = 8,
) -> list[ContextMemory]:
    if limit <= 0:
        return []

    task = await session.get(Task, task_id)
    if task is None:
        raise ValueError(f"Task {task_id} does not exist.")

    task_text = " ".join(part for part in [task.title, task.description or ""] if part).strip()
    task_tags = normalize_tags(list(task.metadata_json.get("tags", []))) if isinstance(task.metadata_json, dict) else []

    candidates = await search_memories(
        session,
        MemorySearchRequest(
            workspace_id=task.workspace_id,
            text_query=task_text or None,
            limit=max(limit * 4, 12),
        ),
    )
    if task_tags:
        tag_candidates = await search_memories(
            session,
            MemorySearchRequest(
                workspace_id=task.workspace_id,
                tags=tuple(task_tags),
                limit=max(limit * 4, 12),
            ),
        )
        candidate_map = {memory.id: memory for memory in candidates}
        for memory in tag_candidates:
            candidate_map[memory.id] = memory
        candidates = list(candidate_map.values())

    scored = sorted(
        (
            ContextMemory(
                memory_id=memory.id,
                memory_type=memory.memory_type,
                scope=memory.scope,
                content=memory.content,
                source=memory.source,
                confidence=memory.confidence,
                tags=tuple(memory.tags_json or []),
                relevance=_score_memory(memory, task_text=task_text, task_tags=task_tags, task_id=task.id),
            )
            for memory in candidates
        ),
        key=lambda item: item.relevance,
        reverse=True,
    )

    selected = [memory for memory in scored if memory.relevance > 0][:limit]
    if not selected:
        selected = scored[:limit]

    now = datetime.now(timezone.utc)
    selected_ids = {memory.memory_id for memory in selected}
    for memory in candidates:
        if memory.id in selected_ids:
            memory.last_used_at = now

    await session.commit()
    return selected


def _score_memory(memory: Memory, *, task_text: str, task_tags: list[str], task_id: UUID) -> float:
    task_tokens = tokenize_text(task_text)
    memory_tokens = tokenize_text(memory.content)
    text_overlap = len(task_tokens & memory_tokens)
    tag_overlap = len(set(task_tags) & set(normalize_tags(memory.tags_json or [])))
    type_weight = MEMORY_TYPE_WEIGHTS.get(memory.memory_type, 1.0)
    scope_weight = 1.8 if memory.task_id == task_id else SCOPE_WEIGHTS.get(memory.scope, 0.7)

    # Recency decay: recent memories score higher
    now = datetime.now(timezone.utc)
    recency_weight = 0.0
    try:
        if memory.last_used_at is not None:
            last_used = memory.last_used_at
            if last_used.tzinfo is None:
                last_used = last_used.replace(tzinfo=timezone.utc)
            age_hours = (now - last_used).total_seconds() / 3600
            recency_weight = max(0.0, 2.0 - (age_hours / 24))
        elif memory.created_at is not None:
            created = memory.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age_hours = (now - created).total_seconds() / 3600
            recency_weight = max(0.0, 1.5 - (age_hours / 48))
    except (TypeError, AttributeError):
        recency_weight = 0.5

    # Boost warnings and decisions (actionable memories)
    actionable_boost = 1.5 if memory.memory_type in ("warning", "decision") else 0.0

    return (
        (text_overlap * 3.0)
        + (tag_overlap * 2.0)
        + (memory.confidence * 2.0)
        + type_weight
        + scope_weight
        + recency_weight
        + actionable_boost
    )