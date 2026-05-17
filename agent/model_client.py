"""
Hermes-style model client with native tool calling.

Instead of generating a JSON plan, the model receives tool definitions and
returns tool_calls directly. The conversation loop feeds tool results back
to the model until it decides to respond with text or call task_complete.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.tool_definitions import build_tool_definitions
from agents.base import AgentProfile
from app.config import Settings
from services.model_router import ModelExecutionRequest, ModelRouter

logger = logging.getLogger(__name__)

# ── Legacy compatibility ──────────────────────────────────────────────


@dataclass(frozen=True)
class PlannedStep:
    """Legacy: kept for backward compat with existing DB step records."""
    title: str
    description: str
    tool_name: str | None = None
    command: str | None = None
    reason: str | None = None


class PlanningModel(Protocol):
    async def create_plan(
        self,
        *,
        task: dict[str, Any],
        memory_context: list[dict[str, Any]],
        skill_context: list[dict[str, Any]],
        task_run_id: UUID | None,
    ) -> list[PlannedStep]: ...


# ── New Hermes-style types ────────────────────────────────────────────


@dataclass(frozen=True)
class ToolCallRequest:
    """A single tool call the model wants to make."""
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelTurn:
    """One turn of model output — either text or tool calls."""
    content: str | None = None
    tool_calls: tuple[ToolCallRequest, ...] = ()
    finish_reason: str | None = None
    model: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0

    @property
    def is_done(self) -> bool:
        return not self.has_tool_calls and self.content is not None


@dataclass
class Conversation:
    """Manages the message history for a multi-turn tool-calling loop."""
    messages: list[dict[str, Any]] = field(default_factory=list)
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    turn_count: int = 0

    def add_system(self, content: str) -> None:
        self.messages.append({"role": "system", "content": content})

    def add_user(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})

    def add_assistant_text(self, content: str) -> None:
        self.messages.append({"role": "assistant", "content": content})

    def add_assistant_tool_calls(self, tool_calls: Sequence[ToolCallRequest]) -> None:
        self.messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    },
                }
                for tc in tool_calls
            ],
        })

    def add_tool_result(self, tool_call_id: str, name: str, content: str) -> None:
        self.messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": name,
            "content": content[:8000],
        })

    def track_usage(self, turn: ModelTurn) -> None:
        self.turn_count += 1
        if turn.prompt_tokens:
            self.total_prompt_tokens += turn.prompt_tokens
        if turn.completion_tokens:
            self.total_completion_tokens += turn.completion_tokens


# ── Guardrails ────────────────────────────────────────────────────────


class ToolCallGuardrails:
    """Hermes-style guardrails: detect loops, repeated failures, runaway."""

    def __init__(self, *, max_iterations: int = 40, max_consecutive_failures: int = 5) -> None:
        self.max_iterations = max_iterations
        self.max_consecutive_failures = max_consecutive_failures
        self._iteration_count = 0
        self._consecutive_failures = 0
        self._recent_calls: list[str] = []

    def record_success(self) -> None:
        self._iteration_count += 1
        self._consecutive_failures = 0

    def record_failure(self, call_sig: str) -> None:
        self._iteration_count += 1
        self._consecutive_failures += 1
        self._recent_calls.append(call_sig)

    @property
    def should_halt(self) -> bool:
        if self._iteration_count >= self.max_iterations:
            return True
        if self._consecutive_failures >= self.max_consecutive_failures:
            return True
        if len(self._recent_calls) >= 6:
            last_six = self._recent_calls[-6:]
            if len(set(last_six)) <= 2:
                return True
        return False

    @property
    def halt_reason(self) -> str:
        if self._iteration_count >= self.max_iterations:
            return f"Reached maximum iterations ({self.max_iterations})"
        if self._consecutive_failures >= self.max_consecutive_failures:
            return f"Too many consecutive failures ({self._consecutive_failures})"
        return "Detected repetitive loop in tool calls"


# ── Model Client ──────────────────────────────────────────────────────


class HermesModelClient:
    """
    Hermes-style model client with native OpenAI tool calling.
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        profile: AgentProfile | None = None,
        model_router: ModelRouter | None = None,
        model_override: str | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._profile = profile
        self._model_router = model_router or ModelRouter(settings, session_factory)
        self._model_override = model_override

    def build_conversation(
        self,
        *,
        task: dict[str, Any],
        memory_context: list[dict[str, Any]],
        skill_context: list[dict[str, Any]],
    ) -> Conversation:
        conv = Conversation()
        conv.add_system(self._build_system_prompt())
        conv.add_user(self._build_task_message(task, memory_context, skill_context))
        return conv

    async def get_next_turn(
        self,
        conversation: Conversation,
        *,
        tools: list[dict[str, Any]] | None = None,
        task_id: UUID | None = None,
        task_run_id: UUID | None = None,
    ) -> ModelTurn:
        model_name = self._resolve_model()

        try:
            from litellm import acompletion

            kwargs = self._model_router._build_completion_kwargs(
                model_name,
                ModelExecutionRequest(
                    agent_slug=self._profile.slug if self._profile else "hermes_agent",
                    model=model_name,
                    messages=conversation.messages,
                    temperature=self._profile.temperature if self._profile else 0.15,
                    max_tokens=self._profile.max_tokens_per_run if self._profile else 4096,
                ),
            )

            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"

            start = time.perf_counter()
            response = await acompletion(**kwargs)
            duration_ms = int((time.perf_counter() - start) * 1000)

            choice = response.choices[0]
            message = choice.message

            usage = getattr(response, "usage", None)
            prompt_tokens = getattr(usage, "prompt_tokens", None) if usage else None
            completion_tokens = getattr(usage, "completion_tokens", None) if usage else None

            tool_calls: list[ToolCallRequest] = []
            if hasattr(message, "tool_calls") and message.tool_calls:
                for tc in message.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments)
                    except (json.JSONDecodeError, AttributeError):
                        args = {"raw": tc.function.arguments}
                    tool_calls.append(ToolCallRequest(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=args,
                    ))

            content = getattr(message, "content", None)
            turn = ModelTurn(
                content=content,
                tool_calls=tuple(tool_calls),
                finish_reason=getattr(choice, "finish_reason", None),
                model=model_name,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
            conversation.track_usage(turn)

            await self._record_call(
                model_name=model_name,
                task_id=task_id,
                task_run_id=task_run_id,
                messages=conversation.messages,
                response_text=content or json.dumps([tc.name for tc in tool_calls]),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                duration_ms=duration_ms,
            )

            return turn

        except Exception as exc:
            logger.error("Model call failed: %s", exc, exc_info=True)
            return ModelTurn(
                content=f"I encountered an error calling the model: {exc}",
                finish_reason="error",
                model=model_name,
            )

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        shell_enabled = self._has_shell_access()
        web_enabled = self._settings.enable_web_research and self._has_web_access()
        browser_enabled = self._is_browser_available()
        return build_tool_definitions(
            shell_enabled=shell_enabled,
            web_enabled=web_enabled,
            browser_enabled=browser_enabled,
            memory_enabled=True,
        )

    def _is_browser_available(self) -> bool:
        """Check if Playwright browser tools are available."""
        try:
            from tools.browser_tool import check_browser_available
            available, _ = check_browser_available()
            return available
        except Exception:
            return False

    def _resolve_model(self) -> str:
        if self._model_override:
            return self._model_override
        if self._profile:
            return self._profile.default_model
        return self._settings.default_model or self._settings.litellm_model

    def _has_shell_access(self) -> bool:
        if not self._profile:
            return True
        return any(
            t in self._profile.tools_allowed
            for t in ("safe_shell", "service_control", "file_read", "file_write", "grep", "pytest")
        )

    def _has_web_access(self) -> bool:
        if not self._profile:
            return False
        return any(t in self._profile.tools_allowed for t in ("web_search", "web_open"))

    def _build_system_prompt(self) -> str:
        profile_prompt = self._load_profile_prompt()
        return (
            f"{profile_prompt}\n\n"
            "You are an autonomous AI agent with tools. You execute tasks by calling tools "
            "and analyzing their results iteratively until the job is done.\n\n"
            "CRITICAL RULES:\n"
            "1. Use tools to get real information. NEVER guess or fabricate output.\n"
            "2. After each tool result, analyze it and decide your next action.\n"
            "3. If a command fails, read the error and try a different approach.\n"
            "4. Always VERIFY your work with a final check (curl, ls, cat, nginx -t, etc.).\n"
            "5. Use absolute paths. Never use placeholder paths like <folder_path>.\n"
            "6. To create files, use: tee /path/file > /dev/null << 'EOF'\\ncontent\\nEOF\n"
            "7. Before symlinking nginx configs, clean broken symlinks:\n"
            "   find /etc/nginx/sites-enabled/ -xtype l -delete\n"
            "8. When done and verified, call task_complete with a clear summary.\n"
            "9. If you cannot complete the task after trying, call task_failed with the reason.\n"
            "10. Save important discoveries to memory (server_fact, decision, warning).\n"
            "11. Keep commands concise. One logical action per tool call.\n"
            "12. NEVER cat a file that doesn't exist. Create it first.\n"
            "13. NEVER use sudo — you are already running as root.\n"
            "14. When a command fails with a non-zero exit code, READ the error output carefully "
            "and fix the issue. Do NOT repeat the same failing command.\n"
            "15. NEVER call task_complete if you only asked a question or requested clarification. "
            "If you need more info, call task_failed with a clear question.\n"
            "16. If browser tools are available (browser_navigate, browser_click, browser_type), "
            "use them to interact with websites — log in, fill forms, click buttons, read pages. "
            "If browser tools are NOT in your tool list, you cannot interact with web UIs.\n"
            "17. READ the conversation context in the task description carefully. It contains "
            "the recent chat history so you understand what the user has been discussing.\n"
            "18. To clone and use a GitHub repo: git clone <url>, cd into it, read README, "
            "install dependencies (pip install, npm install, etc.), then run it."
        )

    def _load_profile_prompt(self) -> str:
        if self._profile is None:
            return "You are Agent Sam — a private AI agent operating system."
        try:
            return Path(self._profile.system_prompt_path).read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return f"You are {self._profile.name}, a specialist for {', '.join(self._profile.task_types)}."

    def _build_task_message(
        self,
        task: dict[str, Any],
        memory_context: list[dict[str, Any]],
        skill_context: list[dict[str, Any]],
    ) -> str:
        parts: list[str] = []
        parts.append(f"## Task\n**{task.get('title', 'Untitled')}**\n{task.get('description', '')}")

        if memory_context:
            parts.append("\n## Relevant Context (from memory)")
            for mem in memory_context[:6]:
                content = mem.get("content", "")
                mem_type = mem.get("memory_type", "")
                parts.append(f"- [{mem_type}] {content[:200]}")

        if skill_context:
            parts.append("\n## Relevant Skills")
            for skill in skill_context[:3]:
                parts.append(f"- {skill.get('name', '')}: {skill.get('description', '')[:150]}")

        parts.append(
            "\n## Instructions\n"
            "Analyze the task, use your tools to accomplish it, verify the result, "
            "then call task_complete with a summary. Start now."
        )
        return "\n".join(parts)

    async def _record_call(
        self,
        *,
        model_name: str,
        task_id: UUID | None,
        task_run_id: UUID | None,
        messages: list[dict[str, Any]],
        response_text: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        duration_ms: int,
    ) -> None:
        try:
            from db.models import ModelCall
            from services.model_router import infer_provider

            async with self._session_factory() as session:
                call = ModelCall(
                    task_id=task_id,
                    task_run_id=task_run_id,
                    agent_slug=self._profile.slug if self._profile else "hermes_agent",
                    model_name=model_name,
                    provider=infer_provider(model_name),
                    request_json={"message_count": len(messages)},
                    response_text=response_text[:2000],
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=(prompt_tokens or 0) + (completion_tokens or 0),
                    latency_ms=duration_ms,
                    status="completed",
                )
                session.add(call)
                await session.commit()
        except Exception:
            logger.debug("Failed to record model call", exc_info=True)


# ── Legacy adapter ────────────────────────────────────────────────────


class LiteLLMPlanningModel:
    """
    Legacy adapter: wraps HermesModelClient to implement PlanningModel.
    Returns a single meta-step that tells the executor to use the
    Hermes conversation loop.
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        profile: AgentProfile | None = None,
        model_router: ModelRouter | None = None,
        model_override: str | None = None,
    ) -> None:
        self._hermes = HermesModelClient(
            settings,
            session_factory,
            profile=profile,
            model_router=model_router,
            model_override=model_override,
        )
        self._settings = settings
        self._profile = profile
        self._model_override = model_override

    @property
    def hermes_client(self) -> HermesModelClient:
        return self._hermes

    async def create_plan(
        self,
        *,
        task: dict[str, Any],
        memory_context: list[dict[str, Any]],
        skill_context: list[dict[str, Any]],
        task_run_id: UUID | None,
    ) -> list[PlannedStep]:
        return [
            PlannedStep(
                title="Execute with Hermes conversation loop",
                description="Using native tool calling with iterative execution.",
                tool_name="__hermes_loop__",
                reason="Hermes-style autonomous execution",
            )
        ]

