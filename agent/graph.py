from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.model_client import LiteLLMPlanningModel, PlanningModel
from agent.nodes import AgentNodeHandlers
from agent.progress import ProgressReporter, TaskProgressReporter
from agent.state import AgentState
from app.config import Settings, get_settings


@dataclass(frozen=True)
class AgentRunResult:
    task_id: UUID
    status: str
    final_summary: str
    approval_id: UUID | None = None
    routed_agent: str | None = None
    routed_model: str | None = None
    routing_reason: str | None = None
    estimated_cost_usd: float | None = None
    actual_cost_usd: float | None = None


def build_agent_graph(handlers: AgentNodeHandlers):
    workflow = StateGraph(AgentState)
    workflow.add_node("load_task", handlers.load_task)
    workflow.add_node("build_context", handlers.build_context)
    workflow.add_node("plan", handlers.plan)
    workflow.add_node("execute_step", handlers.execute_step)
    workflow.add_node("save_memory", handlers.save_memory)
    workflow.add_node("report_result", handlers.report_result)
    workflow.add_edge(START, "load_task")
    workflow.add_edge("load_task", "build_context")
    workflow.add_edge("build_context", "plan")
    workflow.add_edge("plan", "execute_step")
    workflow.add_edge("execute_step", "save_memory")
    workflow.add_edge("save_memory", "report_result")
    workflow.add_edge("report_result", END)
    return workflow.compile()


class AgentGraphRunner:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        settings: Settings,
        planning_model: PlanningModel | None = None,
        progress_reporter: ProgressReporter | None = None,
        skills_root: str | Path | None = None,
        allowed_tool_roots: Sequence[str | Path] | None = None,
    ) -> None:
        model = planning_model or LiteLLMPlanningModel(settings, session_factory)
        reporter = progress_reporter or TaskProgressReporter(session_factory, settings)
        handlers = AgentNodeHandlers(
            session_factory,
            planning_model=model,
            progress_reporter=reporter,
            skills_root=Path(skills_root or Path.cwd() / "skills"),
            allowed_tool_roots=allowed_tool_roots,
        )
        self._graph = build_agent_graph(handlers)

    async def run_task(self, task_id: UUID) -> AgentRunResult:
        final_state = await self._graph.ainvoke({"task_id": str(task_id), "step_summaries": []})
        approval_id = final_state.get("pending_approval_id")
        return AgentRunResult(
            task_id=task_id,
            status=str(final_state.get("final_status", "failed")),
            final_summary=str(final_state.get("final_summary", "Task execution failed.")),
            approval_id=UUID(approval_id) if approval_id else None,
        )


_default_agent_runner: AgentGraphRunner | None = None


def get_default_agent_runner() -> AgentGraphRunner:
    global _default_agent_runner
    if _default_agent_runner is None:
        from db.session import AsyncSessionLocal

        settings = get_settings()
        _default_agent_runner = AgentGraphRunner(
            AsyncSessionLocal,
            settings=settings,
            skills_root=Path.cwd() / "skills",
            allowed_tool_roots=[Path.cwd()],
        )
    return _default_agent_runner
