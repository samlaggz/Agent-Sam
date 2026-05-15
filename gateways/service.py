from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from db.models import Approval, Message, Task, ToolCall, User, Workspace
from db.task_queue import (
    TASK_PRIORITIES,
    cancel_task,
    create_task as create_task_request,
    get_queue_status,
    list_subtasks,
    list_task_queue,
    pause_task,
    reprioritize_task,
    resume_task,
)


MAX_TASK_TITLE_LENGTH = 80


@dataclass(frozen=True)
class GatewayContext:
    workspace_id: UUID
    user_id: UUID


@dataclass(frozen=True)
class IncomingGatewayMessage:
    text: str
    source: str
    source_user_id: str | None = None
    source_chat_id: str | None = None
    source_message_id: str | None = None
    source_username: str | None = None


class AgentGatewayService:
    def __init__(self, settings: Settings, session_factory: async_sessionmaker[AsyncSession]) -> None:
        if settings.default_workspace_id is None:
            raise ValueError("DEFAULT_WORKSPACE_ID must be set for gateway requests.")
        if settings.default_user_id is None:
            raise ValueError("DEFAULT_USER_ID must be set for gateway requests.")

        self._settings = settings
        self._session_factory = session_factory
        self._default_workspace_id = settings.default_workspace_id
        self._default_user_id = settings.default_user_id

    async def handle_text_message(self, incoming: IncomingGatewayMessage) -> str:
        async with self._session_factory() as session:
            context = await self._resolve_context(session)
            message = await self._save_incoming_message(session, context, incoming)
            task = await self._create_task_from_text(session, context, incoming, message)
            return self._format_task_confirmation(task)

    async def handle_command(
        self,
        command: str,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> str:
        async with self._session_factory() as session:
            context = await self._resolve_context(session)
            message = await self._save_incoming_message(
                session,
                context,
                incoming,
                metadata_extra={"command": command, "arguments": arguments},
            )

            handlers = {
                "start": self._handle_start_command,
                "new": self._handle_new_command,
                "status": self._handle_status_command,
                "queue": self._handle_queue_command,
                "prioritize": self._handle_prioritize_command,
                "pause": self._handle_pause_command,
                "resume": self._handle_resume_command,
                "approve": self._handle_approve_command,
                "cancel": self._handle_cancel_command,
            }
            handler = handlers.get(command)
            if handler is None:
                return self._help_text()

            return await handler(session, context, message, incoming, arguments)

    async def _handle_start_command(
        self,
        session: AsyncSession,
        context: GatewayContext,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> str:
        del session, context, message, incoming, arguments
        return self._help_text()

    async def _handle_new_command(
        self,
        session: AsyncSession,
        context: GatewayContext,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> str:
        if not arguments:
            return "Usage: /new <task description>"

        task_text = " ".join(arguments).strip()
        task = await self._create_task_from_text(session, context, incoming, message, task_text=task_text)
        return self._format_task_confirmation(task)

    async def _handle_status_command(
        self,
        session: AsyncSession,
        context: GatewayContext,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> str:
        del incoming

        if not arguments:
            return "Usage: /status <task_id>"

        task_id = self._parse_uuid(arguments[0])
        if task_id is None:
            return "Task IDs must be valid UUID values."

        task = await self._load_task(session, context, task_id)
        if task is None:
            return "Task not found in the default workspace."

        subtasks = await list_subtasks(session, parent_task_id=task.id)
        await self._link_message_to_task(session, message, task.id)
        return self._format_task_status(task, len(subtasks))

    async def _handle_queue_command(
        self,
        session: AsyncSession,
        context: GatewayContext,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> str:
        del message, incoming, arguments

        queue_status = await get_queue_status(session, workspace_id=context.workspace_id)
        pending_tasks = await list_task_queue(session, workspace_id=context.workspace_id, limit=5)
        return self._format_queue_status(queue_status, pending_tasks)

    async def _handle_prioritize_command(
        self,
        session: AsyncSession,
        context: GatewayContext,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> str:
        del incoming

        if len(arguments) != 2:
            return (
                "Usage: /prioritize <task_id> <priority>\n"
                f"Allowed priorities: {', '.join(TASK_PRIORITIES)}"
            )

        task_id = self._parse_uuid(arguments[0])
        priority = arguments[1].lower()
        if task_id is None:
            return "Task IDs must be valid UUID values."
        if priority not in TASK_PRIORITIES:
            return f"Priority must be one of: {', '.join(TASK_PRIORITIES)}"

        task = await self._load_task(session, context, task_id)
        if task is None:
            return "Task not found in the default workspace."

        updated_task = await reprioritize_task(session, task_id=task.id, priority=priority)
        await self._link_message_to_task(session, message, task.id)
        return f"Task {updated_task.id} reprioritized to {updated_task.priority}."

    async def _handle_pause_command(
        self,
        session: AsyncSession,
        context: GatewayContext,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> str:
        del incoming
        return await self._update_task_state_command(
            session,
            context,
            message,
            arguments,
            usage="Usage: /pause <task_id>",
            action_label="paused",
            action=pause_task,
        )

    async def _handle_resume_command(
        self,
        session: AsyncSession,
        context: GatewayContext,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> str:
        del incoming
        return await self._update_task_state_command(
            session,
            context,
            message,
            arguments,
            usage="Usage: /resume <task_id>",
            action_label="resumed",
            action=resume_task,
        )

    async def _handle_cancel_command(
        self,
        session: AsyncSession,
        context: GatewayContext,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> str:
        del incoming
        return await self._update_task_state_command(
            session,
            context,
            message,
            arguments,
            usage="Usage: /cancel <task_id>",
            action_label="cancelled",
            action=cancel_task,
        )

    async def _handle_approve_command(
        self,
        session: AsyncSession,
        context: GatewayContext,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> str:
        del incoming

        if not arguments:
            return "Usage: /approve <approval_id>"

        approval_id = self._parse_uuid(arguments[0])
        if approval_id is None:
            return "Approval IDs must be valid UUID values."

        approval = await self._load_approval(session, context, approval_id)
        if approval is None:
            return "Approval not found in the default workspace."

        approval.status = "approved"
        approval.reviewed_by_user_id = context.user_id
        approval.reviewed_at = datetime.now(timezone.utc)

        if approval.tool_call_id is not None:
            tool_call = await session.get(ToolCall, approval.tool_call_id)
            if tool_call is not None:
                tool_call.approved_by_user = True

        await session.commit()

        if approval.task_id is not None:
            await self._link_message_to_task(session, message, approval.task_id)

        return f"Approval {approval.id} marked as approved."

    async def _update_task_state_command(
        self,
        session: AsyncSession,
        context: GatewayContext,
        message: Message,
        arguments: list[str],
        *,
        usage: str,
        action_label: str,
        action,
    ) -> str:
        if not arguments:
            return usage

        task_id = self._parse_uuid(arguments[0])
        if task_id is None:
            return "Task IDs must be valid UUID values."

        task = await self._load_task(session, context, task_id)
        if task is None:
            return "Task not found in the default workspace."

        updated_task = await action(session, task_id=task.id)
        await self._link_message_to_task(session, message, task.id)
        return f"Task {updated_task.id} {action_label}. Current status: {updated_task.status}."

    async def _resolve_context(self, session: AsyncSession) -> GatewayContext:
        workspace = await session.get(Workspace, self._default_workspace_id)
        if workspace is None:
            raise RuntimeError("DEFAULT_WORKSPACE_ID does not reference an existing workspace.")

        user = await session.get(User, self._default_user_id)
        if user is None:
            raise RuntimeError("DEFAULT_USER_ID does not reference an existing user.")

        return GatewayContext(workspace_id=workspace.id, user_id=user.id)

    async def _save_incoming_message(
        self,
        session: AsyncSession,
        context: GatewayContext,
        incoming: IncomingGatewayMessage,
        *,
        metadata_extra: dict[str, Any] | None = None,
    ) -> Message:
        message = Message(
            workspace_id=context.workspace_id,
            user_id=context.user_id,
            role="user",
            content=incoming.text,
            metadata_json=self._build_message_metadata(incoming, metadata_extra=metadata_extra),
        )
        session.add(message)
        await session.commit()
        await session.refresh(message)
        return message

    async def _create_task_from_text(
        self,
        session: AsyncSession,
        context: GatewayContext,
        incoming: IncomingGatewayMessage,
        message: Message,
        *,
        task_text: str | None = None,
    ) -> Task:
        normalized_text = (task_text or incoming.text).strip()
        if not normalized_text:
            raise ValueError("Cannot create a task from an empty message.")

        title = self._build_task_title(normalized_text)
        task = await create_task_request(
            session,
            workspace_id=context.workspace_id,
            title=title,
            description=normalized_text,
            created_by_user_id=context.user_id,
            metadata_json={
                "source": incoming.source,
                "source_message_id": incoming.source_message_id,
                "source_chat_id": incoming.source_chat_id,
                "source_user_id": incoming.source_user_id,
                "gateway_message_id": str(message.id),
            },
        )
        await self._link_message_to_task(session, message, task.id)
        return task

    async def _link_message_to_task(self, session: AsyncSession, message: Message, task_id: UUID) -> None:
        message.task_id = task_id
        await session.commit()
        await session.refresh(message)

    async def _load_task(self, session: AsyncSession, context: GatewayContext, task_id: UUID) -> Task | None:
        task = await session.get(Task, task_id)
        if task is None or task.workspace_id != context.workspace_id:
            return None
        return task

    async def _load_approval(
        self,
        session: AsyncSession,
        context: GatewayContext,
        approval_id: UUID,
    ) -> Approval | None:
        approval = await session.get(Approval, approval_id)
        if approval is None or approval.workspace_id != context.workspace_id:
            return None
        return approval

    def _build_message_metadata(
        self,
        incoming: IncomingGatewayMessage,
        *,
        metadata_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata = {
            "source": incoming.source,
            "source_user_id": incoming.source_user_id,
            "source_chat_id": incoming.source_chat_id,
            "source_message_id": incoming.source_message_id,
            "source_username": incoming.source_username,
        }
        if metadata_extra:
            metadata.update(metadata_extra)
        return metadata

    def _build_task_title(self, text: str) -> str:
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), text.strip())
        if len(first_line) <= MAX_TASK_TITLE_LENGTH:
            return first_line
        return f"{first_line[: MAX_TASK_TITLE_LENGTH - 3].rstrip()}..."

    def _format_task_confirmation(self, task: Task) -> str:
        return (
            "Task created.\n"
            f"ID: {task.id}\n"
            f"Title: {task.title}\n"
            f"Priority: {task.priority}\n"
            f"Status: {task.status}"
        )

    def _format_task_status(self, task: Task, subtask_count: int) -> str:
        assigned_worker = task.assigned_worker or "unassigned"
        parent_task = str(task.parent_task_id) if task.parent_task_id is not None else "none"
        return (
            f"Task {task.id}\n"
            f"Title: {task.title}\n"
            f"Status: {task.status}\n"
            f"Priority: {task.priority}\n"
            f"Assigned worker: {assigned_worker}\n"
            f"Parent task: {parent_task}\n"
            f"Subtasks: {subtask_count}"
        )

    def _format_queue_status(self, queue_status: dict[str, Any], pending_tasks: list[Task]) -> str:
        lines = [
            "Queue status",
            f"Total: {queue_status['total']}",
            f"Pending: {queue_status['pending']}",
            f"Running: {queue_status['running']}",
            f"Paused: {queue_status['paused']}",
        ]

        if pending_tasks:
            lines.append("")
            lines.append("Next pending tasks:")
            for task in pending_tasks:
                lines.append(f"- {task.priority}: {task.title} ({task.id})")

        return "\n".join(lines)

    def _help_text(self) -> str:
        return (
            "Agent Sam commands:\n"
            "/new <task description>\n"
            "/status <task_id>\n"
            "/queue\n"
            f"/prioritize <task_id> <{'|'.join(TASK_PRIORITIES)}>\n"
            "/pause <task_id>\n"
            "/resume <task_id>\n"
            "/approve <approval_id>\n"
            "/cancel <task_id>"
        )

    def _parse_uuid(self, raw_value: str) -> UUID | None:
        try:
            return UUID(raw_value)
        except ValueError:
            return None