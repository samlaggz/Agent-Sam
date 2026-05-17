from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from agents.base import AgentProfile
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from services.model_router import ModelExecutionRequest, ModelRouter


@dataclass(frozen=True)
class PlannedStep:
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
    ) -> list[PlannedStep]:
        ...


class LiteLLMPlanningModel:
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

    async def create_plan(
        self,
        *,
        task: dict[str, Any],
        memory_context: list[dict[str, Any]],
        skill_context: list[dict[str, Any]],
        task_run_id: UUID | None,
    ) -> list[PlannedStep]:
        request_json = self._build_request_payload(
            task=task,
            memory_context=memory_context,
            skill_context=skill_context,
        )
        response_text = ""

        try:
            response = await self._model_router.run_completion(
                ModelExecutionRequest(
                    agent_slug=self._profile.slug if self._profile is not None else "planning_agent",
                    model=self._model_override or self._resolve_default_model(),
                    fallback_models=self._profile.fallback_models if self._profile is not None else (),
                    messages=self._build_messages(
                        task=task,
                        memory_context=memory_context,
                        skill_context=skill_context,
                    ),
                    task_id=UUID(str(task["id"])),
                    task_run_id=task_run_id,
                    temperature=self._profile.temperature if self._profile is not None else 0.2,
                    max_tokens=self._profile.max_tokens_per_run if self._profile is not None else None,
                    metadata={"planner": self._profile.slug if self._profile is not None else "legacy"},
                )
            )
            response_text = response.content
            planned_steps = self._parse_response(response_text)
        except Exception as exc:
            response_text = f"LiteLLM planning failed: {exc}"
            planned_steps = self._fallback_plan(task)
        return planned_steps

    def _resolve_default_model(self) -> str:
        if self._profile is not None:
            return self._profile.default_model
        return self._settings.litellm_model

    def _build_messages(
        self,
        *,
        task: dict[str, Any],
        memory_context: list[dict[str, Any]],
        skill_context: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        profile_prompt = self._load_profile_prompt()
        tool_names = self._available_tool_names()
        tool_list = ", ".join(tool_names)
        tool_instructions = [f"Only use these tool names when needed: {tool_list}."]
        if "web_search" in tool_names:
            tool_instructions.append("For web_search, put the search query in the command field.")
        if "web_open" in tool_names:
            tool_instructions.append("For web_open, put the target URL in the command field.")
        if "shell_command" in tool_names:
            tool_instructions.append(
                "Use shell_command only when another tool cannot solve the task and only if policy allows it."
            )
        system_prompt = (
            f"{profile_prompt}\n\n"
            "Create a simple step-by-step plan for the task. "
            + " ".join(tool_instructions)
            + " "
            "Return strict JSON with a top-level 'steps' array. "
            "Each step must include: title, description, tool_name, command, reason. "
            "Use null for tool_name and command when the step is reasoning-only. "
            "Do not include markdown fences. "
            "IMPORTANT RULES: "
            "1. Never use shell_command for internal task queues — those are handled by the gateway. "
            "2. Always use absolute paths. Never use placeholder text like <folder_path>. "
            "3. If the exact path is unknown, first run: find / -name 'name' -type d 2>/dev/null "
            "4. Commands must be valid executable shell commands. "
            "5. ALWAYS add a final verification step that tests the result (e.g. curl, ls, nginx -t). "
            "6. If a step fails, add a fix step — do not just report the failure. "
            "7. Keep plans SHORT: 2-4 steps max for simple tasks, 4-6 for complex ones. "
            "8. To CREATE a file, use tee with heredoc: tee /path/file > /dev/null << 'EOF'\\ncontent\\nEOF "
            "9. Before symlinking or reloading nginx, first remove any broken symlinks: find /etc/nginx/sites-enabled/ -xtype l -delete "
            "10. NEVER cat a file that doesn't exist yet. Create it first."
        )
        user_prompt = json.dumps(
            self._build_request_payload(
                task=task,
                memory_context=memory_context,
                skill_context=skill_context,
            ),
            indent=2,
            sort_keys=True,
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def _load_profile_prompt(self) -> str:
        if self._profile is None:
            return "You are the planning component for a private AI agent operating system."
        return Path(self._profile.system_prompt_path).read_text(encoding="utf-8").strip()

    def _available_tool_names(self) -> tuple[str, ...]:
        if self._profile is None:
            return ("utc_now", "health_snapshot", "shell_command")

        tool_names: list[str] = ["utc_now", "health_snapshot"]
        if any(
            tool_name in self._profile.tools_allowed
            for tool_name in ("safe_shell", "service_control", "file_read", "file_write", "grep", "pytest")
        ):
            tool_names.append("shell_command")
        if self._settings.enable_web_research and "web_search" in self._profile.tools_allowed:
            tool_names.append("web_search")
        if self._settings.enable_web_research and "web_open" in self._profile.tools_allowed:
            tool_names.append("web_open")

        normalized: list[str] = []
        seen: set[str] = set()
        for tool_name in tool_names:
            if tool_name in seen:
                continue
            normalized.append(tool_name)
            seen.add(tool_name)
        return tuple(normalized)

    def _build_request_payload(
        self,
        *,
        task: dict[str, Any],
        memory_context: list[dict[str, Any]],
        skill_context: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "task": task,
            "memory_context": memory_context,
            "skill_context": skill_context,
        }

    def _parse_response(self, response_text: str) -> list[PlannedStep]:
        cleaned = response_text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            cleaned = cleaned.replace("json", "", 1).strip()

        payload = json.loads(cleaned)
        raw_steps = payload.get("steps", payload if isinstance(payload, list) else [])
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError("Planning response did not include any steps.")

        planned_steps: list[PlannedStep] = []
        for raw_step in raw_steps[:6]:
            if not isinstance(raw_step, dict):
                continue
            title = str(raw_step.get("title") or "Unnamed step").strip()
            description = str(raw_step.get("description") or title).strip()
            tool_name = raw_step.get("tool_name")
            command = raw_step.get("command")
            reason = raw_step.get("reason")
            planned_steps.append(
                PlannedStep(
                    title=title,
                    description=description,
                    tool_name=str(tool_name).strip() or None if tool_name is not None else None,
                    command=str(command).strip() or None if command is not None else None,
                    reason=str(reason).strip() or None if reason is not None else None,
                )
            )

        if not planned_steps:
            raise ValueError("Planning response could not be parsed into steps.")
        return planned_steps

    def _fallback_plan(self, task: dict[str, Any]) -> list[PlannedStep]:
        task_title = str(task.get("title") or "Task")
        task_description = str(task.get("description") or task_title)
        return [
            PlannedStep(
                title="Review task details",
                description=f"Review the task request and restate the core objective: {task_description}",
            ),
            PlannedStep(
                title="Capture safe runtime context",
                description="Capture a minimal runtime snapshot before changing anything.",
                tool_name="health_snapshot",
                reason="Gather a safe health signal for the task.",
            ),
            PlannedStep(
                title="Summarize next action",
                description=f"Summarize what was learned while working on {task_title}.",
            ),
        ]

