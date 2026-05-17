from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from agents.registry import get_agent_profile
from app.config import Settings
from services.budget_service import BudgetService
from services.model_router import ModelExecutionRequest, ModelRouter


@pytest.mark.asyncio
async def test_model_router_uses_openrouter_model_string(monkeypatch, session_factory) -> None:
    observed_kwargs: dict = {}

    async def fake_acompletion(**kwargs):
        observed_kwargs.update(kwargs)
        return {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        }

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)

    settings = Settings(openrouter_api_key="test-key", openrouter_base_url="https://openrouter.ai/api/v1")
    router = ModelRouter(settings, session_factory)
    request = ModelExecutionRequest(
        agent_slug="coding_agent",
        model="openrouter/openai/gpt-4.1-mini",
        messages=[{"role": "user", "content": "hello"}],
        task_id=uuid4(),
    )

    result = await router.run_completion(request)

    assert result.success is True
    assert observed_kwargs["model"] == "openrouter/openai/gpt-4.1-mini"
    assert observed_kwargs["api_key"] == "test-key"
    assert observed_kwargs["base_url"] == "https://openrouter.ai/api/v1"


@pytest.mark.asyncio
async def test_budget_service_downgrades_or_escalates(session_factory) -> None:
    settings = Settings(max_cost_per_task_usd=0.03, daily_model_budget_usd=0.03)
    service = BudgetService(settings, session_factory)
    profile = get_agent_profile("coding_agent")

    escalated = await service.choose_model(
        profile,
        task_id=uuid4(),
        failed_attempt=True,
        estimated_cost_usd=0.02,
    )
    downgraded = await service.choose_model(
        profile,
        task_id=uuid4(),
        requested_model=profile.escalation_model,
        estimated_cost_usd=0.09,
    )

    assert escalated.approved_model == profile.escalation_model
    assert escalated.escalated is True
    assert downgraded.approved_model in {profile.default_model, *profile.fallback_models}
    assert downgraded.downgraded is True or downgraded.requires_human_approval is True