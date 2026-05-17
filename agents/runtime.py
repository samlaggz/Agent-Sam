from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.graph import AgentGraphRunner, AgentRunResult
from agent.model_client import LiteLLMPlanningModel
from agents.registry import get_agent_profile
from agents.router import RouteRequest, RouterAgent
from app.config import Settings, get_settings
from db.models import AgentEvaluation, AgentRun, ModelCall, Task, TaskRun
from services.agent_learning_service import extract_learning_from_completed_task
from services.budget_service import BudgetService
from services.model_router import ModelRouter


@dataclass(frozen=True)
class SpecialistRuntimeDecision:
    agent_slug: str
    model: str
    reason: str
    estimated_cost_usd: float | None


class SpecialistAgentRuntime:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, settings: Settings) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._router = RouterAgent()
        self._model_router = ModelRouter(settings, session_factory)
        self._budget_service = BudgetService(settings, session_factory)

    async def run_task(self, task_id: UUID) -> AgentRunResult:
        async with self._session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise ValueError(f"Task {task_id} does not exist.")
            task_run = await session.scalar(
                select(TaskRun).where(TaskRun.task_id == task.id).order_by(TaskRun.attempt_number.desc()).limit(1)
            )

            route_request = RouteRequest(
                title=task.title,
                description=task.description or "",
                metadata=dict(task.metadata_json or {}),
                requested_agent_slug=(task.metadata_json or {}).get("requested_agent"),
                premium_requested=bool((task.metadata_json or {}).get("premium_requested", False)),
                safety_critical=bool(task.priority == "urgent" or (task.metadata_json or {}).get("safety_critical", False)),
            )
            route_decision = self._router.route(route_request)
            profile = get_agent_profile(route_decision.agent_slug)
            budget_decision = await self._budget_service.choose_model(
                profile,
                requested_model=route_decision.model,
                estimated_cost_usd=_estimate_cost_hint(route_decision.estimated_cost_level),
                task_id=task.id,
                premium_requested=route_request.premium_requested,
                safety_critical=route_decision.requires_human_approval,
            )

            metadata_json = dict(task.metadata_json or {})
            metadata_json.update(
                {
                    "routed_agent": route_decision.agent_slug,
                    "routed_model": budget_decision.approved_model,
                    "routing_reason": route_decision.reason,
                    "estimated_cost": budget_decision.estimated_cost_usd,
                }
            )
            task.metadata_json = metadata_json

            agent_run = AgentRun(
                task_id=task.id,
                task_run_id=task_run.id if task_run is not None else None,
                agent_slug=route_decision.agent_slug,
                model=budget_decision.approved_model,
                status="running",
                started_at=datetime.now(timezone.utc),
                input_summary=f"{task.title}\n{task.description or ''}".strip(),
                cost_estimate=budget_decision.estimated_cost_usd,
                metadata_json={
                    "routing_reason": route_decision.reason,
                    "requires_human_approval": route_decision.requires_human_approval,
                },
            )
            session.add(agent_run)
            await session.commit()
            await session.refresh(agent_run)

        planning_model = LiteLLMPlanningModel(
            self._settings,
            self._session_factory,
            profile=profile,
            model_router=self._model_router,
            model_override=budget_decision.approved_model,
        )
        runner = AgentGraphRunner(
            self._session_factory,
            settings=self._settings,
            planning_model=planning_model,
            skills_root=Path.cwd() / "skills",
            allowed_tool_roots=_default_allowed_tool_roots(),
        )
        result = await runner.run_task(task_id)
        actual_cost = await self._finalize_agent_run(task_id=task_id, agent_run_id=agent_run.id, result=result)
        return AgentRunResult(
            task_id=task_id,
            status=result.status,
            final_summary=result.final_summary,
            approval_id=result.approval_id,
            routed_agent=route_decision.agent_slug,
            routed_model=budget_decision.approved_model,
            routing_reason=route_decision.reason,
            estimated_cost_usd=budget_decision.estimated_cost_usd,
            actual_cost_usd=actual_cost,
        )

    async def _finalize_agent_run(self, *, task_id: UUID, agent_run_id: UUID, result: AgentRunResult) -> float | None:
        async with self._session_factory() as session:
            task = await session.get(Task, task_id)
            agent_run = await session.get(AgentRun, agent_run_id)
            if task is None or agent_run is None:
                return None
            cost_query = select(func.sum(ModelCall.actual_cost_usd))
            if agent_run.task_run_id is not None:
                cost_query = cost_query.where(ModelCall.task_run_id == agent_run.task_run_id)
            else:
                cost_query = cost_query.where(ModelCall.task_id == task_id)
            actual_cost = await session.scalar(cost_query)
            actual_cost_value = float(actual_cost) if actual_cost is not None else None
            agent_run.status = result.status
            agent_run.completed_at = datetime.now(timezone.utc)
            agent_run.output_summary = result.final_summary
            agent_run.actual_cost = actual_cost_value
            task.metadata_json = {
                **dict(task.metadata_json or {}),
                "actual_cost": actual_cost_value,
                "final_agent_status": result.status,
            }
            if self._should_run_qa(task, agent_run):
                qa_profile = get_agent_profile("qa_reviewer_agent")
                session.add(
                    AgentEvaluation(
                        agent_run_id=agent_run.id,
                        evaluator_slug=qa_profile.slug,
                        status="completed",
                        checklist_json=list(qa_profile.evaluation_checklist),
                        notes=f"QA review requested for {agent_run.agent_slug}; final status {result.status}.",
                        metadata_json={"risk_level": get_agent_profile(agent_run.agent_slug).risk_level},
                    )
                )
            await session.commit()
            if result.status == "completed":
                await extract_learning_from_completed_task(session, task_id, agent_run.agent_slug)
                await session.commit()
            if actual_cost_value is not None:
                await self._budget_service.record_actual_cost(model_name=agent_run.model, amount=actual_cost_value)
            return actual_cost_value

    def _should_run_qa(self, task: Task, agent_run: AgentRun) -> bool:
        return bool(task.priority == "urgent" or get_agent_profile(agent_run.agent_slug).risk_level == "high")


def _estimate_cost_hint(cost_level: str) -> float:
    return {"low": 0.01, "medium": 0.05, "high": 0.12}.get(cost_level, 0.05)


def _default_allowed_tool_roots() -> list[Path]:
    cwd = Path.cwd().resolve()
    roots = [cwd]
    if os.name != "nt":
        roots.extend(Path(path) for path in ("/opt", "/srv", "/var", "/etc", "/home", "/root"))
    unique_roots: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if root in seen:
            continue
        unique_roots.append(root)
        seen.add(root)
    return unique_roots


_default_specialist_runtime: SpecialistAgentRuntime | None = None


def get_default_specialist_runtime() -> SpecialistAgentRuntime:
    global _default_specialist_runtime
    if _default_specialist_runtime is None:
        from db.session import AsyncSessionLocal

        _default_specialist_runtime = SpecialistAgentRuntime(AsyncSessionLocal, settings=get_settings())
    return _default_specialist_runtime