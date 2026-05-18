from __future__ import annotations

from collections.abc import Sequence
import json
import re
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import AgentEvent, Task, ToolCall
from db.task_queue import create_task as create_task_record
from db.task_queue import list_task_queue as list_task_queue_entries
from db.task_queue import PENDING_TASK_STATUSES
from db.task_queue import update_task_status as set_task_status


SECRET_VALUE_RE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|token|password|secret)\s*([:=])\s*([^\s\n]+)"
)
SENSITIVE_KEY_RE = re.compile(r"(?i)(api[_-]?key|access[_-]?token|token|password|secret)")


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


def _redact_secret_text(value: str) -> str:
    return SECRET_VALUE_RE.sub(lambda match: f"{match.group(1)}{match.group(2)} [REDACTED]", value)


def _redact_secret_data(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if SENSITIVE_KEY_RE.search(str(key)):
                redacted[str(key)] = "[REDACTED]"
            else:
                redacted[str(key)] = _redact_secret_data(item)
        return redacted
    if isinstance(value, list):
        return [_redact_secret_data(item) for item in value]
    if isinstance(value, str):
        return _redact_secret_text(value)
    return value


async def _next_agent_event_sequence(
    session: AsyncSession,
    *,
    task_id: UUID | None,
    agent_run_id: UUID | None,
) -> int:
    statement = select(func.coalesce(func.max(AgentEvent.sequence), 0))
    if agent_run_id is not None:
        statement = statement.where(AgentEvent.agent_run_id == agent_run_id)
    elif task_id is not None:
        statement = statement.where(AgentEvent.task_id == task_id)
    return int(await session.scalar(statement) or 0) + 1


async def append_event(
    session: AsyncSession,
    *,
    task_id: UUID | None,
    agent_run_id: UUID | None,
    agent_slug: str | None,
    event_type: str,
    content: str,
    metadata_json: dict[str, Any] | None = None,
    parent_event_id: UUID | None = None,
    sequence: int | None = None,
) -> AgentEvent:
    next_sequence = sequence or await _next_agent_event_sequence(
        session,
        task_id=task_id,
        agent_run_id=agent_run_id,
    )
    event = AgentEvent(
        task_id=task_id,
        agent_run_id=agent_run_id,
        agent_slug=agent_slug,
        sequence=next_sequence,
        event_type=event_type,
        content=_redact_secret_text(content),
        metadata_json=_redact_secret_data(metadata_json or {}),
        parent_event_id=parent_event_id,
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)
    return event


async def list_events(
    session: AsyncSession,
    *,
    task_id: UUID | None = None,
    agent_run_id: UUID | None = None,
    limit: int | None = None,
) -> list[AgentEvent]:
    statement = select(AgentEvent)
    if agent_run_id is not None:
        statement = statement.where(AgentEvent.agent_run_id == agent_run_id)
    elif task_id is not None:
        statement = statement.where(AgentEvent.task_id == task_id)
    statement = statement.order_by(AgentEvent.sequence.asc(), AgentEvent.created_at.asc())
    if limit is not None:
        statement = statement.limit(limit)
    result = await session.execute(statement)
    return list(result.scalars().all())


async def export_events_jsonl(
    session: AsyncSession,
    *,
    task_id: UUID | None = None,
    agent_run_id: UUID | None = None,
) -> str:
    rows = await list_events(session, task_id=task_id, agent_run_id=agent_run_id)
    lines = [
        json.dumps(
            {
                "id": str(row.id),
                "task_id": str(row.task_id) if row.task_id else None,
                "agent_run_id": str(row.agent_run_id) if row.agent_run_id else None,
                "agent_slug": row.agent_slug,
                "sequence": row.sequence,
                "event_type": row.event_type,
                "content": row.content,
                "metadata": row.metadata_json,
                "parent_event_id": str(row.parent_event_id) if row.parent_event_id else None,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            },
            default=str,
        )
        for row in rows
    ]
    return "\n".join(lines) + ("\n" if lines else "")