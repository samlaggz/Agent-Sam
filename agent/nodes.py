from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.model_client import PlannedStep, PlanningModel
from agent.progress import ProgressReporter
from agent.skills import SkillContext, load_skill_context
from agent.state import AgentState, PlanStepState, TaskSnapshot
from app.config import Settings
from db.memory_service import MemoryCreateRequest, ContextMemory, build_task_context, save_memory
from db.models import Approval, Task, TaskRun, TaskStep, ToolCall
from db.repositories import log_tool_call
from tools.registry import build_runtime_tool_registry, get_tool_registry
from tools.safe_tools import SafeTool
from tools.shell_command import ShellCommandRequest, ShellCommandResult, ShellCommandTool
from tools.web_research import WebResearchProvider, WebResearchTool


@dataclass(frozen=True)
class StepExecutionOutcome:
    status: str
    summary: str
    tool_call_id: UUID | None = None
    approval_id: UUID | None = None


class AgentNodeHandlers:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        settings: Settings,
        planning_model: PlanningModel,
        progress_reporter: ProgressReporter,
        skills_root: Path,
        allowed_tool_roots: Sequence[str | Path] | None = None,
        web_research_provider: WebResearchProvider | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._planning_model = planning_model
        self._progress_reporter = progress_reporter
        self._skills_root = Path(skills_root)
        self._safe_tool_registry = get_tool_registry()
        self._runtime_tool_registry = build_runtime_tool_registry(
            session_factory,
            settings=settings,
            allowed_roots=allowed_tool_roots,
            web_research_provider=web_research_provider,
        )
        self._shell_command_tool = cast(ShellCommandTool, self._runtime_tool_registry["shell_command"])
        self._web_research_tool = cast(WebResearchTool | None, self._runtime_tool_registry.get("web_search"))
        roots = list(allowed_tool_roots or [Path.cwd()])
        self._default_working_directory = Path(roots[0]).resolve()

    async def load_task(self, state: AgentState) -> AgentState:
        next_state = dict(state)
        task_id = UUID(str(next_state["task_id"]))

        async with self._session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise ValueError(f"Task {task_id} does not exist.")

            task_run = await session.scalar(
                select(TaskRun)
                .where(TaskRun.task_id == task.id)
                .order_by(TaskRun.attempt_number.desc())
                .limit(1)
            )

        task_snapshot: TaskSnapshot = {
            "id": str(task.id),
            "title": task.title,
            "description": task.description or "",
            "workspace_id": str(task.workspace_id),
            "created_by_user_id": str(task.created_by_user_id) if task.created_by_user_id is not None else None,
            "priority": task.priority,
            "metadata_json": dict(task.metadata_json or {}),
        }
        next_state.update(
            task=task_snapshot,
            task_run_id=str(task_run.id) if task_run is not None else None,
            worker_name=task_run.worker_name if task_run is not None else None,
            final_status="running",
        )

        await self._progress_reporter.report(task_id, f"Agent started: {task.title}", stage="load_task")
        return cast(AgentState, next_state)

    async def build_context(self, state: AgentState) -> AgentState:
        next_state = dict(state)
        task_snapshot = cast(TaskSnapshot, next_state["task"])
        task_id = UUID(task_snapshot["id"])
        task_run_id = self._optional_uuid(next_state.get("task_run_id"))

        async with self._session_factory() as session:
            memory_context = await build_task_context(session, task_id=task_id, limit=6)
            skill_context = await load_skill_context(
                session,
                workspace_id=UUID(task_snapshot["workspace_id"]),
                query_text=f"{task_snapshot['title']}\n{task_snapshot['description']}",
                skills_root=self._skills_root,
                limit=4,
            )

            if task_run_id is not None:
                task_run = await session.get(TaskRun, task_run_id)
                if task_run is not None:
                    task_run.context_json = {
                        **dict(task_run.context_json or {}),
                        "memory_ids": [str(item.memory_id) for item in memory_context],
                        "skill_names": [item.name for item in skill_context],
                    }
                    await session.commit()

        next_state["memory_context"] = [self._serialize_memory(item) for item in memory_context]
        next_state["skill_context"] = [self._serialize_skill(item) for item in skill_context]

        await self._progress_reporter.report(
            task_id,
            f"Context ready: {len(memory_context)} memories and {len(skill_context)} skills loaded.",
            stage="build_context",
        )
        return cast(AgentState, next_state)

    async def plan(self, state: AgentState) -> AgentState:
        next_state = dict(state)
        task_snapshot = cast(TaskSnapshot, next_state["task"])
        task_id = UUID(task_snapshot["id"])

        existing_steps = await self._load_existing_steps(task_id)
        if existing_steps:
            next_state["plan_steps"] = existing_steps
            await self._progress_reporter.report(
                task_id,
                f"Loaded existing plan with {len(existing_steps)} steps.",
                stage="plan",
            )
            return cast(AgentState, next_state)

        planned_steps = await self._planning_model.create_plan(
            task=task_snapshot,
            memory_context=list(next_state.get("memory_context", [])),
            skill_context=list(next_state.get("skill_context", [])),
            task_run_id=self._optional_uuid(next_state.get("task_run_id")),
        )
        if not planned_steps:
            planned_steps = [
                PlannedStep(
                    title="Summarize task",
                    description="Summarize the task and capture the next action.",
                )
            ]

        plan_steps = await self._persist_plan(task_snapshot, planned_steps)
        next_state["plan_steps"] = plan_steps
        await self._progress_reporter.report(
            task_id,
            f"Plan created with {len(plan_steps)} steps.",
            stage="plan",
        )
        return cast(AgentState, next_state)

    async def execute_step(self, state: AgentState) -> AgentState:
        next_state = dict(state)
        task_snapshot = cast(TaskSnapshot, next_state["task"])
        task_id = UUID(task_snapshot["id"])
        worker_name = next_state.get("worker_name")
        step_summaries = list(next_state.get("step_summaries", []))
        plan_steps = [dict(step) for step in next_state.get("plan_steps", [])]

        final_status = "completed"
        final_summary = "Task completed without executable steps."
        pending_approval_id: str | None = None

        for index, step in enumerate(plan_steps):
            if step.get("status") == "completed":
                continue

            if step.get("status") == "pending_approval":
                approval_id_hint = step.get("approval_id")
                if approval_id_hint is not None:
                    tool_call_id_hint = step.get("tool_call_id")
                    if tool_call_id_hint is not None:
                        existing_result = await self._continue_existing_shell_command(
                            UUID(str(tool_call_id_hint)), cast(PlanStepState, step)
                        )
                        if existing_result is not None:
                            updated_step = await self._apply_step_outcome(
                                task_snapshot,
                                cast(PlanStepState, step),
                                existing_result,
                                worker_name=worker_name,
                            )
                            plan_steps[index] = dict(updated_step)
                            step_summaries.append(existing_result.summary)
                            if existing_result.status == "pending_approval":
                                final_status = "paused"
                                final_summary = existing_result.summary
                                pending_approval_id = str(existing_result.approval_id) if existing_result.approval_id is not None else None
                                break
                            if existing_result.status == "failed":
                                final_status = "failed"
                                final_summary = existing_result.summary
                                break
                            continue
                    final_status = "paused"
                    final_summary = f"Step {step.get('position', '?')} is waiting for approval."
                    pending_approval_id = str(approval_id_hint)
                    break

            await self._mark_step_running(step, worker_name=worker_name)
            await self._progress_reporter.report(
                task_id,
                f"Step {step['position']}/{len(plan_steps)} started: {step['title']}",
                stage="execute_step",
            )

            outcome = await self._execute_planned_step(task_snapshot, cast(PlanStepState, step))
            updated_step = await self._apply_step_outcome(
                task_snapshot,
                cast(PlanStepState, step),
                outcome,
                worker_name=worker_name,
            )
            plan_steps[index] = dict(updated_step)
            step_summaries.append(outcome.summary)

            await self._progress_reporter.report(task_id, outcome.summary, stage="step_summary")

            if outcome.status == "pending_approval":
                await self._progress_reporter.report(task_id, outcome.summary, stage="approval_required")
                final_status = "paused"
                final_summary = outcome.summary
                pending_approval_id = str(outcome.approval_id) if outcome.approval_id is not None else None
                break
            if outcome.status == "failed":
                final_status = "failed"
                final_summary = outcome.summary
                break

        if final_status == "completed":
            if step_summaries:
                final_summary = step_summaries[-1]
            elif plan_steps and all(step.get("status") == "completed" for step in plan_steps):
                final_summary = "Task plan was already completed."

        next_state.update(
            plan_steps=cast(list[PlanStepState], plan_steps),
            step_summaries=step_summaries,
            final_status=final_status,
            final_summary=final_summary,
            pending_approval_id=pending_approval_id,
        )
        return cast(AgentState, next_state)

    async def save_memory(self, state: AgentState) -> AgentState:
        next_state = dict(state)
        task_snapshot = cast(TaskSnapshot, next_state["task"])
        task_id = UUID(task_snapshot["id"])
        final_status = str(next_state.get("final_status", "failed"))
        final_summary = str(next_state.get("final_summary", "Task execution did not produce a summary."))

        memory_type = "task_summary"
        if final_status == "paused":
            memory_type = "decision"
        elif final_status == "failed":
            memory_type = "warning"

        async with self._session_factory() as session:
            await save_memory(
                session,
                MemoryCreateRequest(
                    workspace_id=UUID(task_snapshot["workspace_id"]),
                    user_id=self._optional_uuid(task_snapshot.get("created_by_user_id")),
                    task_id=task_id,
                    memory_type=memory_type,
                    scope="task",
                    source="agent",
                    confidence=0.85 if final_status == "completed" else 0.7,
                    content=final_summary,
                    tags=("agent", "final", final_status),
                    metadata_json={
                        "task_run_id": next_state.get("task_run_id"),
                        "pending_approval_id": next_state.get("pending_approval_id"),
                    },
                ),
            )

        return cast(AgentState, next_state)

    async def report_result(self, state: AgentState) -> AgentState:
        next_state = dict(state)
        task_snapshot = cast(TaskSnapshot, next_state["task"])
        task_id = UUID(task_snapshot["id"])
        final_status = str(next_state.get("final_status", "failed"))
        final_summary = str(next_state.get("final_summary", "Task execution failed."))

        if final_status == "completed":
            message = f"Task completed: {task_snapshot['title']}\n{final_summary}"
        elif final_status == "paused":
            approval_hint = next_state.get("pending_approval_id")
            message = f"Task paused pending approval: {task_snapshot['title']}\n{final_summary}"
            if approval_hint:
                message = f"{message}\nApproval ID: {approval_hint}"
        else:
            message = f"Task failed: {task_snapshot['title']}\n{final_summary}"

        await self._progress_reporter.report(task_id, message, stage="report_result")
        return cast(AgentState, next_state)

    async def _load_existing_steps(self, task_id: UUID) -> list[PlanStepState]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(TaskStep)
                .where(TaskStep.task_id == task_id)
                .order_by(TaskStep.position.asc())
            )
            task_steps = result.scalars().all()
            return [self._serialize_task_step(task_step) for task_step in task_steps]

    async def _persist_plan(self, task_snapshot: TaskSnapshot, planned_steps: list[PlannedStep]) -> list[PlanStepState]:
        task_id = UUID(task_snapshot["id"])

        async with self._session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise ValueError(f"Task {task_id} does not exist.")

            persisted_steps: list[PlanStepState] = []
            for position, planned_step in enumerate(planned_steps, start=1):
                subtask = Task(
                    workspace_id=task.workspace_id,
                    created_by_user_id=task.created_by_user_id,
                    parent_task_id=task.id,
                    title=planned_step.title,
                    description=planned_step.description,
                    priority=task.priority,
                    status="paused",
                    metadata_json={
                        "source": "agent_plan",
                        "step_position": position,
                        "tool_name": planned_step.tool_name,
                        "command": planned_step.command,
                    },
                )
                session.add(subtask)
                await session.flush()

                task_step = TaskStep(
                    task_id=task.id,
                    position=position,
                    title=planned_step.title,
                    description=planned_step.description,
                    status="pending",
                    metadata_json={
                        "tool_name": planned_step.tool_name,
                        "command": planned_step.command,
                        "reason": planned_step.reason,
                        "subtask_id": str(subtask.id),
                    },
                )
                session.add(task_step)
                await session.flush()

                persisted_steps.append(self._serialize_task_step(task_step))

            await session.commit()
            return persisted_steps

    async def _mark_step_running(self, step: PlanStepState, *, worker_name: str | None) -> None:
        async with self._session_factory() as session:
            task_step = await session.get(TaskStep, UUID(step["task_step_id"]))
            if task_step is None:
                return

            now = datetime.now(timezone.utc)
            if task_step.started_at is None:
                task_step.started_at = now
            task_step.status = "running"

            subtask = await self._load_subtask(session, step)
            if subtask is not None and subtask.status != "completed":
                if subtask.started_at is None:
                    subtask.started_at = now
                subtask.status = "running"
                subtask.assigned_worker = worker_name
                subtask.completed_at = None

            await session.commit()

    async def _execute_planned_step(self, task_snapshot: TaskSnapshot, step: PlanStepState) -> StepExecutionOutcome:
        tool_name = step.get("tool_name")
        if not tool_name or tool_name in ("null", "none", "None"):
            return StepExecutionOutcome(
                status="completed",
                summary=f"Step {step['position']} completed: {step['description']}",
            )

        if tool_name == "shell_command":
            return await self._execute_shell_command_step(task_snapshot, step)

        if tool_name == "web_search":
            return await self._execute_web_search_step(task_snapshot, step)

        if tool_name == "web_open":
            return await self._execute_web_open_step(task_snapshot, step)

        safe_tool = self._safe_tool_registry.get(tool_name)
        if safe_tool is None:
            return StepExecutionOutcome(
                status="failed",
                summary=f"Step {step['position']} failed: unsupported tool '{tool_name}'.",
            )

        return await self._execute_registered_safe_tool(task_snapshot, step, safe_tool)

    async def _execute_registered_safe_tool(
        self,
        task_snapshot: TaskSnapshot,
        step: PlanStepState,
        safe_tool: SafeTool,
    ) -> StepExecutionOutcome:
        start = time.perf_counter()
        output_text: str | None = None
        stderr_text: str | None = None
        exit_code = 0
        status = "completed"

        try:
            tool_output = safe_tool.handler()
            output_text = json.dumps(tool_output, indent=2, sort_keys=True)
            summary = f"Step {step['position']} completed with {safe_tool.name}."
        except Exception as exc:
            stderr_text = str(exc)
            exit_code = 1
            status = "failed"
            summary = f"Step {step['position']} failed while running {safe_tool.name}: {exc}"

        duration_ms = max(1, int((time.perf_counter() - start) * 1000))

        async with self._session_factory() as session:
            tool_call = await log_tool_call(
                session,
                tool_name=safe_tool.name,
                input_payload={
                    "reason": step.get("reason") or step.get("description"),
                    "step_title": step["title"],
                },
                output_text=output_text,
                stderr_text=stderr_text,
                duration_ms=duration_ms,
                status=status,
                risk_level="safe",
                approved_by_user=True,
                exit_code=exit_code,
                task_id=UUID(task_snapshot["id"]),
            )

        if output_text and status == "completed":
            summary = f"{summary} Output: {self._excerpt(output_text)}"

        return StepExecutionOutcome(
            status=status,
            summary=summary,
            tool_call_id=tool_call.id,
        )

    async def _execute_web_search_step(self, task_snapshot: TaskSnapshot, step: PlanStepState) -> StepExecutionOutcome:
        if self._web_research_tool is None:
            return StepExecutionOutcome(
                status="failed",
                summary=f"Step {step['position']} failed: web_search is unavailable because web research is not enabled.",
            )

        query = (step.get("command") or step.get("reason") or step.get("description") or step["title"]).strip()
        if not query:
            return StepExecutionOutcome(
                status="failed",
                summary=f"Step {step['position']} failed: web_search requires a query string.",
            )

        try:
            result = await self._web_research_tool.search(task_id=UUID(task_snapshot["id"]), task_run_id=None, query=query)
        except Exception as exc:
            return StepExecutionOutcome(
                status="failed",
                summary=f"Step {step['position']} failed while running web_search: {exc}",
            )

        return StepExecutionOutcome(
            status="completed",
            summary=f"Step {step['position']} completed with web_search. {self._excerpt(result.summary)}",
            tool_call_id=result.tool_call_id,
        )

    async def _execute_web_open_step(self, task_snapshot: TaskSnapshot, step: PlanStepState) -> StepExecutionOutcome:
        if self._web_research_tool is None:
            return StepExecutionOutcome(
                status="failed",
                summary=f"Step {step['position']} failed: web_open is unavailable because web research is not enabled.",
            )

        url = (step.get("command") or "").strip()
        if not self._looks_like_http_url(url):
            fallback_url = await self._resolve_recent_web_search_url(task_snapshot)
            if fallback_url is not None:
                url = fallback_url
        if not url:
            return StepExecutionOutcome(
                status="failed",
                summary=f"Step {step['position']} failed: web_open requires a URL in command.",
            )

        try:
            result = await self._web_research_tool.open(task_id=UUID(task_snapshot["id"]), task_run_id=None, url=url)
        except Exception as exc:
            return StepExecutionOutcome(
                status="failed",
                summary=f"Step {step['position']} failed while running web_open: {exc}",
            )

        return StepExecutionOutcome(
            status="completed",
            summary=f"Step {step['position']} completed with web_open. Output: {self._excerpt(result.content)}",
            tool_call_id=result.tool_call_id,
        )

    async def _resolve_recent_web_search_url(self, task_snapshot: TaskSnapshot) -> str | None:
        task_id = UUID(task_snapshot["id"])
        async with self._session_factory() as session:
            result = await session.execute(
                select(ToolCall)
                .where(ToolCall.task_id == task_id, ToolCall.tool_name == "web_search")
                .order_by(ToolCall.created_at.desc())
                .limit(1)
            )
            tool_call = result.scalar_one_or_none()
            if tool_call is None:
                return None

            input_payload = tool_call.input_payload if isinstance(tool_call.input_payload, dict) else {}
            sources = input_payload.get("sources")
            if isinstance(sources, list):
                for source in sources:
                    if isinstance(source, dict):
                        url = str(source.get("url") or "").strip()
                        if self._looks_like_http_url(url):
                            return url

            output_text = tool_call.output_text or ""
            match = re.search(r"https?://\S+", output_text)
            if match:
                return match.group(0).rstrip(")].,;")
        return None

    def _looks_like_http_url(self, value: str) -> bool:
        lowered = value.strip().lower()
        return lowered.startswith("http://") or lowered.startswith("https://")

    async def _execute_shell_command_step(self, task_snapshot: TaskSnapshot, step: PlanStepState) -> StepExecutionOutcome:
        existing_tool_call_id = self._optional_uuid(step.get("tool_call_id"))
        if existing_tool_call_id is not None:
            existing_result = await self._continue_existing_shell_command(existing_tool_call_id, step)
            if existing_result is not None:
                return existing_result

        command = (step.get("command") or "").strip()
        if not command:
            return StepExecutionOutcome(
                status="failed",
                summary=f"Step {step['position']} failed: shell_command requires a command string.",
            )

        result = await self._shell_command_tool.submit_command(
            ShellCommandRequest(
                command=command,
                working_directory=self._resolve_working_directory(task_snapshot),
                reason=step.get("reason") or step.get("description") or step["title"],
                task_id=UUID(task_snapshot["id"]),
            )
        )
        return self._shell_result_to_outcome(step, result, command)

    async def _continue_existing_shell_command(
        self,
        tool_call_id: UUID,
        step: PlanStepState,
    ) -> StepExecutionOutcome | None:
        async with self._session_factory() as session:
            tool_call = await session.get(ToolCall, tool_call_id)
            if tool_call is None:
                return None

            approval = await session.scalar(
                select(Approval)
                .where(Approval.tool_call_id == tool_call.id)
                .order_by(Approval.created_at.desc())
                .limit(1)
            )

        command = str(step.get("command") or tool_call.input_payload.get("command") or "")
        if tool_call.status == "completed":
            return StepExecutionOutcome(
                status="completed",
                summary=f"Step {step['position']} completed with shell_command. Output: {self._excerpt(tool_call.output_text or '')}",
                tool_call_id=tool_call.id,
            )
        if tool_call.status in {"failed", "timed_out", "blocked"}:
            detail = tool_call.stderr_text or tool_call.output_text or "Shell command failed."
            return StepExecutionOutcome(
                status="failed",
                summary=f"Step {step['position']} failed while running shell_command: {self._excerpt(detail)}",
                tool_call_id=tool_call.id,
            )
        if tool_call.status == "pending_approval":
            approved = tool_call.approved_by_user or (approval is not None and approval.status == "approved")
            if approved:
                result = await self._shell_command_tool.execute_approved_tool_call(tool_call.id)
                return self._shell_result_to_outcome(step, result, command)

            summary = (
                f"Step {step['position']} is waiting for approval to run `{command}`. "
                "Approve the tool call and resume the task to continue."
            )
            return StepExecutionOutcome(
                status="pending_approval",
                summary=summary,
                tool_call_id=tool_call.id,
                approval_id=approval.id if approval is not None else None,
            )
        return None

    def _shell_result_to_outcome(
        self,
        step: PlanStepState,
        result: ShellCommandResult,
        command: str,
    ) -> StepExecutionOutcome:
        if result.requires_approval or result.status == "pending_approval":
            summary = (
                f"Step {step['position']} requires approval before running `{command}`. "
                f"Approval ID: {result.approval_id}."
            )
            return StepExecutionOutcome(
                status="pending_approval",
                summary=summary,
                tool_call_id=result.tool_call_id,
                approval_id=result.approval_id,
            )
        if self._is_read_only_inspection_command(command):
            inspection_summary = self._summarize_read_only_command_result(command, result)
            return StepExecutionOutcome(
                status="completed",
                summary=inspection_summary,
                tool_call_id=result.tool_call_id,
            )
        if result.status == "completed":
            summary = (
                f"Step {step['position']} completed with shell_command. "
                f"Output: {self._excerpt(result.stdout or result.stderr)}"
            )
            return StepExecutionOutcome(
                status="completed",
                summary=summary,
                tool_call_id=result.tool_call_id,
            )

        detail = result.stderr or result.stdout or "No output captured."
        return StepExecutionOutcome(
            status="failed",
            summary=f"Step {step['position']} failed while running `{command}`: {self._excerpt(detail)}",
            tool_call_id=result.tool_call_id,
        )

    def _is_read_only_inspection_command(self, command: str) -> bool:
        normalized = " ".join(command.strip().lower().split())
        return normalized.startswith(
            (
                "find ",
                "grep ",
                "rg ",
                "ripgrep ",
                "ls ",
                "dir ",
                "cat ",
                "head ",
                "tail ",
                "ps ",
                "pgrep ",
                "which ",
                "where ",
            )
        ) or "| grep" in normalized

    def _summarize_read_only_command_result(self, command: str, result: ShellCommandResult) -> str:
        lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        if lines:
            preview = lines[:6]
            bullet_list = "\n".join(f"- {line}" for line in preview)
            extra = ""
            if len(lines) > len(preview):
                extra = f"\n- ...and {len(lines) - len(preview)} more"
            if command.strip().lower().startswith("ps ") or "| grep" in command.lower():
                return f"I checked the running processes and found:\n{bullet_list}{extra}"
            return f"I found these matches:\n{bullet_list}{extra}"

        stderr_text = (result.stderr or "").strip().lower()
        if result.status == "failed" and stderr_text and "permission denied" not in stderr_text:
            return f"I couldn't complete that inspection cleanly: {self._excerpt(result.stderr or '')}"
        return "I checked, but I couldn't find any matching results."

    async def _apply_step_outcome(
        self,
        task_snapshot: TaskSnapshot,
        step: PlanStepState,
        outcome: StepExecutionOutcome,
        *,
        worker_name: str | None,
    ) -> PlanStepState:
        task_id = UUID(task_snapshot["id"])
        task_step_id = UUID(step["task_step_id"])
        now = datetime.now(timezone.utc)

        async with self._session_factory() as session:
            task_step = await session.get(TaskStep, task_step_id)
            if task_step is None:
                raise ValueError(f"Task step {task_step_id} does not exist.")

            subtask = await self._load_subtask(session, step)
            metadata_json = dict(task_step.metadata_json or {})
            metadata_json.update(
                {
                    "summary": outcome.summary,
                    "tool_name": step.get("tool_name"),
                    "command": step.get("command"),
                    "reason": step.get("reason"),
                    "tool_call_id": str(outcome.tool_call_id) if outcome.tool_call_id is not None else metadata_json.get("tool_call_id"),
                    "approval_id": str(outcome.approval_id) if outcome.approval_id is not None else metadata_json.get("approval_id"),
                }
            )
            task_step.metadata_json = metadata_json
            if task_step.started_at is None:
                task_step.started_at = now

            if outcome.status == "completed":
                task_step.status = "completed"
                task_step.completed_at = now
                if subtask is not None:
                    self._set_subtask_status(subtask, status="completed", worker_name=None, now=now)
            elif outcome.status == "pending_approval":
                task_step.status = "pending_approval"
                task_step.completed_at = None
                if subtask is not None:
                    self._set_subtask_status(subtask, status="paused", worker_name=None, now=now)
            else:
                task_step.status = "failed"
                task_step.completed_at = now
                if subtask is not None:
                    self._set_subtask_status(subtask, status="failed", worker_name=worker_name, now=now)

            summary_memory = await save_memory(
                session,
                MemoryCreateRequest(
                    workspace_id=UUID(task_snapshot["workspace_id"]),
                    user_id=self._optional_uuid(task_snapshot.get("created_by_user_id")),
                    task_id=task_id,
                    memory_type="task_summary",
                    scope="task",
                    source="agent",
                    confidence=0.85 if outcome.status == "completed" else 0.7,
                    content=outcome.summary,
                    tags=("agent", f"step-{step['position']}", outcome.status),
                    metadata_json={
                        "task_step_id": str(task_step.id),
                        "tool_call_id": str(outcome.tool_call_id) if outcome.tool_call_id is not None else None,
                    },
                ),
            )

            task_step.metadata_json = {
                **dict(task_step.metadata_json or {}),
                "summary_memory_id": str(summary_memory.id),
            }
            await session.commit()
            await session.refresh(task_step)

            return self._serialize_task_step(task_step)

    def _set_subtask_status(self, task: Task, *, status: str, worker_name: str | None, now: datetime) -> None:
        task.status = status
        if task.started_at is None:
            task.started_at = now

        if status == "completed":
            task.completed_at = now
            task.assigned_worker = None
        elif status == "paused":
            task.completed_at = None
            task.assigned_worker = None
        elif status == "failed":
            task.completed_at = now
            task.assigned_worker = None
        else:
            task.completed_at = None
            task.assigned_worker = worker_name

    async def _load_subtask(self, session: AsyncSession, step: PlanStepState) -> Task | None:
        subtask_id = self._optional_uuid(step.get("subtask_id"))
        if subtask_id is None:
            return None
        return await session.get(Task, subtask_id)

    def _resolve_working_directory(self, task_snapshot: TaskSnapshot) -> str:
        metadata_json = task_snapshot.get("metadata_json", {})
        if isinstance(metadata_json, dict):
            working_directory = metadata_json.get("working_directory")
            if isinstance(working_directory, str) and working_directory.strip():
                return working_directory
        return str(self._default_working_directory)

    def _serialize_task_step(self, task_step: TaskStep) -> PlanStepState:
        metadata_json = dict(task_step.metadata_json or {})
        return {
            "position": task_step.position,
            "title": task_step.title,
            "description": task_step.description or "",
            "tool_name": metadata_json.get("tool_name"),
            "command": metadata_json.get("command"),
            "reason": metadata_json.get("reason"),
            "task_step_id": str(task_step.id),
            "subtask_id": metadata_json.get("subtask_id"),
            "status": task_step.status,
            "tool_call_id": metadata_json.get("tool_call_id"),
            "approval_id": metadata_json.get("approval_id"),
        }

    def _serialize_memory(self, memory: ContextMemory) -> dict[str, Any]:
        return {
            "memory_id": str(memory.memory_id),
            "memory_type": memory.memory_type,
            "scope": memory.scope,
            "content": memory.content,
            "source": memory.source,
            "confidence": memory.confidence,
            "tags": list(memory.tags),
            "relevance": memory.relevance,
        }

    def _serialize_skill(self, skill: SkillContext) -> dict[str, Any]:
        return {
            "name": skill.name,
            "version": skill.version,
            "source": skill.source,
            "description": skill.description,
            "excerpt": skill.excerpt,
            "score": skill.score,
            "triggers": list(skill.triggers),
            "tools_allowed": list(skill.tools_allowed),
        }

    def _excerpt(self, text: str, limit: int = 180) -> str:
        cleaned = " ".join(text.split())
        if len(cleaned) <= limit:
            return cleaned
        return f"{cleaned[:limit - 3].rstrip()}..."

    def _optional_uuid(self, raw_value: str | None) -> UUID | None:
        if not raw_value:
            return None
        return UUID(str(raw_value))
