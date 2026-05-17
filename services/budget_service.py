from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agents.base import AgentProfile
from app.config import Settings
from db.models import ModelBudget


@dataclass(frozen=True)
class BudgetDecision:
    approved_model: str
    estimated_cost_usd: float
    downgraded: bool
    escalated: bool
    requires_human_approval: bool
    reason: str


class BudgetService:
    def __init__(self, settings: Settings, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._settings = settings
        self._session_factory = session_factory

    async def choose_model(
        self,
        profile: AgentProfile,
        *,
        requested_model: str | None = None,
        estimated_cost_usd: float | None = None,
        task_id: UUID | None = None,
        failed_attempt: bool = False,
        low_confidence: bool = False,
        high_complexity: bool = False,
        premium_requested: bool = False,
        safety_critical: bool = False,
    ) -> BudgetDecision:
        model_name = requested_model or profile.default_model
        downgraded = False
        escalated = False

        if self._settings.allow_model_escalation and any(
            (failed_attempt, low_confidence, high_complexity, premium_requested, safety_critical)
        ):
            model_name = profile.escalation_model
            escalated = True

        estimated_cost = estimated_cost_usd if estimated_cost_usd is not None else estimate_model_cost_level(model_name)
        per_task_limit = min(profile.max_cost_per_task_usd, self._settings.max_cost_per_task_usd)
        requires_human_approval = False
        reason = "within budget"

        if estimated_cost > per_task_limit:
            cheaper_option = next((fallback for fallback in profile.fallback_models if estimate_model_cost_level(fallback) <= per_task_limit), None)
            if cheaper_option is not None:
                model_name = cheaper_option
                estimated_cost = estimate_model_cost_level(model_name)
                downgraded = True
                escalated = False
                reason = "downgraded to fit per-task budget"
            else:
                requires_human_approval = True
                reason = "estimated model cost exceeds per-task budget"

        remaining_daily_budget = await self._remaining_daily_budget()
        if remaining_daily_budget is not None and estimated_cost > remaining_daily_budget:
            cheaper_option = next((fallback for fallback in profile.fallback_models if estimate_model_cost_level(fallback) <= remaining_daily_budget), None)
            if cheaper_option is not None:
                model_name = cheaper_option
                estimated_cost = estimate_model_cost_level(model_name)
                downgraded = True
                escalated = False
                reason = "downgraded to fit daily budget"
            else:
                requires_human_approval = True
                reason = "estimated model cost exceeds remaining daily budget"

        if task_id is not None:
            await self.record_budget_decision(task_id=task_id, profile=profile, decision_model=model_name, amount=estimated_cost, reason=reason)

        return BudgetDecision(
            approved_model=model_name,
            estimated_cost_usd=estimated_cost,
            downgraded=downgraded,
            escalated=escalated,
            requires_human_approval=requires_human_approval,
            reason=reason,
        )

    async def record_actual_cost(self, *, model_name: str, amount: float) -> None:
        await self._upsert_budget_record(scope="daily", scope_key=self._daily_scope_key(), amount=amount)

    async def record_budget_decision(
        self,
        *,
        task_id: UUID,
        profile: AgentProfile,
        decision_model: str,
        amount: float,
        reason: str,
    ) -> None:
        await self._upsert_budget_record(
            scope="task",
            scope_key=str(task_id),
            amount=amount,
            budget_limit=min(profile.max_cost_per_task_usd, self._settings.max_cost_per_task_usd),
            metadata={"agent_slug": profile.slug, "model": decision_model, "reason": reason},
        )

    async def _remaining_daily_budget(self) -> float | None:
        spent = 0.0
        async with self._session_factory() as session:
            row = await session.scalar(
                select(ModelBudget).where(
                    ModelBudget.scope == "daily",
                    ModelBudget.scope_key == self._daily_scope_key(),
                )
            )
            if row is not None:
                spent = float(row.spent_usd)
        return max(0.0, self._settings.daily_model_budget_usd - spent)

    async def _upsert_budget_record(
        self,
        *,
        scope: str,
        scope_key: str,
        amount: float,
        budget_limit: float | None = None,
        metadata: dict | None = None,
    ) -> None:
        async with self._session_factory() as session:
            record = await session.scalar(
                select(ModelBudget).where(ModelBudget.scope == scope, ModelBudget.scope_key == scope_key)
            )
            if record is None:
                now = datetime.now(timezone.utc)
                record = ModelBudget(
                    scope=scope,
                    scope_key=scope_key,
                    budget_usd=budget_limit if budget_limit is not None else self._settings.daily_model_budget_usd,
                    spent_usd=0.0,
                    period_start=now if scope == "daily" else None,
                    period_end=(now + timedelta(days=1)) if scope == "daily" else None,
                    metadata_json=metadata or {},
                )
                session.add(record)
            record.spent_usd = float(record.spent_usd) + max(0.0, amount)
            if metadata:
                updated_metadata = dict(record.metadata_json or {})
                updated_metadata.update(metadata)
                record.metadata_json = updated_metadata
            await session.commit()

    def _daily_scope_key(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def estimate_model_cost_level(model_name: str) -> float:
    lowered = model_name.lower()
    if any(token in lowered for token in ("mini", "haiku", "flash", "small")):
        return 0.01
    if any(token in lowered for token in ("sonnet", "medium", "turbo", "large")):
        return 0.05
    return 0.12