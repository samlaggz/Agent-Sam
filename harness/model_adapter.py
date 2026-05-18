from __future__ import annotations

from abc import ABC, abstractmethod
import json
from typing import Any, Mapping, Sequence
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agents.registry import get_agent_profile
from app.config import Settings
from harness.actions import AnyAction, parse_action
from services.model_router import ModelExecutionRequest, ModelRouter


class ModelRequest(BaseModel):
    agent_slug: str
    task_id: UUID | None = None
    agent_run_id: UUID | None = None
    system_prompt: str
    user_prompt: str
    messages: list[Mapping[str, str]] = Field(default_factory=list)
    available_tools: list[str] = Field(default_factory=list)
    max_tokens: int | None = None
    structured_json: bool = True
    current_cost_usd: float = 0.0
    max_cost_usd: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelResponse(BaseModel):
    model: str
    content: str
    action: AnyAction | None = None
    parsed_json: dict[str, Any] | None = None
    actual_cost_usd: float | None = None
    duration_ms: int | None = None
    success: bool = True
    error: str | None = None


class ModelAdapter(ABC):
    @abstractmethod
    async def next_action(self, request: ModelRequest) -> ModelResponse:
        raise NotImplementedError


class LiteLLMModelAdapter(ModelAdapter):
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        model_router: ModelRouter | None = None,
    ) -> None:
        self._settings = settings
        self._model_router = model_router or ModelRouter(settings, session_factory)

    async def next_action(self, request: ModelRequest) -> ModelResponse:
        if request.max_cost_usd is not None and request.current_cost_usd >= request.max_cost_usd:
            return ModelResponse(
                model="budget_guard",
                content="",
                success=False,
                error="Task exceeded budget before requesting another model call.",
            )
        profile = get_agent_profile(request.agent_slug)
        model_name = self._model_router.choose_model(profile)
        messages = [
            {"role": "system", "content": request.system_prompt},
            *[dict(message) for message in request.messages],
            {
                "role": "user",
                "content": (
                    f"Task context:\n{request.user_prompt}\n\n"
                    f"Available tools: {', '.join(request.available_tools) or 'none'}\n"
                    "Return a single JSON object with an action_type and any required fields."
                ),
            },
        ]
        result = await self._model_router.run_completion(
            ModelExecutionRequest(
                agent_slug=request.agent_slug,
                model=model_name,
                task_id=request.task_id,
                task_run_id=request.agent_run_id,
                messages=messages,
                max_tokens=request.max_tokens,
                fallback_models=tuple(profile.fallback_models),
                metadata={**request.metadata, "structured_json": request.structured_json},
            )
        )
        try:
            parsed = _extract_json_object(result.content)
            action = parse_action(parsed)
            return ModelResponse(
                model=result.model,
                content=result.content,
                action=action,
                parsed_json=parsed,
                actual_cost_usd=result.actual_cost_usd,
                duration_ms=result.duration_ms,
            )
        except Exception as exc:
            return ModelResponse(
                model=result.model,
                content=result.content,
                actual_cost_usd=result.actual_cost_usd,
                duration_ms=result.duration_ms,
                success=False,
                error=str(exc),
            )


def _extract_json_object(content: str) -> dict[str, Any]:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        stripped = stripped.split("\n", maxsplit=1)[-1]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("Model response did not contain a JSON object.")
    return json.loads(stripped[start : end + 1])