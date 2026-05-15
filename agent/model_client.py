from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from db.models import ModelCall


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
    def __init__(self, settings: Settings, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._settings = settings
        self._session_factory = session_factory

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
        status = "failed"
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        total_tokens: int | None = None

        start = time.perf_counter()
        try:
            from litellm import acompletion

            response = await acompletion(
                model=self._settings.litellm_model,
                api_key=self._settings.litellm_api_key or None,
                temperature=0.2,
                messages=self._build_messages(
                    task=task,
                    memory_context=memory_context,
                    skill_context=skill_context,
                ),
            )
            response_text = self._extract_response_text(response)
            prompt_tokens, completion_tokens, total_tokens = self._extract_usage(response)
            planned_steps = self._parse_response(response_text)
            status = "completed"
        except Exception as exc:
            response_text = f"LiteLLM planning failed: {exc}"
            planned_steps = self._fallback_plan(task)

        latency_ms = max(1, int((time.perf_counter() - start) * 1000))
        await self._record_model_call(
            task_id=UUID(str(task["id"])),
            task_run_id=task_run_id,
            request_json=request_json,
            response_text=response_text,
            status=status,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )
        return planned_steps

    def _build_messages(
        self,
        *,
        task: dict[str, Any],
        memory_context: list[dict[str, Any]],
        skill_context: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        system_prompt = (
            "You are the planning component for a private AI agent operating system. "
            "Create a simple step-by-step plan for the task. "
            "Only use these tool names when needed: utc_now, health_snapshot, shell_command. "
            "Use shell_command only when another tool cannot solve the task. "
            "Return strict JSON with a top-level 'steps' array. "
            "Each step must include: title, description, tool_name, command, reason. "
            "Use null for tool_name and command when the step is reasoning-only. "
            "Do not include markdown fences."
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

    def _extract_response_text(self, response: Any) -> str:
        if isinstance(response, dict):
            choices = response.get("choices", [])
            if choices:
                message = choices[0].get("message", {})
                return str(message.get("content", ""))

        choices = getattr(response, "choices", None)
        if choices:
            message = getattr(choices[0], "message", None)
            if message is not None:
                content = getattr(message, "content", "")
                return str(content)
        return ""

    def _extract_usage(self, response: Any) -> tuple[int | None, int | None, int | None]:
        usage = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
        if usage is None:
            return None, None, None

        if isinstance(usage, dict):
            return usage.get("prompt_tokens"), usage.get("completion_tokens"), usage.get("total_tokens")

        return (
            getattr(usage, "prompt_tokens", None),
            getattr(usage, "completion_tokens", None),
            getattr(usage, "total_tokens", None),
        )

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

    async def _record_model_call(
        self,
        *,
        task_id: UUID,
        task_run_id: UUID | None,
        request_json: dict[str, Any],
        response_text: str,
        status: str,
        latency_ms: int,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
    ) -> None:
        async with self._session_factory() as session:
            session.add(
                ModelCall(
                    task_id=task_id,
                    task_run_id=task_run_id,
                    provider=self._infer_provider(self._settings.litellm_model),
                    model_name=self._settings.litellm_model,
                    request_json=request_json,
                    response_text=response_text,
                    status=status,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    latency_ms=latency_ms,
                )
            )
            await session.commit()

    def _infer_provider(self, model_name: str) -> str:
        if "/" in model_name:
            return model_name.split("/", maxsplit=1)[0]
        lowered = model_name.lower()
        if lowered.startswith(("gpt", "o1", "o3", "o4")):
            return "openai"
        if lowered.startswith("claude"):
            return "anthropic"
        if lowered.startswith("gemini"):
            return "gemini"
        if lowered.startswith(("ollama", "llama", "mistral", "qwen")):
            return "local"
        return "litellm"