"""
Agent node handlers — Hermes-style iterative conversation loop.

The model gets tool definitions and calls them autonomously. Tool results
are fed back into the conversation. The loop continues until the model
calls task_complete, task_failed, or guardrails halt execution.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.model_client import (
    Conversation,
    HermesModelClient,
    LiteLLMPlanningModel,
    ModelTurn,
    PlannedStep,
    PlanningModel,
    ToolCallGuardrails,
    ToolCallRequest,
)
from agent.progress import ProgressReporter
from agent.skills import SkillContext, load_skill_context
from agent.state import AgentState, PlanStepState, TaskSnapshot
from app.config import Settings
from db.memory_service import ContextMemory, MemoryCreateRequest, build_task_context, save_memory
from db.models import Approval, Task, TaskRun, TaskStep, ToolCall
from db.repositories import log_tool_call
from tools.registry import build_runtime_tool_registry, get_tool_registry
from tools.safe_tools import SafeTool
from tools.shell_command import ShellCommandRequest, ShellCommandResult, ShellCommandTool
from tools.web_research import WebResearchProvider, WebResearchTool

logger = logging.getLogger(__name__)

MAX_HERMES_ITERATIONS = 30


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
        self._settings = settings
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
        self._shell_tool = cast(ShellCommandTool, self._runtime_tool_registry["shell_command"])
        self._web_tool = cast(WebResearchTool | None, self._runtime_tool_registry.get("web_search"))
        roots = list(allowed_tool_roots or [Path.cwd()])
        self._default_working_directory = Path(roots[0]).resolve()

    # ── Graph nodes ───────────────────────────────────────────────────

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
            "created_by_user_id": str(task.created_by_user_id) if task.created_by_user_id else None,
            "priority": task.priority,
            "metadata_json": dict(task.metadata_json or {}),
        }
        next_state.update(
            task=task_snapshot,
            task_run_id=str(task_run.id) if task_run else None,
            worker_name=task_run.worker_name if task_run else None,
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
            if task_run_id:
                task_run = await session.get(TaskRun, task_run_id)
                if task_run:
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
            f"Context loaded: {len(memory_context)} memories, {len(skill_context)} skills.",
            stage="build_context",
        )
        return cast(AgentState, next_state)

    async def plan(self, state: AgentState) -> AgentState:
        """
        Plan node: supports both Hermes mode (single meta-step) and
        legacy mode (multi-step plan for backward compatibility).
        """
        next_state = dict(state)
        task_snapshot = cast(TaskSnapshot, next_state["task"])
        task_id = UUID(task_snapshot["id"])

        planned_steps = await self._planning_model.create_plan(
            task=task_snapshot,
            memory_context=list(next_state.get("memory_context", [])),
            skill_context=list(next_state.get("skill_context", [])),
            task_run_id=self._optional_uuid(next_state.get("task_run_id")),
        )

        is_hermes = (
            len(planned_steps) == 1
            and planned_steps[0].tool_name == "__hermes_loop__"
        )

        if is_hermes:
            # Persist a single tracking step for Hermes mode
            async with self._session_factory() as session:
                task = await session.get(Task, task_id)
                if task:
                    step = TaskStep(
                        task_id=task.id,
                        position=1,
                        title="Hermes conversation loop",
                        description="Autonomous iterative execution with tool calling",
                        status="pending",
                        metadata_json={"mode": "hermes", "tool_name": "__hermes_loop__"},
                    )
                    session.add(step)
                    await session.commit()
                    await session.refresh(step)
                    next_state["plan_steps"] = [self._serialize_task_step(step)]
            await self._progress_reporter.report(task_id, "Starting autonomous execution...", stage="plan")
        else:
            # Legacy: persist multi-step plan
            plan_steps = await self._persist_plan(task_snapshot, planned_steps)
            next_state["plan_steps"] = plan_steps
            await self._progress_reporter.report(
                task_id,
                f"Plan created with {len(plan_steps)} steps.",
                stage="plan",
            )

        return cast(AgentState, next_state)

    async def execute_step(self, state: AgentState) -> AgentState:
        """
        Hermes-style execution: runs the conversation loop.
        Falls back to legacy step-by-step execution for tests/compat.
        """
        next_state = dict(state)
        task_snapshot = cast(TaskSnapshot, next_state["task"])
        task_id = UUID(task_snapshot["id"])
        step_summaries: list[str] = []

        # Get the Hermes client from the planning model
        hermes_client = self._get_hermes_client()
        if hermes_client is None:
            # Legacy fallback: execute plan steps sequentially (for tests/compat)
            return await self._execute_legacy_steps(state)


        # Build conversation and tools
        conversation = hermes_client.build_conversation(
            task=task_snapshot,
            memory_context=list(next_state.get("memory_context", [])),
            skill_context=list(next_state.get("skill_context", [])),
        )
        tools = hermes_client.get_tool_definitions()
        guardrails = ToolCallGuardrails(max_iterations=MAX_HERMES_ITERATIONS)

        final_status = "completed"
        final_summary = "Task completed."
        pending_approval_id: str | None = None

        # ── Main conversation loop ────────────────────────────────
        for iteration in range(MAX_HERMES_ITERATIONS):
            if guardrails.should_halt:
                final_status = "failed"
                final_summary = f"Execution halted: {guardrails.halt_reason}"
                break

            # Get next model turn
            turn = await hermes_client.get_next_turn(
                conversation,
                tools=tools,
                task_id=task_id,
                task_run_id=self._optional_uuid(next_state.get("task_run_id")),
            )

            # If model returned text without tool calls, we're done
            if turn.is_done and not turn.has_tool_calls:
                final_summary = turn.content or "Task completed."
                step_summaries.append(final_summary)
                break

            # If no tool calls and no content, something went wrong
            if not turn.has_tool_calls:
                if turn.content:
                    final_summary = turn.content
                    step_summaries.append(turn.content)
                break

            # Execute each tool call
            conversation.add_assistant_tool_calls(turn.tool_calls)

            for tc in turn.tool_calls:
                # Report progress
                await self._progress_reporter.report(
                    task_id,
                    f"Calling {tc.name}: {self._summarize_tool_args(tc)}",
                    stage="tool_call",
                )

                # Execute the tool
                result_text, outcome_status = await self._execute_tool_call(
                    task_snapshot, tc, step_summaries
                )

                # Handle approval needed — feed it back to the model
                # so it can adapt (e.g., retry without sudo)
                if outcome_status == "pending_approval":
                    hint = (
                        f"{result_text}\n\n"
                        "IMPORTANT: This command requires human approval and cannot run automatically. "
                        "You are running as ROOT — do NOT use sudo. "
                        "Retry the same command without 'sudo' prefix. "
                        "Example: use 'systemctl restart nginx' not 'sudo systemctl restart nginx'."
                    )
                    conversation.add_tool_result(tc.id, tc.name, hint)
                    await self._progress_reporter.report(
                        task_id,
                        f"Tool {tc.name}: command needs approval, retrying without sudo",
                        stage="tool_result",
                    )
                    guardrails.record_success()
                    continue

                # Add result to conversation
                conversation.add_tool_result(tc.id, tc.name, result_text)

                # Report tool result
                await self._progress_reporter.report(
                    task_id,
                    f"Tool {tc.name}: {result_text[:200]}",
                    stage="tool_result",
                )

                # Handle terminal tool calls
                if tc.name == "task_complete":
                    final_status = "completed"
                    final_summary = tc.arguments.get("summary", result_text)
                    step_summaries.append(final_summary)
                    guardrails.record_success()
                    break

                if tc.name == "task_failed":
                    final_status = "failed"
                    final_summary = tc.arguments.get("reason", result_text)
                    step_summaries.append(final_summary)
                    break

                # Track guardrails
                if outcome_status == "failed":
                    call_sig = f"{tc.name}:{json.dumps(tc.arguments, sort_keys=True)[:100]}"
                    guardrails.record_failure(call_sig)
                else:
                    guardrails.record_success()
            else:
                # All tool calls processed, continue loop for next model turn
                continue
            # One of the tool calls triggered a break
            break

        # Update the task step record
        await self._finalize_step(task_id, final_status)

        next_state.update(
            step_summaries=step_summaries,
            final_status=final_status,
            final_summary=final_summary,
            pending_approval_id=pending_approval_id,
        )
        return cast(AgentState, next_state)

    async def _execute_legacy_steps(self, state: AgentState) -> AgentState:
        """Legacy plan-based execution for backward compatibility with tests."""
        next_state = dict(state)
        task_snapshot = cast(TaskSnapshot, next_state["task"])
        task_id = UUID(task_snapshot["id"])
        step_summaries: list[str] = []
        plan_steps = list(next_state.get("plan_steps", []))

        final_status = "completed"
        final_summary = "Task completed."

        for index, step in enumerate(plan_steps):
            if step.get("status") == "completed":
                continue

            tool_name = step.get("tool_name")
            if not tool_name or tool_name in ("null", "none", "None", "__hermes_loop__"):
                step_summaries.append(f"Step {step.get('position', index+1)}: {step.get('description', 'done')}")
                async with self._session_factory() as session:
                    task_step = await session.get(TaskStep, UUID(step["task_step_id"]))
                    if task_step:
                        task_step.status = "completed"
                        task_step.completed_at = datetime.now(timezone.utc)
                    subtask_id = step.get("subtask_id")
                    if subtask_id:
                        subtask = await session.get(Task, UUID(subtask_id))
                        if subtask:
                            subtask.status = "completed"
                            subtask.completed_at = datetime.now(timezone.utc)
                    await session.commit()
                continue

            # Execute safe tools
            safe_tool = self._safe_tool_registry.get(tool_name)
            if safe_tool:
                try:
                    output = safe_tool.handler()
                    output_text = json.dumps(output, indent=2)
                    step_summaries.append(f"Step {step.get('position', index+1)}: {output_text[:200]}")
                    async with self._session_factory() as session:
                        await log_tool_call(
                            session,
                            tool_name=safe_tool.name,
                            input_payload={"reason": step.get("reason", "")},
                            output_text=output_text,
                            duration_ms=1,
                            status="completed",
                            risk_level="safe",
                            approved_by_user=True,
                            exit_code=0,
                            task_id=task_id,
                        )
                        task_step = await session.get(TaskStep, UUID(step["task_step_id"]))
                        if task_step:
                            task_step.status = "completed"
                            task_step.completed_at = datetime.now(timezone.utc)
                        # Also mark the subtask as completed
                        subtask_id = step.get("subtask_id")
                        if subtask_id:
                            subtask = await session.get(Task, UUID(subtask_id))
                            if subtask:
                                subtask.status = "completed"
                                subtask.completed_at = datetime.now(timezone.utc)
                        await session.commit()
                except Exception as exc:
                    step_summaries.append(f"Step {step.get('position', index+1)} failed: {exc}")
                    final_status = "failed"
                    final_summary = str(exc)
                    break
                continue

            # Execute shell commands
            if tool_name == "shell_command":
                command = (step.get("command") or "").strip()
                if command:
                    result = await self._shell_tool.submit_command(
                        ShellCommandRequest(
                            command=command,
                            working_directory=self._resolve_working_directory(task_snapshot),
                            reason=step.get("reason") or step.get("description") or "",
                            task_id=task_id,
                        )
                    )
                    if result.status == "completed":
                        step_summaries.append(f"Step {step.get('position', index+1)}: {result.stdout[:200]}")
                    elif result.requires_approval or result.status == "pending_approval":
                        final_status = "paused"
                        final_summary = f"Waiting for approval on: {command}"
                        next_state["pending_approval_id"] = str(result.approval_id) if result.approval_id else None
                        break
                    else:
                        step_summaries.append(f"Step {step.get('position', index+1)} failed: {result.stderr[:200]}")
                        final_status = "failed"
                        final_summary = result.stderr[:500]
                        break
                    async with self._session_factory() as session:
                        task_step = await session.get(TaskStep, UUID(step["task_step_id"]))
                        if task_step:
                            task_step.status = "completed"
                            task_step.completed_at = datetime.now(timezone.utc)
                        subtask_id = step.get("subtask_id")
                        if subtask_id:
                            sub = await session.get(Task, UUID(subtask_id))
                            if sub:
                                sub.status = "completed"
                                sub.completed_at = datetime.now(timezone.utc)
                        await session.commit()
                continue

            # Execute web search
            if tool_name == "web_search" and self._web_tool is not None:
                query = (step.get("command") or step.get("description") or "").strip()
                if query:
                    try:
                        ws_result = await self._web_tool.search(
                            task_id=task_id, task_run_id=None, query=query
                        )
                        step_summaries.append(
                            f"Step {step.get('position', index+1)}: {ws_result.summary[:200]}"
                        )
                        async with self._session_factory() as session:
                            task_step = await session.get(TaskStep, UUID(step["task_step_id"]))
                            if task_step:
                                task_step.status = "completed"
                                task_step.completed_at = datetime.now(timezone.utc)
                            subtask_id = step.get("subtask_id")
                            if subtask_id:
                                sub = await session.get(Task, UUID(subtask_id))
                                if sub:
                                    sub.status = "completed"
                                    sub.completed_at = datetime.now(timezone.utc)
                            await session.commit()
                    except Exception as exc:
                        step_summaries.append(f"Step {step.get('position', index+1)} failed: {exc}")
                        final_status = "failed"
                        final_summary = str(exc)
                        break
                continue

            # Execute web open
            if tool_name == "web_open" and self._web_tool is not None:
                url = (step.get("command") or "").strip()
                if not url or not (url.startswith("http://") or url.startswith("https://")):
                    # Fallback: find URL from recent web_search tool call
                    async with self._session_factory() as session:
                        import re as _re
                        tc_result = await session.execute(
                            select(ToolCall)
                            .where(ToolCall.task_id == task_id, ToolCall.tool_name == "web_search")
                            .order_by(ToolCall.created_at.desc())
                            .limit(1)
                        )
                        recent_tc = tc_result.scalar_one_or_none()
                        if recent_tc and recent_tc.output_text:
                            url_match = _re.search(r"https?://\S+", recent_tc.output_text)
                            if url_match:
                                url = url_match.group(0).rstrip(")].,;")
                if url:
                    try:
                        wo_result = await self._web_tool.open(
                            task_id=task_id, task_run_id=None, url=url
                        )
                        step_summaries.append(
                            f"Step {step.get('position', index+1)}: {wo_result.content[:200]}"
                        )
                        async with self._session_factory() as session:
                            task_step = await session.get(TaskStep, UUID(step["task_step_id"]))
                            if task_step:
                                task_step.status = "completed"
                                task_step.completed_at = datetime.now(timezone.utc)
                            subtask_id = step.get("subtask_id")
                            if subtask_id:
                                sub = await session.get(Task, UUID(subtask_id))
                                if sub:
                                    sub.status = "completed"
                                    sub.completed_at = datetime.now(timezone.utc)
                            await session.commit()
                    except Exception as exc:
                        step_summaries.append(f"Step {step.get('position', index+1)} failed: {exc}")
                        final_status = "failed"
                        final_summary = str(exc)
                        break
                continue

            step_summaries.append(f"Step {step.get('position', index+1)}: unsupported tool {tool_name}")

        if final_status == "completed" and step_summaries:
            final_summary = step_summaries[-1]

        next_state.update(
            step_summaries=step_summaries,
            final_status=final_status,
            final_summary=final_summary,
        )
        return cast(AgentState, next_state)

    async def save_memory(self, state: AgentState) -> AgentState:
        next_state = dict(state)
        task_snapshot = cast(TaskSnapshot, next_state["task"])
        task_id = UUID(task_snapshot["id"])
        final_status = str(next_state.get("final_status", "failed"))
        final_summary = str(next_state.get("final_summary", "No summary."))

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
                message += f"\nApproval ID: {approval_hint}"
        else:
            message = f"Task failed: {task_snapshot['title']}\n{final_summary}"

        await self._progress_reporter.report(task_id, message, stage="report_result")
        return cast(AgentState, next_state)

    # ── Legacy plan persistence ─────────────────────────────────────

    async def _persist_plan(
        self, task_snapshot: TaskSnapshot, planned_steps: list[PlannedStep]
    ) -> list[PlanStepState]:
        task_id = UUID(task_snapshot["id"])
        async with self._session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise ValueError(f"Task {task_id} does not exist.")

            persisted: list[PlanStepState] = []
            for position, ps in enumerate(planned_steps, start=1):
                subtask = Task(
                    workspace_id=task.workspace_id,
                    created_by_user_id=task.created_by_user_id,
                    parent_task_id=task.id,
                    title=ps.title,
                    description=ps.description,
                    priority=task.priority,
                    status="paused",
                    metadata_json={
                        "source": "agent_plan",
                        "step_position": position,
                        "tool_name": ps.tool_name,
                        "command": ps.command,
                    },
                )
                session.add(subtask)
                await session.flush()

                step = TaskStep(
                    task_id=task.id,
                    position=position,
                    title=ps.title,
                    description=ps.description or "",
                    status="pending",
                    metadata_json={
                        "tool_name": ps.tool_name,
                        "command": ps.command,
                        "reason": ps.reason,
                        "subtask_id": str(subtask.id),
                    },
                )
                session.add(step)
                await session.flush()
                persisted.append(self._serialize_task_step(step))

            await session.commit()
            return persisted

    # ── Tool execution ────────────────────────────────────────────────

    async def _execute_tool_call(
        self,
        task_snapshot: TaskSnapshot,
        tc: ToolCallRequest,
        step_summaries: list[str],
    ) -> tuple[str, str]:
        """
        Execute a single tool call. Returns (result_text, status).
        Status is one of: completed, failed, pending_approval.
        """
        task_id = UUID(task_snapshot["id"])

        try:
            if tc.name == "shell_command":
                return await self._exec_shell(task_snapshot, tc)

            if tc.name == "web_search":
                return await self._exec_web_search(task_snapshot, tc)

            if tc.name == "web_open":
                return await self._exec_web_open(task_snapshot, tc)

            if tc.name == "save_memory":
                return await self._exec_save_memory(task_snapshot, tc)

            if tc.name == "task_complete":
                summary = tc.arguments.get("summary", "Task completed.")
                return summary, "completed"

            if tc.name == "task_failed":
                reason = tc.arguments.get("reason", "Task failed.")
                return reason, "failed"

            return f"Unknown tool: {tc.name}", "failed"

        except Exception as exc:
            logger.error("Tool call %s failed: %s", tc.name, exc, exc_info=True)
            return f"Error executing {tc.name}: {exc}", "failed"

    async def _exec_shell(
        self, task_snapshot: TaskSnapshot, tc: ToolCallRequest
    ) -> tuple[str, str]:
        command = tc.arguments.get("command", "").strip()
        if not command:
            return "Error: no command provided.", "failed"

        working_dir = tc.arguments.get(
            "working_directory",
            self._resolve_working_directory(task_snapshot),
        )
        reason = tc.arguments.get("reason", "Agent tool call")

        result = await self._shell_tool.submit_command(
            ShellCommandRequest(
                command=command,
                working_directory=working_dir,
                reason=reason,
                task_id=UUID(task_snapshot["id"]),
            )
        )

        if result.requires_approval or result.status == "pending_approval":
            approval_msg = (
                f"Command requires approval: `{command}`\n"
                f"Risk: {result.risk_level}\n"
            )
            if result.approval_id:
                approval_msg += f"Approval ID: {result.approval_id}"
            return approval_msg, "pending_approval"

        if result.status in ("completed",):
            output = result.stdout.strip() or result.stderr.strip() or "(no output)"
            if result.exit_code == 0:
                return f"Exit code 0.\n{output[:4000]}", "completed"
            else:
                return f"Exit code {result.exit_code}.\n{output[:4000]}", "failed"

        if result.status in ("failed", "timed_out", "blocked"):
            error = result.stderr.strip() or result.stdout.strip() or result.status
            return f"Command failed ({result.status}): {error[:4000]}", "failed"

        return f"Unexpected status: {result.status}", "failed"

    async def _exec_web_search(
        self, task_snapshot: TaskSnapshot, tc: ToolCallRequest
    ) -> tuple[str, str]:
        if self._web_tool is None:
            return "Web search is not enabled.", "failed"

        query = tc.arguments.get("query", "").strip()
        if not query:
            return "Error: no query provided.", "failed"

        try:
            result = await self._web_tool.search(
                task_id=UUID(task_snapshot["id"]),
                task_run_id=None,
                query=query,
            )
            return result.summary, "completed"
        except Exception as exc:
            return f"Web search failed: {exc}", "failed"

    async def _exec_web_open(
        self, task_snapshot: TaskSnapshot, tc: ToolCallRequest
    ) -> tuple[str, str]:
        if self._web_tool is None:
            return "Web access is not enabled.", "failed"

        url = tc.arguments.get("url", "").strip()
        if not url:
            return "Error: no URL provided.", "failed"

        try:
            result = await self._web_tool.open(
                task_id=UUID(task_snapshot["id"]),
                task_run_id=None,
                url=url,
            )
            return result.content[:4000], "completed"
        except Exception as exc:
            return f"Web open failed: {exc}", "failed"

    async def _exec_save_memory(
        self, task_snapshot: TaskSnapshot, tc: ToolCallRequest
    ) -> tuple[str, str]:
        content = tc.arguments.get("content", "").strip()
        memory_type = tc.arguments.get("memory_type", "server_fact")

        if not content:
            return "Error: no content provided.", "failed"

        try:
            async with self._session_factory() as session:
                await save_memory(
                    session,
                    MemoryCreateRequest(
                        workspace_id=UUID(task_snapshot["workspace_id"]),
                        user_id=self._optional_uuid(task_snapshot.get("created_by_user_id")),
                        task_id=UUID(task_snapshot["id"]),
                        memory_type=memory_type,
                        scope="task",
                        source="agent_tool",
                        confidence=0.8,
                        content=content,
                        tags=("agent", memory_type),
                    ),
                )
            return f"Saved to memory: {content[:100]}", "completed"
        except Exception as exc:
            return f"Failed to save memory: {exc}", "failed"

    # ── Helpers ────────────────────────────────────────────────────────

    def _get_hermes_client(self) -> HermesModelClient | None:
        if isinstance(self._planning_model, LiteLLMPlanningModel):
            return self._planning_model.hermes_client
        return None

    def _resolve_working_directory(self, task_snapshot: TaskSnapshot) -> str:
        metadata = task_snapshot.get("metadata_json", {})
        path_hint = metadata.get("working_directory") or metadata.get("path")
        if path_hint:
            p = Path(str(path_hint))
            if p.is_absolute() and p.exists():
                return str(p)
        return str(self._default_working_directory)

    def _summarize_tool_args(self, tc: ToolCallRequest) -> str:
        if tc.name == "shell_command":
            return tc.arguments.get("command", "")[:100]
        if tc.name == "web_search":
            return tc.arguments.get("query", "")[:80]
        if tc.name == "web_open":
            return tc.arguments.get("url", "")[:80]
        if tc.name == "save_memory":
            return tc.arguments.get("content", "")[:60]
        if tc.name in ("task_complete", "task_failed"):
            return tc.arguments.get("summary", tc.arguments.get("reason", ""))[:80]
        return json.dumps(tc.arguments)[:80]

    def _extract_approval_id(self, text: str) -> str | None:
        import re
        match = re.search(r"Approval ID:\s*([0-9a-fA-F-]{36})", text)
        return match.group(1) if match else None

    async def _finalize_step(self, task_id: UUID, status: str) -> None:
        db_status = {
            "completed": "completed",
            "failed": "failed",
            "paused": "pending_approval",
        }.get(status, "failed")

        async with self._session_factory() as session:
            result = await session.execute(
                select(TaskStep)
                .where(TaskStep.task_id == task_id)
                .order_by(TaskStep.position.desc())
                .limit(1)
            )
            step = result.scalar_one_or_none()
            if step:
                step.status = db_status
                step.completed_at = datetime.now(timezone.utc) if db_status in ("completed", "failed") else None
                await session.commit()

    def _optional_uuid(self, value: Any) -> UUID | None:
        if value is None:
            return None
        try:
            return UUID(str(value))
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def _serialize_memory(item: ContextMemory) -> dict[str, Any]:
        return {
            "memory_id": str(item.memory_id),
            "memory_type": item.memory_type,
            "scope": item.scope,
            "content": item.content,
            "source": item.source,
            "confidence": item.confidence,
            "tags": list(item.tags),
            "relevance": item.relevance,
        }

    @staticmethod
    def _serialize_skill(item: SkillContext) -> dict[str, Any]:
        return {
            "name": item.name,
            "version": item.version,
            "source": item.source,
            "description": item.description,
            "excerpt": item.excerpt,
            "score": item.score,
            "triggers": list(item.triggers),
            "tools_allowed": list(item.tools_allowed),
        }

    @staticmethod
    def _serialize_task_step(step: TaskStep) -> PlanStepState:
        metadata = step.metadata_json if isinstance(step.metadata_json, dict) else {}
        return PlanStepState(
            position=step.position,
            title=step.title,
            description=step.description or "",
            tool_name=metadata.get("tool_name"),
            command=metadata.get("command"),
            reason=metadata.get("reason"),
            task_step_id=str(step.id),
            subtask_id=metadata.get("subtask_id"),
            status=step.status,
            tool_call_id=metadata.get("tool_call_id"),
            approval_id=metadata.get("approval_id"),
        )

    @staticmethod
    def _excerpt(text: str, max_length: int = 300) -> str:
        text = text.strip()
        if len(text) <= max_length:
            return text
        return text[:max_length].rstrip() + "..."
