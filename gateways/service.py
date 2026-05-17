from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy import select

from agents.registry import get_agent_profile, list_agents
from agents.router import RouteRequest, RouterAgent
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
from gateways.base import GatewayAttachment, GatewayResponse, IncomingGatewayMessage
from services.agent_learning_service import approve_skill_proposal, approve_sub_agent_proposal
from services.budget_service import estimate_model_cost_level
from services.model_router import ModelRouter


MAX_TASK_TITLE_LENGTH = 80
logger = logging.getLogger(__name__)


class AgentGatewayService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._router_agent = RouterAgent()
        self._model_router = ModelRouter(Settings(), session_factory)

    async def handle_incoming_message(self, incoming: IncomingGatewayMessage) -> GatewayResponse:
        normalized_text = incoming.text.strip()
        if not normalized_text:
            return GatewayResponse(text="Empty messages cannot be processed.")

        command, arguments = self._parse_command(normalized_text)

        async with self._session_factory() as session:
            await self._resolve_context(
                session,
                workspace_id=incoming.workspace_id,
                user_id=incoming.user_id,
            )
            metadata_extra = None
            if command is not None:
                metadata_extra = {"command": command, "arguments": arguments}
            message = await self._save_incoming_message(session, incoming, metadata_extra=metadata_extra)

            if command is None:
                return await self._handle_text_message(session, message, incoming)
            return await self._handle_command(session, message, incoming, command, arguments)

    async def _handle_command(
        self,
        session: AsyncSession,
        message: Message,
        incoming: IncomingGatewayMessage,
        command: str,
        arguments: list[str],
    ) -> GatewayResponse:
        handlers = {
            "start": self._handle_start_command,
            "help": self._handle_help_command,
            "new": self._handle_new_command,
            "status": self._handle_status_command,
            "queue": self._handle_queue_command,
            "prioritize": self._handle_prioritize_command,
            "pause": self._handle_pause_command,
            "resume": self._handle_resume_command,
            "approve": self._handle_approve_command,
            "cancel": self._handle_cancel_command,
            "agents": self._handle_agents_command,
            "agent": self._handle_agent_command,
            "route": self._handle_route_command,
            "models": self._handle_models_command,
            "budget": self._handle_budget_command,
            "skills": self._handle_skills_command,
            "subagents": self._handle_subagents_command,
        }
        handler = handlers.get(command)
        if handler is None:
            return GatewayResponse(text=self._help_text())

        return await handler(session, message, incoming, arguments)

    async def _handle_text_message(
        self,
        session: AsyncSession,
        message: Message,
        incoming: IncomingGatewayMessage,
    ) -> GatewayResponse:
        task = await self._create_task_from_text(session, incoming, message)
        return GatewayResponse(text=self._format_task_confirmation(task), task_id=task.id)

    async def _handle_start_command(
        self,
        session: AsyncSession,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> GatewayResponse:
        del session, message, incoming, arguments
        return GatewayResponse(text=self._help_text())

    async def _handle_help_command(
        self,
        session: AsyncSession,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> GatewayResponse:
        del session, message, incoming, arguments
        return GatewayResponse(text=self._help_text())

    async def _handle_new_command(
        self,
        session: AsyncSession,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> GatewayResponse:
        if not arguments:
            return GatewayResponse(text="Usage: /new <task description>")

        task_text = " ".join(arguments).strip()
        task = await self._create_task_from_text(session, incoming, message, task_text=task_text)
        return GatewayResponse(text=self._format_task_confirmation(task), task_id=task.id)

    async def _handle_status_command(
        self,
        session: AsyncSession,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> GatewayResponse:
        del incoming

        if not arguments:
            return GatewayResponse(text="Usage: /status <task_id>")

        task_id = self._parse_uuid(arguments[0])
        if task_id is None:
            return GatewayResponse(text="Task IDs must be valid UUID values.")

        task = await self._load_task(session, workspace_id=message.workspace_id, task_id=task_id)
        if task is None:
            return GatewayResponse(text="Task not found in the selected workspace.")

        subtasks = await list_subtasks(session, parent_task_id=task.id)
        await self._link_message_to_task(session, message, task.id)
        return GatewayResponse(text=self._format_task_status(task, len(subtasks)), task_id=task.id)

    async def _handle_queue_command(
        self,
        session: AsyncSession,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> GatewayResponse:
        del message, arguments

        queue_status = await get_queue_status(session, workspace_id=incoming.workspace_id)
        pending_tasks = await list_task_queue(session, workspace_id=incoming.workspace_id, limit=5)
        return GatewayResponse(text=self._format_queue_status(queue_status, pending_tasks))

    async def _handle_prioritize_command(
        self,
        session: AsyncSession,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> GatewayResponse:
        del incoming

        if len(arguments) != 2:
            return GatewayResponse(
                text=(
                    "Usage: /prioritize <task_id> <priority>\n"
                    f"Allowed priorities: {', '.join(TASK_PRIORITIES)}"
                )
            )

        task_id = self._parse_uuid(arguments[0])
        priority = arguments[1].lower()
        if task_id is None:
            return GatewayResponse(text="Task IDs must be valid UUID values.")
        if priority not in TASK_PRIORITIES:
            return GatewayResponse(text=f"Priority must be one of: {', '.join(TASK_PRIORITIES)}")

        task = await self._load_task(session, workspace_id=message.workspace_id, task_id=task_id)
        if task is None:
            return GatewayResponse(text="Task not found in the selected workspace.")

        updated_task = await reprioritize_task(session, task_id=task.id, priority=priority)
        await self._link_message_to_task(session, message, task.id)
        return GatewayResponse(
            text=f"Task {updated_task.id} reprioritized to {updated_task.priority}.",
            task_id=updated_task.id,
        )

    async def _handle_pause_command(
        self,
        session: AsyncSession,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> GatewayResponse:
        del incoming
        return await self._update_task_state_command(
            session,
            message,
            arguments,
            usage="Usage: /pause <task_id>",
            action_label="paused",
            action=pause_task,
        )

    async def _handle_resume_command(
        self,
        session: AsyncSession,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> GatewayResponse:
        del incoming
        return await self._update_task_state_command(
            session,
            message,
            arguments,
            usage="Usage: /resume <task_id>",
            action_label="resumed",
            action=resume_task,
        )

    async def _handle_cancel_command(
        self,
        session: AsyncSession,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> GatewayResponse:
        del incoming
        return await self._update_task_state_command(
            session,
            message,
            arguments,
            usage="Usage: /cancel <task_id>",
            action_label="cancelled",
            action=cancel_task,
        )

    async def _handle_approve_command(
        self,
        session: AsyncSession,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> GatewayResponse:
        del incoming

        if not arguments:
            return GatewayResponse(text="Usage: /approve <approval_id>")

        approval_id = self._parse_uuid(arguments[0])
        if approval_id is None:
            return GatewayResponse(text="Approval IDs must be valid UUID values.")

        approval = await self._load_approval(session, workspace_id=message.workspace_id, approval_id=approval_id)
        if approval is None:
            return GatewayResponse(text="Approval not found in the selected workspace.")

        approval.status = "approved"
        approval.reviewed_by_user_id = message.user_id
        approval.reviewed_at = datetime.now(timezone.utc)

        if approval.tool_call_id is not None:
            tool_call = await session.get(ToolCall, approval.tool_call_id)
            if tool_call is not None:
                tool_call.approved_by_user = True

        await session.commit()

        if approval.task_id is not None:
            await self._link_message_to_task(session, message, approval.task_id)

        return GatewayResponse(
            text=f"Approval {approval.id} marked as approved.",
            task_id=approval.task_id,
            approval_id=approval.id,
        )

    async def _handle_agents_command(
        self,
        session: AsyncSession,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> GatewayResponse:
        del session, message, incoming, arguments
        profiles = list_agents()
        lines = ["Available agents:"]
        for profile in profiles:
            if profile.slug == "router_agent":
                continue
            lines.append(f"- {profile.slug}: {profile.description}")
        return GatewayResponse(text="\n".join(lines))

    async def _handle_agent_command(
        self,
        session: AsyncSession,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> GatewayResponse:
        del session, message, incoming
        if not arguments:
            return GatewayResponse(text="Usage: /agent <slug>")
        try:
            profile = get_agent_profile(arguments[0])
        except KeyError:
            return GatewayResponse(text="Unknown agent slug.")
        return GatewayResponse(
            text=(
                f"Agent: {profile.name}\n"
                f"Slug: {profile.slug}\n"
                f"Task types: {', '.join(profile.task_types)}\n"
                f"Default model: {profile.default_model}\n"
                f"Escalation model: {profile.escalation_model}\n"
                f"Tools: {', '.join(profile.tools_allowed)}\n"
                f"Max cost/task: ${profile.max_cost_per_task_usd:.2f}"
            )
        )

    async def _handle_route_command(
        self,
        session: AsyncSession,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> GatewayResponse:
        del incoming
        if not arguments:
            return GatewayResponse(text="Usage: /route <task_id>")
        task_id = self._parse_uuid(arguments[0])
        if task_id is None:
            return GatewayResponse(text="Task IDs must be valid UUID values.")
        task = await self._load_task(session, workspace_id=message.workspace_id, task_id=task_id)
        if task is None:
            return GatewayResponse(text="Task not found in the selected workspace.")
        decision = self._router_agent.route(
            RouteRequest(
                title=task.title,
                description=task.description or "",
                metadata=dict(task.metadata_json or {}),
            )
        )
        await self._link_message_to_task(session, message, task.id)
        return GatewayResponse(
            text=(
                f"Route for task {task.id}\n"
                f"Agent: {decision.agent_slug}\n"
                f"Model: {decision.model}\n"
                f"Confidence: {decision.confidence:.2f}\n"
                f"Cost level: {decision.estimated_cost_level}\n"
                f"Approval required: {decision.requires_human_approval}\n"
                f"Reason: {decision.reason}"
            ),
            task_id=task.id,
        )

    async def _handle_models_command(
        self,
        session: AsyncSession,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> GatewayResponse:
        del session, message, incoming, arguments
        models = self._model_router.list_configured_models(list_agents())
        return GatewayResponse(text="Configured models:\n" + "\n".join(f"- {model}" for model in models))

    async def _handle_budget_command(
        self,
        session: AsyncSession,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> GatewayResponse:
        del session, message, incoming, arguments
        settings = Settings()
        lines = [
            f"Global max cost/task: ${settings.max_cost_per_task_usd:.2f}",
            f"Daily model budget: ${settings.daily_model_budget_usd:.2f}",
        ]
        for profile in list_agents():
            if profile.slug == "router_agent":
                continue
            lines.append(
                f"- {profile.slug}: ${profile.max_cost_per_task_usd:.2f} default={profile.default_model} est={estimate_model_cost_level(profile.default_model):.2f}"
            )
        return GatewayResponse(text="Budget summary\n" + "\n".join(lines))

    async def _handle_skills_command(
        self,
        session: AsyncSession,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> GatewayResponse:
        del incoming
        if not arguments or arguments[0].lower() == "pending":
            result = await session.execute(
                select(Approval, Task)
                .outerjoin(Task, Approval.task_id == Task.id)
                .where(Approval.workspace_id == message.workspace_id, Approval.skill_proposal_id.is_not(None), Approval.status == "pending")
                .order_by(Approval.created_at.asc())
            )
            approvals = list(result.all())
            if not approvals:
                return GatewayResponse(text="No pending skill proposals.")
            lines = ["Pending skill proposals:"]
            for approval, _ in approvals:
                lines.append(f"- {approval.skill_proposal_id} (approval {approval.id})")
            return GatewayResponse(text="\n".join(lines))
        if len(arguments) == 2 and arguments[0].lower() == "approve":
            proposal_id = self._parse_uuid(arguments[1])
            if proposal_id is None:
                return GatewayResponse(text="Proposal IDs must be valid UUID values.")
            proposal = await approve_skill_proposal(session, proposal_id=proposal_id, reviewed_by_user_id=message.user_id)
            return GatewayResponse(text=f"Skill proposal {proposal.id} approved.")
        return GatewayResponse(text="Usage: /skills pending OR /skills approve <proposal_id>")

    async def _handle_subagents_command(
        self,
        session: AsyncSession,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> GatewayResponse:
        del incoming
        from db.models import SubAgentProposal

        if not arguments or arguments[0].lower() == "pending":
            proposals = list(
                (
                    await session.execute(
                        select(SubAgentProposal)
                        .where(SubAgentProposal.status == "pending")
                        .order_by(SubAgentProposal.created_at.asc())
                    )
                ).scalars()
            )
            if not proposals:
                return GatewayResponse(text="No pending sub-agent proposals.")
            lines = ["Pending sub-agent proposals:"]
            for proposal in proposals:
                lines.append(f"- {proposal.id}: {proposal.proposed_slug}")
            return GatewayResponse(text="\n".join(lines))
        if len(arguments) == 2 and arguments[0].lower() == "approve":
            proposal_id = self._parse_uuid(arguments[1])
            if proposal_id is None:
                return GatewayResponse(text="Proposal IDs must be valid UUID values.")
            proposal = await approve_sub_agent_proposal(session, proposal_id=proposal_id, reviewed_by_user_id=message.user_id)
            return GatewayResponse(text=f"Sub-agent proposal {proposal.id} approved.")
        return GatewayResponse(text="Usage: /subagents pending OR /subagents approve <proposal_id>")

    async def _update_task_state_command(
        self,
        session: AsyncSession,
        message: Message,
        arguments: list[str],
        *,
        usage: str,
        action_label: str,
        action,
    ) -> GatewayResponse:
        if not arguments:
            return GatewayResponse(text=usage)

        task_id = self._parse_uuid(arguments[0])
        if task_id is None:
            return GatewayResponse(text="Task IDs must be valid UUID values.")

        task = await self._load_task(session, workspace_id=message.workspace_id, task_id=task_id)
        if task is None:
            return GatewayResponse(text="Task not found in the selected workspace.")

        updated_task = await action(session, task_id=task.id)
        await self._link_message_to_task(session, message, task.id)
        return GatewayResponse(
            text=f"Task {updated_task.id} {action_label}. Current status: {updated_task.status}.",
            task_id=updated_task.id,
        )

    async def _resolve_context(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        user_id: UUID,
    ) -> None:
        workspace = await session.get(Workspace, workspace_id)
        if workspace is None:
            raise RuntimeError("Gateway workspace_id does not reference an existing workspace.")

        user = await session.get(User, user_id)
        if user is None:
            raise RuntimeError("Gateway user_id does not reference an existing user.")

    async def _save_incoming_message(
        self,
        session: AsyncSession,
        incoming: IncomingGatewayMessage,
        *,
        metadata_extra: dict[str, Any] | None = None,
    ) -> Message:
        message = Message(
            workspace_id=incoming.workspace_id,
            user_id=incoming.user_id,
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
            workspace_id=incoming.workspace_id,
            title=title,
            description=normalized_text,
            created_by_user_id=incoming.user_id,
            metadata_json={
                "gateway_name": incoming.gateway_name,
                "gateway_message_id": incoming.gateway_message_id,
                "gateway_chat_id": incoming.gateway_chat.gateway_chat_id,
                "gateway_user_id": incoming.gateway_user.gateway_user_id,
                "gateway_username": incoming.gateway_user.username,
                "gateway_message_record_id": str(message.id),
                "attachments": self._serialize_attachments(incoming.attachments),
            },
        )
        await self._link_message_to_task(session, message, task.id)
        return task

    async def _link_message_to_task(self, session: AsyncSession, message: Message, task_id: UUID) -> None:
        message.task_id = task_id
        await session.commit()
        await session.refresh(message)

    async def _load_task(self, session: AsyncSession, *, workspace_id: UUID, task_id: UUID) -> Task | None:
        task = await session.get(Task, task_id)
        if task is None or task.workspace_id != workspace_id:
            return None
        return task

    async def _load_approval(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        approval_id: UUID,
    ) -> Approval | None:
        approval = await session.get(Approval, approval_id)
        if approval is None or approval.workspace_id != workspace_id:
            return None
        return approval

    def _build_message_metadata(
        self,
        incoming: IncomingGatewayMessage,
        *,
        metadata_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata = {
            "gateway_name": incoming.gateway_name,
            "gateway_user_id": incoming.gateway_user.gateway_user_id,
            "gateway_username": incoming.gateway_user.username,
            "gateway_display_name": incoming.gateway_user.display_name,
            "gateway_chat_id": incoming.gateway_chat.gateway_chat_id,
            "gateway_chat_title": incoming.gateway_chat.title,
            "gateway_chat_type": incoming.gateway_chat.chat_type,
            "gateway_message_id": incoming.gateway_message_id,
            "attachments": self._serialize_attachments(incoming.attachments),
            "raw_payload": incoming.raw_payload,
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
            "/start\n"
            "/help\n"
            "/new <task description>\n"
            "/status <task_id>\n"
            "/queue\n"
            "/agents\n"
            "/agent <slug>\n"
            "/route <task_id>\n"
            "/models\n"
            "/budget\n"
            "/skills pending\n"
            "/skills approve <proposal_id>\n"
            "/subagents pending\n"
            "/subagents approve <proposal_id>\n"
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

    def _parse_command(self, text: str) -> tuple[str | None, list[str]]:
        stripped = text.strip()
        if not stripped.startswith("/"):
            return None, []

        parts = stripped.split()
        command_token = parts[0][1:]
        command = command_token.split("@", maxsplit=1)[0].strip().lower()
        if not command:
            return None, []
        return command, parts[1:]

    def _serialize_attachments(
        self,
        attachments: tuple[GatewayAttachment, ...],
    ) -> list[dict[str, Any]]:
        serialized: list[dict[str, Any]] = []
        for attachment in attachments:
            serialized.append(
                {
                    "content_type": attachment.content_type,
                    "url": attachment.url,
                    "file_name": attachment.file_name,
                    "size_bytes": attachment.size_bytes,
                    "metadata": dict(attachment.metadata),
                }
            )
        return serialized