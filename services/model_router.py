from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agents.base import AgentProfile
from app.config import Settings
from db.models import ModelCall


@dataclass(frozen=True)
class ModelExecutionRequest:
    agent_slug: str
    model: str
    messages: Sequence[Mapping[str, str]]
    task_id: UUID | None = None
    task_run_id: UUID | None = None
    temperature: float = 0.0
    max_tokens: int | None = None
    fallback_models: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelExecutionResult:
    model: str
    provider: str
    content: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    estimated_cost_usd: float | None
    actual_cost_usd: float | None
    duration_ms: int
    success: bool
    raw_response: Any


@dataclass(frozen=True)
class ModelValidationResult:
    model: str
    valid: bool
    provider: str
    reason: str


class ModelRouter:
    def __init__(self, settings: Settings, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._settings = settings
        self._session_factory = session_factory

    async def run_completion(self, request: ModelExecutionRequest) -> ModelExecutionResult:
        errors: list[str] = []
        models_to_try = (request.model, *request.fallback_models)
        for model_name in models_to_try:
            provider = infer_provider(model_name)
            start = time.perf_counter()
            request_json = {
                "agent_slug": request.agent_slug,
                "messages": [dict(message) for message in request.messages],
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
                "metadata": dict(request.metadata),
            }
            try:
                from litellm import acompletion

                response = await acompletion(**self._build_completion_kwargs(model_name, request))
                content = extract_response_text(response)
                prompt_tokens, completion_tokens, total_tokens = extract_usage(response)
                actual_cost_usd = estimate_completion_cost(
                    response=response,
                    model=model_name,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
                duration_ms = max(1, int((time.perf_counter() - start) * 1000))
                await self._record_model_call(
                    request=request,
                    model_name=model_name,
                    provider=provider,
                    request_json=request_json,
                    response_text=content,
                    status="completed",
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    estimated_cost_usd=actual_cost_usd,
                    actual_cost_usd=actual_cost_usd,
                    latency_ms=duration_ms,
                    error_text=None,
                )
                return ModelExecutionResult(
                    model=model_name,
                    provider=provider,
                    content=content,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    estimated_cost_usd=actual_cost_usd,
                    actual_cost_usd=actual_cost_usd,
                    duration_ms=duration_ms,
                    success=True,
                    raw_response=response,
                )
            except Exception as exc:
                duration_ms = max(1, int((time.perf_counter() - start) * 1000))
                error_text = str(exc)
                errors.append(f"{model_name}: {error_text}")
                await self._record_model_call(
                    request=request,
                    model_name=model_name,
                    provider=provider,
                    request_json=request_json,
                    response_text="",
                    status="failed",
                    prompt_tokens=None,
                    completion_tokens=None,
                    total_tokens=None,
                    estimated_cost_usd=None,
                    actual_cost_usd=None,
                    latency_ms=duration_ms,
                    error_text=error_text,
                )
        raise RuntimeError("; ".join(errors) or "Model completion failed")

    def choose_model(
        self,
        profile: AgentProfile,
        *,
        low_confidence: bool = False,
        failed_attempt: bool = False,
        high_complexity: bool = False,
        premium_requested: bool = False,
        safety_critical: bool = False,
    ) -> str:
        if not self._settings.allow_model_escalation:
            return profile.default_model
        if any((low_confidence, failed_attempt, high_complexity, premium_requested, safety_critical)):
            return profile.escalation_model
        return profile.default_model

    def list_configured_models(self, profiles: Sequence[AgentProfile]) -> tuple[str, ...]:
        models: list[str] = []
        for profile in profiles:
            models.append(profile.default_model)
            models.extend(profile.fallback_models)
            models.append(profile.escalation_model)
        normalized: list[str] = []
        seen: set[str] = set()
        for model_name in models:
            cleaned = model_name.strip()
            if not cleaned or cleaned in seen:
                continue
            normalized.append(cleaned)
            seen.add(cleaned)
        return tuple(normalized)

    def validate_model_name(self, model_name: str) -> ModelValidationResult:
        cleaned = model_name.strip()
        if not cleaned:
            return ModelValidationResult(model=model_name, valid=False, provider="unknown", reason="model is empty")
        provider = infer_provider(cleaned)
        if provider == "openrouter" and cleaned.count("/") < 2:
            return ModelValidationResult(
                model=cleaned,
                valid=False,
                provider=provider,
                reason="OpenRouter models must use openrouter/provider/model-name syntax",
            )
        if provider != "openrouter" and "/" not in cleaned and cleaned.count("-") < 1:
            return ModelValidationResult(
                model=cleaned,
                valid=False,
                provider=provider,
                reason="model name is too ambiguous",
            )
        return ModelValidationResult(model=cleaned, valid=True, provider=provider, reason="ok")

    async def validate_models(self, model_names: Sequence[str], *, live: bool = False) -> tuple[ModelValidationResult, ...]:
        validations = [self.validate_model_name(model_name) for model_name in model_names]
        if not live:
            return tuple(validations)

        live_results: list[ModelValidationResult] = []
        for validation in validations:
            if not validation.valid:
                live_results.append(validation)
                continue
            request = ModelExecutionRequest(
                agent_slug="router_agent",
                model=validation.model,
                messages=[{"role": "user", "content": "Reply with the word ok."}],
                temperature=0.0,
                max_tokens=8,
            )
            try:
                await self.run_completion(request)
            except Exception as exc:
                live_results.append(
                    ModelValidationResult(
                        model=validation.model,
                        valid=False,
                        provider=validation.provider,
                        reason=f"live validation failed: {exc}",
                    )
                )
            else:
                live_results.append(validation)
        return tuple(live_results)

    def _build_completion_kwargs(self, model_name: str, request: ModelExecutionRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model_name,
            "messages": [dict(message) for message in request.messages],
            "temperature": request.temperature,
        }
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens

        provider = infer_provider(model_name)
        if provider == "openrouter":
            kwargs["api_key"] = self._settings.openrouter_api_key or self._settings.litellm_api_key or None
            kwargs["base_url"] = self._settings.openrouter_base_url
        elif provider == "openai":
            kwargs["api_key"] = self._settings.openai_api_key or self._settings.litellm_api_key or None
        elif provider == "anthropic":
            kwargs["api_key"] = self._settings.anthropic_api_key or self._settings.litellm_api_key or None
        elif provider == "gemini":
            kwargs["api_key"] = self._settings.gemini_api_key or self._settings.litellm_api_key or None
        elif provider == "ollama":
            kwargs["base_url"] = self._settings.ollama_base_url
        return kwargs

    async def _record_model_call(
        self,
        *,
        request: ModelExecutionRequest,
        model_name: str,
        provider: str,
        request_json: Mapping[str, Any],
        response_text: str,
        status: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
        estimated_cost_usd: float | None,
        actual_cost_usd: float | None,
        latency_ms: int,
        error_text: str | None,
    ) -> None:
        async with self._session_factory() as session:
            session.add(
                ModelCall(
                    task_id=request.task_id,
                    task_run_id=request.task_run_id,
                    provider=provider,
                    model_name=model_name,
                    request_json=json.loads(json.dumps(dict(request_json))),
                    response_text=response_text,
                    status=status,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    estimated_cost_usd=estimated_cost_usd,
                    actual_cost_usd=actual_cost_usd,
                    latency_ms=latency_ms,
                    error_text=error_text,
                )
            )
            await session.commit()


def infer_provider(model_name: str) -> str:
    cleaned = model_name.strip().lower()
    if cleaned.startswith("openrouter/"):
        return "openrouter"
    if cleaned.startswith(("openai/", "gpt", "o1", "o3", "o4")):
        return "openai"
    if cleaned.startswith(("anthropic/", "claude")):
        return "anthropic"
    if cleaned.startswith(("gemini/", "google/")):
        return "gemini"
    if cleaned.startswith(("ollama/", "llama", "mistral", "qwen")):
        return "ollama"
    return cleaned.split("/", maxsplit=1)[0] if "/" in cleaned else "litellm"


def extract_response_text(response: Any) -> str:
    if isinstance(response, dict):
        choices = response.get("choices", [])
        if choices:
            message = choices[0].get("message", {})
            return str(message.get("content", ""))
    choices = getattr(response, "choices", None)
    if choices:
        message = getattr(choices[0], "message", None)
        if message is not None:
            return str(getattr(message, "content", ""))
    return ""


def extract_usage(response: Any) -> tuple[int | None, int | None, int | None]:
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


def estimate_completion_cost(
    *,
    response: Any,
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> float | None:
    try:
        from litellm import completion_cost

        return float(
            completion_cost(
                completion_response=response,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        )
    except Exception:
        return None