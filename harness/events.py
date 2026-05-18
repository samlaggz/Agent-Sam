from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db import repositories
from db.models import AgentEvent


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    task_id: UUID | None = None
    run_id: UUID | None = None
    agent_slug: str | None = None
    event_type: str
    timestamp: datetime = Field(default_factory=_utc_now)
    sequence: int | None = None
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    parent_event_id: UUID | None = None


class UserMessageEvent(Event):
    event_type: Literal["user_message"] = "user_message"


class AgentThoughtEvent(Event):
    event_type: Literal["agent_thought"] = "agent_thought"


class ToolCallEvent(Event):
    event_type: Literal["tool_call"] = "tool_call"


class ToolResultEvent(Event):
    event_type: Literal["tool_result"] = "tool_result"


class FileEditEvent(Event):
    event_type: Literal["file_edit"] = "file_edit"


class ShellCommandEvent(Event):
    event_type: Literal["shell_command"] = "shell_command"


class BrowserActionEvent(Event):
    event_type: Literal["browser_action"] = "browser_action"


class ApprovalRequestedEvent(Event):
    event_type: Literal["approval_requested"] = "approval_requested"


class ApprovalResolvedEvent(Event):
    event_type: Literal["approval_resolved"] = "approval_resolved"


class ErrorEvent(Event):
    event_type: Literal["error"] = "error"


class RunSummaryEvent(Event):
    event_type: Literal["run_summary"] = "run_summary"


EVENT_MODELS = {
    "user_message": UserMessageEvent,
    "agent_thought": AgentThoughtEvent,
    "tool_call": ToolCallEvent,
    "tool_result": ToolResultEvent,
    "file_edit": FileEditEvent,
    "shell_command": ShellCommandEvent,
    "browser_action": BrowserActionEvent,
    "approval_requested": ApprovalRequestedEvent,
    "approval_resolved": ApprovalResolvedEvent,
    "error": ErrorEvent,
    "run_summary": RunSummaryEvent,
}


def parse_event(payload: dict[str, Any]) -> Event:
    event_type = str(payload.get("event_type", "")).strip().lower()
    model = EVENT_MODELS.get(event_type, Event)
    return model.model_validate(payload)


def agent_event_to_model(row: AgentEvent) -> Event:
    payload = {
        "id": row.id,
        "task_id": row.task_id,
        "run_id": row.agent_run_id,
        "agent_slug": row.agent_slug,
        "event_type": row.event_type,
        "timestamp": row.created_at,
        "sequence": row.sequence,
        "content": row.content,
        "metadata": dict(row.metadata_json or {}),
        "parent_event_id": row.parent_event_id,
    }
    return parse_event(payload)


class EventStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def append(self, event: Event) -> Event:
        async with self._session_factory() as session:
            row = await repositories.append_event(
                session,
                task_id=event.task_id,
                agent_run_id=event.run_id,
                agent_slug=event.agent_slug,
                event_type=event.event_type,
                content=event.content,
                metadata_json=dict(event.metadata),
                parent_event_id=event.parent_event_id,
                sequence=event.sequence,
            )
        return agent_event_to_model(row)

    async def list_events(self, *, task_id: UUID | None = None, run_id: UUID | None = None) -> list[Event]:
        async with self._session_factory() as session:
            rows = await repositories.list_events(session, task_id=task_id, agent_run_id=run_id)
        return [agent_event_to_model(row) for row in rows]

    async def export_jsonl(self, *, task_id: UUID | None = None, run_id: UUID | None = None) -> str:
        async with self._session_factory() as session:
            return await repositories.export_events_jsonl(session, task_id=task_id, agent_run_id=run_id)

    async def export_jsonl_to_path(
        self,
        destination: str | Path,
        *,
        task_id: UUID | None = None,
        run_id: UUID | None = None,
    ) -> Path:
        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_text(await self.export_jsonl(task_id=task_id, run_id=run_id), encoding="utf-8")
        return destination_path


def event_to_json(event: Event) -> str:
    return json.dumps(event.model_dump(mode="json"), default=str)