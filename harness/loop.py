from __future__ import annotations

from time import monotonic
from uuid import UUID

from agent.graph import AgentRunResult
from app.config import Settings
from db.models import Task
from harness.actions import FinishAction
from harness.events import AgentThoughtEvent, ErrorEvent, EventStore, RunSummaryEvent, UserMessageEvent
from harness.model_adapter import ModelAdapter, ModelRequest
from harness.observations import ApprovalObservation, ErrorObservation
from harness.tool_executor import ToolExecutor
from harness.workspace import WorkspaceManager
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class HarnessLoop:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        workspace_manager: WorkspaceManager,
        event_store: EventStore,
        model_adapter: ModelAdapter,
        tool_executor: ToolExecutor,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._workspace_manager = workspace_manager
        self._event_store = event_store
        self._model_adapter = model_adapter
        self._tool_executor = tool_executor

    async def run_task(
        self,
        *,
        task_id: UUID,
        agent_slug: str,
        agent_run_id: UUID | None = None,
        initial_user_message: str | None = None,
    ) -> AgentRunResult:
        start = monotonic()
        workspace = await self._workspace_manager.create_task_workspace(task_id)
        history = await self._event_store.list_events(task_id=task_id, run_id=agent_run_id)
        task = await self._load_task(task_id)
        if not history:
            first_message = initial_user_message or f"{task.title}\n{task.description or ''}".strip()
            await self._event_store.append(
                UserMessageEvent(task_id=task_id, run_id=agent_run_id, agent_slug=agent_slug, content=first_message)
            )
            history = await self._event_store.list_events(task_id=task_id, run_id=agent_run_id)

        repeated_errors = 0
        current_cost = 0.0
        for iteration in range(self._settings.agent_loop_max_iterations):
            if monotonic() - start > self._settings.agent_loop_max_runtime_seconds:
                await self._event_store.append(
                    ErrorEvent(task_id=task_id, run_id=agent_run_id, agent_slug=agent_slug, content="Harness loop exceeded runtime limit.")
                )
                return await self._finish(task_id, agent_run_id, agent_slug, workspace.events_jsonl, "failed", "Harness loop exceeded runtime limit.")

            response = await self._model_adapter.next_action(
                ModelRequest(
                    agent_slug=agent_slug,
                    task_id=task_id,
                    agent_run_id=agent_run_id,
                    system_prompt=self._build_system_prompt(agent_slug),
                    user_prompt=self._build_user_prompt(task, history),
                    messages=[{"role": "assistant", "content": event.content} for event in history[-8:]],
                    available_tools=[tool.name for tool in self._tool_executor.registry.tools_for_agent(agent_slug)],
                    max_tokens=1024,
                    current_cost_usd=current_cost,
                    max_cost_usd=self._settings.max_cost_per_task_usd,
                    metadata={"iteration": iteration},
                )
            )
            if response.actual_cost_usd:
                current_cost += response.actual_cost_usd
            if not response.success or response.action is None:
                repeated_errors += 1
                await self._event_store.append(
                    ErrorEvent(
                        task_id=task_id,
                        run_id=agent_run_id,
                        agent_slug=agent_slug,
                        content=response.error or "Model did not return a valid action.",
                    )
                )
                if repeated_errors >= self._settings.agent_loop_max_repeated_errors:
                    return await self._finish(task_id, agent_run_id, agent_slug, workspace.events_jsonl, "failed", response.error or "Repeated model errors.")
                history = await self._event_store.list_events(task_id=task_id, run_id=agent_run_id)
                continue

            await self._event_store.append(
                AgentThoughtEvent(
                    task_id=task_id,
                    run_id=agent_run_id,
                    agent_slug=agent_slug,
                    content=response.content or response.action.action_type,
                    metadata={"action_type": response.action.action_type},
                )
            )
            if isinstance(response.action, FinishAction):
                return await self._finish(task_id, agent_run_id, agent_slug, workspace.events_jsonl, "completed", response.action.summary)

            observation = await self._tool_executor.execute_action(
                response.action,
                task_id=task_id,
                agent_run_id=agent_run_id,
                agent_slug=agent_slug,
                workspace=workspace,
            )
            if isinstance(observation, ApprovalObservation) and observation.status == "pending":
                return await self._finish(task_id, agent_run_id, agent_slug, workspace.events_jsonl, "paused", observation.summary, approval_id=observation.approval_id)
            if isinstance(observation, ErrorObservation):
                repeated_errors += 1
                if repeated_errors >= self._settings.agent_loop_max_repeated_errors:
                    return await self._finish(task_id, agent_run_id, agent_slug, workspace.events_jsonl, "failed", observation.error_message)
            else:
                repeated_errors = 0
            history = await self._event_store.list_events(task_id=task_id, run_id=agent_run_id)

        return await self._finish(task_id, agent_run_id, agent_slug, workspace.events_jsonl, "failed", "Harness loop exceeded maximum iterations.")

    async def _finish(
        self,
        task_id: UUID,
        agent_run_id: UUID | None,
        agent_slug: str,
        trace_path,
        status: str,
        summary: str,
        *,
        approval_id: UUID | None = None,
    ) -> AgentRunResult:
        await self._event_store.append(
            RunSummaryEvent(task_id=task_id, run_id=agent_run_id, agent_slug=agent_slug, content=summary, metadata={"status": status})
        )
        await self._event_store.export_jsonl_to_path(trace_path, task_id=task_id, run_id=agent_run_id)
        return AgentRunResult(task_id=task_id, status=status, final_summary=summary, approval_id=approval_id)

    async def _load_task(self, task_id: UUID) -> Task:
        async with self._session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise ValueError(f"Task {task_id} does not exist.")
            return task

    def _build_system_prompt(self, agent_slug: str) -> str:
        return (
            f"You are {agent_slug} inside Agent_Sam's harness. "
            "Return exactly one JSON action at a time. Use file patches where possible, "
            "stay inside the workspace, do not expose secrets, and stop for approvals when needed."
        )

    def _build_user_prompt(self, task: Task, history) -> str:
        history_lines = [f"[{event.sequence}] {event.event_type}: {event.content}" for event in history[-12:]]
        return f"Title: {task.title}\nDescription: {task.description or ''}\nHistory:\n" + "\n".join(history_lines)