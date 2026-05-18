from __future__ import annotations

import json
from datetime import datetime, timezone
import re
import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy import select

from agents.registry import get_agent_profile, list_agents
from agents.router import RouteRequest, RouterAgent
from app.config import Settings
from db.models import Approval, Message, Task, ToolCall, User, Workspace
from db.repositories import list_events as list_agent_events
from db.task_queue import (
    ACTIVE_TASK_STATUSES,
    TASK_PRIORITIES,
    TERMINAL_TASK_STATUSES,
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
from harness.workspace import WorkspaceManager
from services.agent_learning_service import approve_skill_proposal, approve_sub_agent_proposal
from services.budget_service import estimate_model_cost_level
from services.model_router import ModelExecutionRequest, ModelRouter, infer_provider


MAX_TASK_TITLE_LENGTH = 80
INLINE_CHAT_MAX_TOKENS = 400
INTENT_CLASSIFIER_MAX_TOKENS = 32
FOLLOWUP_CONFIRMATION_WINDOW = 6
logger = logging.getLogger(__name__)

_TASK_ACTION_PATTERN = re.compile(
    r"\b(add|analy[sz]e|audit|browse|build|change|check|create|debug|deploy|design|find|fix|implement|improve|install|investigate|look up|lookup|make|migrate|optimi[sz]e|refactor|remove|repair|replace|review|run|search|set up|setup|ship|test|trace|triage|update|upgrade|verify|write)\b"
)
_TASK_REQUEST_PREFIX_PATTERN = re.compile(
    r"^(please|can you|could you|would you|i need you to|need you to|help me|try to|let'?s)\b"
)
_CHAT_GREETING_PATTERN = re.compile(r"^(hi|hello|hey|yo|thanks|thank you)\b")
_CHAT_QUESTION_PREFIX_PATTERN = re.compile(r"^(what|why|how|who|where|when|which|explain|tell me|show me)\b")
_AFFIRMATION_PATTERN = re.compile(r"^(yes|yeah|yep|ok|okay|sure|please do|go ahead|do it|do it fast|yes do it fast|yes go|go yes|proceed|yes proceed|go and do it|yes go and do it|just do it)\b")
_TASK_STARTERS = {
    "add",
    "analyze",
    "analyse",
    "audit",
    "build",
    "change",
    "check",
    "create",
    "debug",
    "deploy",
    "design",
    "find",
    "fix",
    "implement",
    "improve",
    "install",
    "investigate",
    "lookup",
    "make",
    "migrate",
    "optimize",
    "optimise",
    "refactor",
    "remove",
    "repair",
    "replace",
    "review",
    "run",
    "search",
    "setup",
    "ship",
    "test",
    "trace",
    "triage",
    "update",
    "upgrade",
    "write",
}


class AgentGatewayService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        settings: Settings | None = None,
        model_router: ModelRouter | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings or Settings()
        self._router_agent = RouterAgent()
        self._model_router = model_router or ModelRouter(self._settings, session_factory)

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
            "code": self._handle_code_command,
            "test": self._handle_test_command,
            "pr": self._handle_pr_command,
            "events": self._handle_events_command,
            "workspace": self._handle_workspace_command,
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
        management_reply = await self._maybe_handle_natural_language_task_management(session, message, incoming)
        if management_reply is not None:
            return management_reply

        inherited_task_text = await self._resolve_followup_task_text(session, incoming)
        text_for_intent = inherited_task_text or incoming.text
        intent = await self._decide_text_intent(incoming.text)
        if inherited_task_text is not None:
            intent = "task"

        # Enrich context-dependent references BEFORE routing regardless of intent
        enriched_text = await self._enrich_with_recent_path_context(session, incoming, text_for_intent)
        if enriched_text != text_for_intent:
            text_for_intent = enriched_text
            intent = "task"

        if intent == "chat":
            return await self._handle_chat_message(session, message, incoming)
        task = await self._create_task_from_text(session, incoming, message, task_text=text_for_intent)
        route_preview = self._route_task_preview(task.title, task.description or "")
        return GatewayResponse(text=self._format_task_confirmation(task, route_preview=route_preview), task_id=task.id)

    async def _handle_chat_message(
        self,
        session: AsyncSession,
        message: Message,
        incoming: IncomingGatewayMessage,
    ) -> GatewayResponse:
        del message
        followup_reply = await self._maybe_answer_task_followup(session, incoming, incoming.text)
        if followup_reply is not None:
            return GatewayResponse(text=followup_reply)

        history = await self._load_recent_conversation_messages(session, incoming)
        special_reply = self._special_chat_reply(incoming.text)
        if special_reply is not None:
            return GatewayResponse(text=special_reply)

        # Load relevant memories to give chat context-awareness
        memory_context = await self._load_chat_memories(session, incoming)
        reply = await self._generate_inline_chat_reply(
            incoming.text, history=history, memory_context=memory_context,
        )
        return GatewayResponse(text=reply)

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
        route_preview = self._route_task_preview(task.title, task.description or "")
        return GatewayResponse(text=self._format_task_confirmation(task, route_preview=route_preview), task_id=task.id)

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
            metadata={"approval_id": str(approval.id), "action": "approved"},
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

    async def _handle_code_command(
        self,
        session: AsyncSession,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> GatewayResponse:
        if not arguments:
            return GatewayResponse(text="Usage: /code <task description>")
        task = await self._create_task_from_text(
            session,
            incoming,
            message,
            task_text=" ".join(arguments).strip(),
            metadata_overrides={"requested_agent": "coding_agent", "harness_requested": True, "workflow": "code"},
        )
        route_preview = self._route_task_preview(task.title, task.description or "")
        return GatewayResponse(text=self._format_task_confirmation(task, route_preview=route_preview), task_id=task.id)

    async def _handle_test_command(
        self,
        session: AsyncSession,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> GatewayResponse:
        if len(arguments) != 1:
            return GatewayResponse(text="Usage: /test <task_id>")
        task_id = self._parse_uuid(arguments[0])
        if task_id is None:
            return GatewayResponse(text="Task IDs must be valid UUID values.")
        parent_task = await self._load_task(session, workspace_id=message.workspace_id, task_id=task_id)
        if parent_task is None:
            return GatewayResponse(text="Task not found in the selected workspace.")
        task = await create_task_request(
            session,
            workspace_id=incoming.workspace_id,
            parent_task_id=parent_task.id,
            title=f"Test: {parent_task.title}",
            description=f"Run focused tests and validation for task {parent_task.id}: {parent_task.title}",
            created_by_user_id=incoming.user_id,
            metadata_json=self._build_task_request_metadata(
                incoming,
                message,
                {"requested_agent": "testing_agent", "harness_requested": True, "workflow": "test", "target_task_id": str(parent_task.id)},
            ),
        )
        await self._link_message_to_task(session, message, task.id)
        route_preview = self._route_task_preview(task.title, task.description or "")
        return GatewayResponse(text=self._format_task_confirmation(task, route_preview=route_preview), task_id=task.id)

    async def _handle_pr_command(
        self,
        session: AsyncSession,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> GatewayResponse:
        if len(arguments) != 1:
            return GatewayResponse(text="Usage: /pr <task_id>")
        task_id = self._parse_uuid(arguments[0])
        if task_id is None:
            return GatewayResponse(text="Task IDs must be valid UUID values.")
        parent_task = await self._load_task(session, workspace_id=message.workspace_id, task_id=task_id)
        if parent_task is None:
            return GatewayResponse(text="Task not found in the selected workspace.")
        task = await create_task_request(
            session,
            workspace_id=incoming.workspace_id,
            parent_task_id=parent_task.id,
            title=f"Prepare PR: {parent_task.title}",
            description=f"Create a branch, commit changes, push, and open a PR for task {parent_task.id}: {parent_task.title}",
            created_by_user_id=incoming.user_id,
            metadata_json=self._build_task_request_metadata(
                incoming,
                message,
                {"requested_agent": "coding_agent", "harness_requested": True, "workflow": "pr", "target_task_id": str(parent_task.id)},
            ),
        )
        await self._link_message_to_task(session, message, task.id)
        route_preview = self._route_task_preview(task.title, task.description or "")
        return GatewayResponse(text=self._format_task_confirmation(task, route_preview=route_preview), task_id=task.id)

    async def _handle_events_command(
        self,
        session: AsyncSession,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> GatewayResponse:
        del incoming
        if len(arguments) != 1:
            return GatewayResponse(text="Usage: /events <task_id>")
        task_id = self._parse_uuid(arguments[0])
        if task_id is None:
            return GatewayResponse(text="Task IDs must be valid UUID values.")
        task = await self._load_task(session, workspace_id=message.workspace_id, task_id=task_id)
        if task is None:
            return GatewayResponse(text="Task not found in the selected workspace.")
        events = await list_agent_events(session, task_id=task.id, limit=10)
        if not events:
            return GatewayResponse(text=f"No harness events recorded for task {task.id}.", task_id=task.id)
        lines = [f"Recent events for task {task.id}:"]
        for event in events:
            lines.append(f"[{event.sequence}] {event.event_type}: {event.content}")
        await self._link_message_to_task(session, message, task.id)
        return GatewayResponse(text="\n".join(lines), task_id=task.id)

    async def _handle_workspace_command(
        self,
        session: AsyncSession,
        message: Message,
        incoming: IncomingGatewayMessage,
        arguments: list[str],
    ) -> GatewayResponse:
        del incoming
        if len(arguments) != 1:
            return GatewayResponse(text="Usage: /workspace <task_id>")
        task_id = self._parse_uuid(arguments[0])
        if task_id is None:
            return GatewayResponse(text="Task IDs must be valid UUID values.")
        task = await self._load_task(session, workspace_id=message.workspace_id, task_id=task_id)
        if task is None:
            return GatewayResponse(text="Task not found in the selected workspace.")
        manager = WorkspaceManager(self._settings, self._session_factory)
        layout = await manager.get_task_workspace(task.id)
        await self._link_message_to_task(session, message, task.id)
        return GatewayResponse(
            text=(
                f"Workspace for task {task.id}\n"
                f"Root: {layout.root}\n"
                f"Repo: {layout.repo}\n"
                f"Events: {layout.events_jsonl}"
            ),
            task_id=task.id,
        )

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
        lines = [
            f"Global max cost/task: ${self._settings.max_cost_per_task_usd:.2f}",
            f"Daily model budget: ${self._settings.daily_model_budget_usd:.2f}",
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
        metadata_overrides: dict[str, Any] | None = None,
    ) -> Task:
        normalized_text = (task_text or incoming.text).strip()
        if not normalized_text:
            raise ValueError("Cannot create a task from an empty message.")

        # Include recent conversation history for context
        conversation_context = await self._build_conversation_context(session, incoming)
        description = normalized_text
        if conversation_context:
            description = f"{normalized_text}\n\n## Recent conversation context:\n{conversation_context}"

        title = self._build_task_title(normalized_text)
        task = await create_task_request(
            session,
            workspace_id=incoming.workspace_id,
            title=title,
            description=description,
            created_by_user_id=incoming.user_id,
            metadata_json=self._build_task_request_metadata(incoming, message, metadata_overrides),
        )
        await self._link_message_to_task(session, message, task.id)
        return task

    def _build_task_request_metadata(
        self,
        incoming: IncomingGatewayMessage,
        message: Message,
        metadata_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata = {
            "source": incoming.gateway_name,
            "source_chat_id": incoming.gateway_chat.gateway_chat_id,
            "source_user_id": incoming.gateway_user.gateway_user_id,
            "gateway_name": incoming.gateway_name,
            "gateway_message_id": incoming.gateway_message_id,
            "gateway_chat_id": incoming.gateway_chat.gateway_chat_id,
            "gateway_user_id": incoming.gateway_user.gateway_user_id,
            "gateway_username": incoming.gateway_user.username,
            "gateway_message_record_id": str(message.id),
            "attachments": self._serialize_attachments(incoming.attachments),
        }
        if metadata_overrides:
            metadata.update(metadata_overrides)
        return metadata

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

    def _format_task_confirmation(self, task: Task, *, route_preview=None) -> str:
        message = "Got it — I'm on it."
        if route_preview is not None:
            message += (
                f"\nAgent: {route_preview.agent_slug}\n"
                f"Why: {route_preview.reason}"
            )
        message += f"\nTask ID: {task.id}"
        return message

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

    def _format_queue_status_detailed(self, queue_status: dict[str, Any], active_tasks: list[Task]) -> str:
        pending = queue_status.get("pending", 0)
        running = queue_status.get("running", 0)
        paused = queue_status.get("paused", 0)
        total_active = pending + running + paused

        if total_active == 0:
            return "No tasks are currently pending, running, or paused."

        lines = [f"There are **{total_active}** active tasks ({pending} pending, {running} running, {paused} paused):"]
        for task in active_tasks:
            status_emoji = {"pending": "⏳", "running": "⚙️", "paused": "⏸", "paused_approval": "🔐"}.get(task.status, "•")
            lines.append(f"{status_emoji} [{task.status}] {task.title} — ID: {task.id}")
        return "\n".join(lines)

    def _help_text(self) -> str:
        return (
            "Agent Sam commands:\n"
            "Plain chat stays conversational. Explicit work requests create tracked tasks.\n"
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
            "/cancel <task_id>\n"
            "/code <task description>\n"
            "/test <task_id>\n"
            "/pr <task_id>\n"
            "/events <task_id>\n"
            "/workspace <task_id>"
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

    def _requires_server_action(self, normalized_text: str) -> bool:
        """Detect messages that MUST become tasks because they describe server problems or request server changes."""
        error_patterns = (
            "can't be reached", "took too long", "connection refused", "connection timed out",
            "err_connection", "err_timed_out", "502 bad gateway", "503 service unavailable",
            "500 internal", "still not working", "still saying", "still down",
            "configure", "set up", "set it up", "point it to", "point this to",
            "reload nginx", "restart nginx", "restart apache",
        )
        return any(p in normalized_text for p in error_patterns)

    async def _decide_text_intent(self, text: str) -> str:
        """
        Fast, layered intent detection — heuristics first, model fallback only
        when genuinely ambiguous. Optimized to avoid unnecessary LLM calls.
        """
        normalized_text = self._normalize_text(text)

        # Layer 1: Hard rules (no ambiguity, instant)
        if self._requires_server_action(normalized_text):
            return "task"
        if self._is_capability_question(normalized_text) or self._is_followup_question(normalized_text):
            return "chat"

        # Layer 2: Strong pattern matching (fast, reliable)
        if self._is_current_events_request(normalized_text):
            return "task"
        if self._is_strong_task_request(normalized_text, original_text=text):
            return "task"
        if self._is_strong_chat_message(normalized_text):
            return "chat"
        if self._looks_like_task_request(normalized_text, original_text=text):
            return "task"

        # Layer 3: Model classification (only for truly ambiguous messages)
        intent = await self._classify_text_intent_with_model(text)
        if intent is not None:
            return intent

        # Layer 4: Default — short messages are chat, longer ones are tasks
        words = normalized_text.split()
        if len(words) <= 4:
            return "chat"
        return "task"

    async def _classify_text_intent_with_model(self, text: str) -> str | None:
        model_name = self._resolve_inline_chat_model()
        if not self._can_run_model(model_name):
            return None

        try:
            response = await self._model_router.run_completion(
                ModelExecutionRequest(
                    agent_slug="gateway_intent_router",
                    model=model_name,
                    messages=self._build_intent_classification_messages(text),
                    temperature=0.0,
                    max_tokens=INTENT_CLASSIFIER_MAX_TOKENS,
                    metadata={"gateway_mode": "intent_classifier"},
                )
            )
        except Exception:
            logger.debug("Gateway intent classification fell back to local heuristics.", exc_info=True)
            return None

        payload = self._parse_json_object(response.content)
        if isinstance(payload, dict):
            intent = str(payload.get("intent", "")).strip().lower()
            if intent in {"chat", "task"}:
                return intent

        lowered = response.content.strip().lower()
        if "task" in lowered and "chat" not in lowered:
            return "task"
        if "chat" in lowered:
            return "chat"
        return None

    async def _generate_inline_chat_reply(
        self,
        text: str,
        *,
        history: list[dict[str, str]] | None = None,
        memory_context: list[dict[str, str]] | None = None,
    ) -> str:
        model_name = self._resolve_inline_chat_model()
        if not self._can_run_model(model_name):
            return self._fallback_chat_reply(text)

        try:
            response = await self._model_router.run_completion(
                ModelExecutionRequest(
                    agent_slug="gateway_chat_agent",
                    model=model_name,
                    messages=self._build_inline_chat_messages(
                        text, history=history or [], memory_context=memory_context or [],
                    ),
                    temperature=0.3,
                    max_tokens=INLINE_CHAT_MAX_TOKENS,
                    metadata={"gateway_mode": "inline_chat"},
                )
            )
        except Exception:
            logger.warning("Inline gateway chat completion failed; using fallback reply.", exc_info=True)
            return self._fallback_chat_reply(text)

        reply = response.content.strip()
        return reply or self._fallback_chat_reply(text)

    def _build_intent_classification_messages(self, text: str) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "You classify gateway messages. Return strict JSON only with one key: "
                    "{\"intent\":\"chat\"} or {\"intent\":\"task\"}. "
                    "Choose task when the user asks to: run commands, check/fix server state, configure services, "
                    "find/list/create/modify files or folders, point domains, restart services, diagnose errors, "
                    "or any action that requires shell access or server changes. "
                    "Choose chat ONLY for: greetings, 'what are you', capability questions, simple clarifications, thanks. "
                    "When in doubt, choose task."
                ),
            },
            {"role": "user", "content": text.strip()},
        ]

    def _build_inline_chat_messages(
        self,
        text: str,
        *,
        history: list[dict[str, str]],
        memory_context: list[dict[str, str]] | None = None,
    ) -> list[dict[str, str]]:
        web_access_state = "enabled" if self._settings.enable_web_research else "disabled"
        current_model = self._resolve_inline_chat_model()

        # Build memory section if available
        memory_section = ""
        if memory_context:
            memory_lines = [f"- [{m.get('type', '?')}] {m['content']}" for m in memory_context[:6]]
            memory_section = (
                "\n\nYOUR SAVED MEMORIES (facts you learned from previous tasks):\n"
                + "\n".join(memory_lines)
                + "\nUse these to answer questions about what you know/remember."
            )

        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "You are Agent Sam — a private AI agent OS. You are the conversational chat layer.\n\n"
                    "YOUR IDENTITY (memorize this — never say GPT-4 or OpenAI):\n"
                    f"- Your name is Agent Sam. Your current LLM is: {current_model}\n"
                    "- You are powered by DeepSeek V3 via OpenRouter, with specialist agents.\n"
                    "- You are NOT ChatGPT, NOT GPT-4, NOT an OpenAI product. You are Agent Sam.\n"
                    "- When asked what model: say 'I'm Agent Sam, running on DeepSeek V3 via OpenRouter.'\n\n"
                    "YOUR CAPABILITIES:\n"
                    "- You DO save important details from every task to a memory database.\n"
                    "- You have specialist agents: server_ops, coding, research, testing, planning.\n"
                    "- You can execute shell commands, manage servers, search the web.\n"
                    "- You can browse websites using a real browser (Playwright) — login, click, fill forms.\n"
                    "- You can clone and setup any GitHub repo, install dependencies, and run it.\n"
                    "- You save server facts, decisions, warnings, and task summaries automatically.\n"
                    "- Your code is at: https://github.com/samlaggz/Agent-Sam\n"
                    "- You CAN access your own code and make changes via server_ops tasks.\n\n"
                    "RULES:\n"
                    "- Be concise, warm, and helpful. Use natural language.\n"
                    "- NEVER output shell commands, code blocks, or pretend to run anything.\n"
                    "- If the user needs a server action, say: 'I'll queue that as a task for you.'\n"
                    "- Use conversation history AND memories to answer questions.\n"
                    f"- Web research: {web_access_state}.\n"
                    "- If asked about saving: YES, you save important details from tasks automatically.\n"
                    "- Keep replies under 3 sentences unless the user asks for detail."
                    f"{memory_section}"
                ),
            },
        ]
        messages.extend(history)
        messages.append({"role": "user", "content": text.strip()})
        return messages

    def _resolve_inline_chat_model(self) -> str:
        return self._settings.default_model.strip() or self._settings.litellm_model.strip()

    def _can_run_model(self, model_name: str) -> bool:
        provider = infer_provider(model_name)
        if provider == "openrouter":
            return bool(self._settings.openrouter_api_key or self._settings.litellm_api_key)
        if provider == "openai":
            return bool(self._settings.openai_api_key or self._settings.litellm_api_key)
        if provider == "anthropic":
            return bool(self._settings.anthropic_api_key or self._settings.litellm_api_key)
        if provider == "gemini":
            return bool(self._settings.gemini_api_key or self._settings.litellm_api_key)
        if provider == "ollama":
            return bool(self._settings.ollama_base_url)
        return bool(self._settings.litellm_api_key)

    def _looks_like_task_request(self, normalized_text: str, *, original_text: str) -> bool:
        if self._is_strong_task_request(normalized_text, original_text=original_text):
            return True
        if _TASK_REQUEST_PREFIX_PATTERN.match(normalized_text) and _TASK_ACTION_PATTERN.search(normalized_text):
            return True
        action_matches = {match.group(0) for match in _TASK_ACTION_PATTERN.finditer(normalized_text)}
        if len(action_matches) >= 2:
            return True
        return False

    def _is_strong_task_request(self, normalized_text: str, *, original_text: str) -> bool:
        words = normalized_text.split()
        if not words:
            return False
        first_word = words[0]
        first_two_words = " ".join(words[:2])
        if "\n" in original_text and len(words) >= 6:
            return True
        if normalized_text.startswith(("task:", "todo:", "queue this", "create a task", "make a task")):
            return True
        if first_word in _TASK_STARTERS or first_two_words == "set up":
            return True
        if _TASK_REQUEST_PREFIX_PATTERN.match(normalized_text) and _TASK_ACTION_PATTERN.search(normalized_text):
            return True
        return False

    def _is_strong_chat_message(self, normalized_text: str) -> bool:
        if not normalized_text:
            return False
        words = normalized_text.split()
        # Server-action keywords should NEVER be classified as chat
        server_action_keywords = (
            "nginx", "apache", "systemctl", "pm2", "docker", "domain", "ssl", "certificate",
            "firewall", "port", "dns", "config", "restart", "reload", "site", "server",
            "can't be reached", "connection refused", "timed out", "err_", "502", "503", "500",
            "point", "configure", "setup", "install", "deploy",
        )
        if any(kw in normalized_text for kw in server_action_keywords):
            return False
        if _CHAT_GREETING_PATTERN.match(normalized_text) and len(words) <= 5:
            return True
        if normalized_text.endswith("?") and not self._looks_like_task_request_question(normalized_text):
            return True
        if _CHAT_QUESTION_PREFIX_PATTERN.match(normalized_text) and not _TASK_ACTION_PATTERN.search(normalized_text):
            return True
        if len(words) <= 3 and not _TASK_ACTION_PATTERN.search(normalized_text):
            return True
        return False

    def _looks_like_task_request_question(self, normalized_text: str) -> bool:
        return bool(_TASK_REQUEST_PREFIX_PATTERN.match(normalized_text) and _TASK_ACTION_PATTERN.search(normalized_text))

    def _is_capability_question(self, normalized_text: str) -> bool:
        capability_phrases = (
            "do you have internet access",
            "do you have web access",
            "do you have online access",
            "can you access the internet",
            "can you browse the internet",
            "what can you do",
            "what tools do you have",
        )
        return any(phrase in normalized_text for phrase in capability_phrases)

    def _is_current_events_request(self, normalized_text: str) -> bool:
        current_event_markers = (
            "latest news",
            "what is the latest news",
            "current news",
            "breaking news",
            "what's happening",
            "whats happening",
            "latest update on",
            "news on",
            "war news",
            "iran war news",
        )
        if any(marker in normalized_text for marker in current_event_markers):
            return True
        if "news" in normalized_text and any(token in normalized_text for token in ("iran", "war", "conflict", "attack", "strike")):
            return True
        return False

    def _is_followup_question(self, normalized_text: str) -> bool:
        followup_phrases = (
            "any update",
            "what's the update",
            "whats the update",
            "status update",
            "what happened",
            "did you finish",
            "are you done",
            "progress update",
            "you checked",
            "can you share the progress",
            "how are you checking it",
        )
        return any(phrase in normalized_text for phrase in followup_phrases)

    async def _load_chat_memories(
        self,
        session: AsyncSession,
        incoming: IncomingGatewayMessage,
    ) -> list[dict[str, str]]:
        """Load recent memories to give the chat layer context awareness."""
        try:
            from db.memory_service import MemorySearchRequest, search_memories

            memories = await search_memories(
                session,
                MemorySearchRequest(
                    workspace_id=incoming.workspace_id,
                    text_query=incoming.text[:200],
                    limit=8,
                ),
            )
            return [
                {"type": m.memory_type, "content": m.content[:200], "source": m.source}
                for m in memories
            ]
        except Exception:
            logger.debug("Failed to load chat memories", exc_info=True)
            return []

    async def _build_conversation_context(
        self,
        session: AsyncSession,
        incoming: IncomingGatewayMessage,
    ) -> str:
        """Build a summary of recent conversation for task context."""
        try:
            history = await self._load_recent_conversation_messages(session, incoming, limit=10)
            if not history:
                return ""
            lines: list[str] = []
            for msg in history[-8:]:
                role = "User" if msg["role"] == "user" else "Agent"
                content = msg["content"].strip()[:200]
                lines.append(f"{role}: {content}")
            return "\n".join(lines)
        except Exception:
            return ""

    def _fallback_chat_reply(self, text: str) -> str:
        normalized_text = self._normalize_text(text)
        if "internet access" in normalized_text or "web access" in normalized_text or "online access" in normalized_text:
            if self._settings.enable_web_research:
                return "Yes. Web research is enabled. Ask me to look something up and I will route it as tracked work to the right specialist."
            return "Not right now. Web research is disabled in the current runtime configuration."
        if _CHAT_GREETING_PATTERN.match(normalized_text):
            return "Hi. I can chat here for quick questions, and I create tracked tasks only when you ask me to do work or use /new."
        if normalized_text.startswith(("what is this", "what's this", "what does this")):
            return (
                "This is the Agent Sam gateway chat. Normal conversation stays in chat, and explicit work requests become tracked tasks. "
                "Use /help for commands or /new <task description> when you want to queue work yourself."
            )
        if normalized_text.endswith("?") or _CHAT_QUESTION_PREFIX_PATTERN.match(normalized_text):
            return (
                "I can answer quick questions here. If you want tracked work queued for the worker, ask me to do something explicitly or use /new <task description>."
            )
        return "I can chat here and I can queue work. Use /new <task description> when you want a tracked task."

    def _route_task_preview(self, title: str, description: str):
        return self._router_agent.route(
            RouteRequest(
                title=title,
                description=description,
                metadata={"source": "gateway", "enable_web_research": self._settings.enable_web_research},
            )
        )

    def _special_chat_reply(self, text: str) -> str | None:
        normalized_text = self._normalize_text(text)
        if _CHAT_GREETING_PATTERN.match(normalized_text):
            return "Hello! How can I help?"
        # Model identity questions — answer directly without LLM call
        model_phrases = ("which model", "what model", "which llm", "what llm", "who are you", "what are you")
        if any(phrase in normalized_text for phrase in model_phrases):
            current_model = self._resolve_inline_chat_model()
            return f"I'm Agent Sam, running on {current_model} via OpenRouter with specialist agents."
        # Memory/saving questions — answer directly
        saving_phrases = ("are you saving", "do you save", "do you remember", "saving everything", "saving details")
        if any(phrase in normalized_text for phrase in saving_phrases):
            return (
                "Yes! I save important details from every task to my memory database — server facts, "
                "decisions, warnings, and task summaries. I use these to give better context on future tasks. "
                "I also have access to my own code at /opt/agent-sam and can modify it."
            )
        if "internet access" in normalized_text or "web access" in normalized_text or "online access" in normalized_text:
            if self._settings.enable_web_research:
                return "Yes. I have web research enabled for routed work. If you ask me to look something up, I can hand it to the research flow and send the result back."
            return "No. Web research is currently disabled in this deployment."
        return None

    async def _resolve_followup_task_text(
        self,
        session: AsyncSession,
        incoming: IncomingGatewayMessage,
    ) -> str | None:
        normalized_text = self._normalize_text(incoming.text)
        if not _AFFIRMATION_PATTERN.match(normalized_text):
            return None

        history = await self._load_recent_conversation_messages(session, incoming, limit=FOLLOWUP_CONFIRMATION_WINDOW * 2)

        # First pass: look for a prior actionable user message
        for item in reversed(history):
            if item["role"] != "user":
                continue
            prior_text = item["content"].strip()
            prior_normalized = self._normalize_text(prior_text)
            if prior_normalized == normalized_text:
                continue
            if self._is_capability_question(prior_normalized):
                continue
            if self._is_current_events_request(prior_normalized) or self._looks_like_task_request(prior_normalized, original_text=prior_text):
                return prior_text

        # Second pass: look for an assistant message that suggested an action, and build a task from it
        for item in reversed(history):
            if item["role"] != "assistant":
                continue
            text = item["content"].strip()
            if any(kw in text.lower() for kw in ("i will", "shall i", "would you like me to", "i can", "next step", "to fix", "to troubleshoot")):
                # Find the most recent user message that led to this assistant reply
                for prior in reversed(history):
                    if prior["role"] == "user" and prior["content"].strip().lower() != normalized_text:
                        return prior["content"].strip()
                # No prior user message found — use the assistant suggestion as task context
                summary_line = text.splitlines()[0][:100]
                return f"Execute the suggested action: {summary_line}"

        return None

    async def _load_recent_conversation_messages(
        self,
        session: AsyncSession,
        incoming: IncomingGatewayMessage,
        *,
        limit: int = 8,
    ) -> list[dict[str, str]]:
        result = await session.execute(
            select(Message)
            .where(Message.workspace_id == incoming.workspace_id, Message.user_id == incoming.user_id)
            .order_by(Message.created_at.desc())
            .limit(30)
        )
        recent_messages = list(reversed(result.scalars().all()))

        history: list[dict[str, str]] = []
        for recent_message in recent_messages:
            metadata = recent_message.metadata_json if isinstance(recent_message.metadata_json, dict) else {}
            if recent_message.role == "user":
                if metadata.get("gateway_name") != incoming.gateway_name:
                    continue
                if metadata.get("gateway_chat_id") != incoming.gateway_chat.gateway_chat_id:
                    continue
            elif recent_message.role == "assistant":
                transport = metadata.get("transport") or metadata.get("gateway_name")
                chat_id = metadata.get("source_chat_id") or metadata.get("gateway_chat_id")
                if transport not in {incoming.gateway_name, "agent"} and transport is not None:
                    continue
                if chat_id not in {None, incoming.gateway_chat.gateway_chat_id}:
                    continue
            else:
                continue

            if not recent_message.content.strip():
                continue
            history.append({"role": recent_message.role if recent_message.role in {"user", "assistant"} else "assistant", "content": recent_message.content.strip()})

        return history[-limit:]

    async def _maybe_answer_task_followup(
        self,
        session: AsyncSession,
        incoming: IncomingGatewayMessage,
        text: str,
    ) -> str | None:
        normalized_text = self._normalize_text(text)
        if not self._is_followup_question(normalized_text):
            return None

        recent_task = await self._load_recent_task_for_chat(session, incoming)
        if recent_task is None:
            return None

        latest_message_result = await session.execute(
            select(Message)
            .where(Message.task_id == recent_task.id, Message.role == "assistant")
            .order_by(Message.created_at.desc())
            .limit(1)
        )
        latest_message = latest_message_result.scalar_one_or_none()
        status_line = f"Latest task: {recent_task.title}\nStatus: {recent_task.status}"
        if latest_message is not None and latest_message.content.strip():
            return status_line + "\n\n" + latest_message.content.strip()
        return status_line

    async def _load_recent_task_for_chat(
        self,
        session: AsyncSession,
        incoming: IncomingGatewayMessage,
    ) -> Task | None:
        result = await session.execute(
            select(Task)
            .where(Task.workspace_id == incoming.workspace_id, Task.created_by_user_id == incoming.user_id)
            .order_by(Task.created_at.desc())
            .limit(20)
        )
        tasks = result.scalars().all()
        for task in tasks:
            metadata = task.metadata_json if isinstance(task.metadata_json, dict) else {}
            source = metadata.get("source") or metadata.get("gateway_name")
            chat_id = metadata.get("source_chat_id") or metadata.get("gateway_chat_id")
            if source != incoming.gateway_name:
                continue
            if chat_id != incoming.gateway_chat.gateway_chat_id:
                continue
            if task.status not in TERMINAL_TASK_STATUSES:
                return task
            return task
        return None

    async def _maybe_handle_natural_language_task_management(
        self,
        session: AsyncSession,
        message: Message,
        incoming: IncomingGatewayMessage,
    ) -> GatewayResponse | None:
        normalized_text = self._normalize_text(incoming.text)

        if self._is_bulk_cancel_request(normalized_text):
            all_active = await list_task_queue(
                session,
                workspace_id=incoming.workspace_id,
                statuses=("pending", "running", "paused"),
                limit=200,
            )
            cancelled_count = 0
            for task in all_active:
                if task.status not in TERMINAL_TASK_STATUSES:
                    await cancel_task(session, task_id=task.id)
                    cancelled_count += 1
            if cancelled_count == 0:
                return GatewayResponse(text="No active tasks to cancel. The queue is already empty.")
            return GatewayResponse(text=f"Done. Cancelled {cancelled_count} task(s). The queue is now empty.")

        if self._is_queue_question(normalized_text) or self._is_queue_question(incoming.text.lower().strip()):
            queue_status = await get_queue_status(session, workspace_id=incoming.workspace_id)
            active_tasks = await list_task_queue(
                session,
                workspace_id=incoming.workspace_id,
                statuses=("pending", "running", "paused"),
                limit=10,
            )
            return GatewayResponse(text=self._format_queue_status_detailed(queue_status, active_tasks))

        if self._is_cancel_request(normalized_text):
            task = await self._load_recent_task_for_chat(session, incoming)
            if task is None:
                return GatewayResponse(text="I couldn't find a recent task in this chat to cancel.")
            if task.status in TERMINAL_TASK_STATUSES:
                return GatewayResponse(text=f"That task is already {task.status}.", task_id=task.id)
            cancelled = await cancel_task(session, task_id=task.id)
            if cancelled is None:
                return GatewayResponse(text="I couldn't cancel that task.")
            await self._link_message_to_task(session, message, cancelled.id)
            return GatewayResponse(
                text=f"I cancelled the task `{cancelled.title}`. Current status: {cancelled.status}.",
                task_id=cancelled.id,
            )

        if self._is_status_question(normalized_text):
            task = await self._load_recent_task_for_chat(session, incoming)
            if task is None:
                return GatewayResponse(text="There isn't a recent task in this chat yet.")
            subtasks = await list_subtasks(session, parent_task_id=task.id)
            await self._link_message_to_task(session, message, task.id)
            return GatewayResponse(text=self._format_task_status(task, len(subtasks)), task_id=task.id)

        return None

    def _is_queue_question(self, normalized_text: str) -> bool:
        phrases = (
            "any running or pending tasks",
            "any pending task",
            "any pending tasks",
            "any queued task",
            "any queued tasks",
            "what are current queued or running task",
            "what are current queued or running tasks",
            "check queue",
            "check the queue",
            "check current task queue",
            "current task queue",
            "check pending task",
            "check pending tasks",
            "check and tell me if any pending task",
            "check and tell me if any pending tasks",
            "what tasks are running",
            "what tasks are pending",
            "list tasks",
            "show tasks",
            "show pending tasks",
            "show running tasks",
            "show queue",
            "how many tasks",
            "how many task",
            "tasks in queue",
            "task in queue",
            "in queue now",
            "tell is there any pending",
            "is there any pending",
            "is there any task",
            "any task in",
            "list of task",
            "list of tasks",
            "list task queue",
            "list tasks in queue",
            "list of tasks in queue",
            "list tasks in task queue",
            "what is in task queue",
            "what is in the queue",
            "is task queue",
            "task queue",
        )
        return any(phrase in normalized_text for phrase in phrases)

    def _is_bulk_cancel_request(self, normalized_text: str) -> bool:
        phrases = (
            "remove all",
            "clear all tasks",
            "cancel all tasks",
            "delete all tasks",
            "clear queue",
            "cancel all",
            "delete all",
            "remove all tasks",
            "clear all",
            "delete them",
            "cancel them",
            "remove them",
            "delete those",
            "cancel those",
            "close them",
            "close all",
            "close them all",
            "stop all",
            "stop them",
            "stop all tasks",
            "kill all",
            "kill all tasks",
        )
        return any(phrase in normalized_text for phrase in phrases)

    def _is_cancel_request(self, normalized_text: str) -> bool:
        phrases = ("delete this task", "cancel this task", "remove this task")
        return any(phrase in normalized_text for phrase in phrases)

    def _is_status_question(self, normalized_text: str) -> bool:
        phrases = (
            "what is the status",
            "tell me status",
            "status about",
            "what is the progress",
            "what are the progress",
            "did you find anything",
        )
        return any(phrase in normalized_text for phrase in phrases)

    def _is_path_reference(self, text: str) -> bool:
        path_patterns = re.compile(r"(/[a-zA-Z0-9_.\-]+){2,}|/var/|/opt/|/etc/|/home/|/srv/")
        return bool(path_patterns.search(text))

    def _is_folder_contents_question(self, normalized_text: str) -> bool:
        phrases = (
            "what is in",
            "what's in",
            "whats in",
            "contents of",
            "what's inside",
            "whats inside",
            "what files",
            "list contents",
            "show contents",
            "what does it contain",
        )
        return any(phrase in normalized_text for phrase in phrases)

    def _is_pwd_question(self, normalized_text: str) -> bool:
        phrases = (
            "give me pwd",
            "pwd for",
            "what is the path",
            "what is the full path",
            "where is this folder",
            "where is that folder",
            "folder path",
            "full path",
        )
        return any(phrase in normalized_text for phrase in phrases)

    def _extract_best_path_from_messages(self, messages: list) -> str | None:
        path_re = re.compile(r"(/(?:[a-zA-Z0-9_.\-]+/)*[a-zA-Z0-9_.\-]+)")
        for msg in messages:
            content = msg.content or ""
            matches = path_re.findall(content)
            for m in matches:
                if len(m) > 4 and "/proc" not in m and "/sys" not in m:
                    return m
        return None

    async def _enrich_with_recent_path_context(self, session: AsyncSession, incoming: IncomingGatewayMessage, text: str) -> str:
        normalized_text = self._normalize_text(text)
        needs_path = (
            self._is_folder_contents_question(normalized_text)
            or self._is_pwd_question(normalized_text)
            or self._has_unclear_path_reference(normalized_text)
        )
        if not needs_path:
            return text

        result = await session.execute(
            select(Message)
            .where(Message.workspace_id == incoming.workspace_id)
            .order_by(Message.created_at.desc())
            .limit(60)
        )
        recent_messages = list(result.scalars().all())

        path = self._extract_best_path_from_messages(recent_messages)
        if path:
            if self._is_pwd_question(normalized_text):
                return f"Print the full path of {path} using: cd {path} && pwd"
            if self._is_folder_contents_question(normalized_text):
                return f"List the contents of {path} using: ls -la {path}"
            return f"{text} — context: the path being referenced is {path}"
        return text

    def _has_unclear_path_reference(self, normalized_text: str) -> bool:
        phrases = (
            "that folder",
            "this folder",
            "the folder",
            "that directory",
            "this directory",
            "it should be in",
            "look in",
            "check in",
        )
        return any(phrase in normalized_text for phrase in phrases)

    def _parse_json_object(self, text: str) -> dict[str, Any] | None:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            cleaned = cleaned.replace("json", "", 1).strip()
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError:
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start == -1 or end == -1 or end <= start:
                return None
            try:
                payload = json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                return None
        return payload if isinstance(payload, dict) else None

    def _normalize_text(self, text: str) -> str:
        return " ".join(text.strip().lower().split())