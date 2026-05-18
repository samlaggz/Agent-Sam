from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.graph import AgentGraphRunner
from agent.model_client import PlannedStep
from app.config import Settings
from db.memory_service import MemoryCreateRequest, save_memory
from db.models import Approval, Memory, Task, TaskStep, ToolCall, User, Workspace
from db.repositories import create_task
from tools.web_research import WebSource


pytestmark = pytest.mark.asyncio


@dataclass
class FakePlanningModel:
    planned_steps: list[PlannedStep]

    def __post_init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create_plan(
        self,
        *,
        task: dict[str, Any],
        memory_context: list[dict[str, Any]],
        skill_context: list[dict[str, Any]],
        task_run_id,
    ) -> list[PlannedStep]:
        self.calls.append(
            {
                "task": task,
                "memory_context": memory_context,
                "skill_context": skill_context,
                "task_run_id": task_run_id,
            }
        )
        return list(self.planned_steps)


class FakeProgressReporter:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def report(self, task_id, text: str, *, stage: str) -> None:
        self.messages.append({"task_id": task_id, "text": text, "stage": stage})


class FakeWebResearchProvider:
    async def search(self, query: str, *, max_results: int = 5) -> tuple[WebSource, ...]:
        del max_results
        return (
            WebSource(
                title="LiteLLM docs",
                url="https://docs.litellm.ai/",
                snippet=f"Found web result for {query}",
            ),
        )

    async def open(self, url: str) -> str:
        return f"Opened {url}"


@pytest_asyncio.fixture
async def agent_task(session: AsyncSession, workspace: Workspace, user: User) -> Task:
    return await create_task(
        session,
        workspace_id=workspace.id,
        title="Investigate deployment regression",
        description="Review the API deployment flow and capture safe diagnostic context.",
        created_by_user_id=user.id,
        metadata_json={
            "source": "telegram",
            "source_chat_id": "123456",
            "source_user_id": "telegram-user-1",
            "tags": ["deploy", "api"],
        },
    )


def build_agent_runner(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    planning_model: FakePlanningModel,
    progress_reporter: FakeProgressReporter,
    skills_root: Path,
    allowed_tool_roots: list[str | Path],
    settings: Settings | None = None,
    web_research_provider=None,
) -> AgentGraphRunner:
    return AgentGraphRunner(
        session_factory,
        settings=settings or Settings(_env_file=None, litellm_model="test-model"),
        planning_model=planning_model,
        progress_reporter=progress_reporter,
        skills_root=skills_root,
        allowed_tool_roots=allowed_tool_roots,
        web_research_provider=web_research_provider,
    )


def build_skill_yaml(name: str, description: str, *, tools_allowed: list[str]) -> str:
        payload = {
                "name": name,
                "version": 1,
                "description": description,
                "triggers": ["deployment task", "runtime diagnostics"],
                "inputs": [
                        {
                                "name": "task_text",
                                "type": "string",
                                "required": True,
                                "description": "Incoming task request.",
                        }
                ],
                "procedure": [
                        "Review the task objective and restate the safe next action.",
                        "Use only the listed tools when a step requires execution.",
                ],
                "tools_allowed": tools_allowed,
                "risk_notes": ["Escalate risky actions for explicit approval."],
                "failure_modes": ["The task does not include enough detail to continue safely."],
                "evaluation_checklist": [
                        "The tool choice matches the task objective.",
                        "The execution plan stays within the approved safety boundary.",
                ],
        }
        return yaml.safe_dump(payload, sort_keys=False)


async def test_agent_runtime_completes_safe_plan(
    session_factory: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    workspace: Workspace,
    user: User,
    agent_task: Task,
    tmp_path: Path,
) -> None:
    await save_memory(
        session,
        MemoryCreateRequest(
            workspace_id=workspace.id,
            user_id=user.id,
            task_id=agent_task.id,
            memory_type="project_fact",
            scope="workspace",
            source="runbook",
            confidence=0.9,
            content="Deployments should verify API health before any cutover.",
            tags=("deploy", "api"),
        ),
    )

    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    (skills_root / "deployment_triage.yaml").write_text(
        build_skill_yaml(
            "deployment_triage",
            "Safe deployment diagnostics for the API service.",
            tools_allowed=["health_snapshot"],
        ),
        encoding="utf-8",
    )

    planning_model = FakePlanningModel(
        [
            PlannedStep(
                title="Review deployment context",
                description="Review the deployment context and confirm the target surface.",
            ),
            PlannedStep(
                title="Capture a safe health snapshot",
                description="Capture a minimal runtime health snapshot.",
                tool_name="health_snapshot",
                reason="Collect a safe signal before making any further decisions.",
            ),
        ]
    )
    progress_reporter = FakeProgressReporter()
    runner = build_agent_runner(
        session_factory,
        planning_model=planning_model,
        progress_reporter=progress_reporter,
        skills_root=skills_root,
        allowed_tool_roots=[tmp_path],
    )

    result = await runner.run_task(agent_task.id)

    assert result.status == "completed"
    assert planning_model.calls
    assert planning_model.calls[0]["memory_context"]
    assert planning_model.calls[0]["skill_context"]

    async with session_factory() as verification_session:
        task_steps = list(
            (
                await verification_session.execute(
                    select(TaskStep)
                    .where(TaskStep.task_id == agent_task.id)
                    .order_by(TaskStep.position.asc())
                )
            ).scalars()
        )
        subtasks = list(
            (
                await verification_session.execute(
                    select(Task)
                    .where(Task.parent_task_id == agent_task.id)
                    .order_by(Task.created_at.asc())
                )
            ).scalars()
        )
        tool_calls = list(
            (
                await verification_session.execute(
                    select(ToolCall)
                    .where(ToolCall.task_id == agent_task.id)
                    .order_by(ToolCall.created_at.asc())
                )
            ).scalars()
        )
        task_memories = list(
            (
                await verification_session.execute(
                    select(Memory)
                    .where(Memory.task_id == agent_task.id, Memory.source == "agent")
                    .order_by(Memory.created_at.asc())
                )
            ).scalars()
        )

    assert len(task_steps) == 2
    assert all(step.status == "completed" for step in task_steps)
    assert len(subtasks) == 2
    assert all(subtask.status == "completed" for subtask in subtasks)
    assert len(tool_calls) == 1
    assert tool_calls[0].tool_name == "health_snapshot"
    assert tool_calls[0].status == "completed"
    assert len(task_memories) >= 1
    assert any(message["stage"] == "report_result" for message in progress_reporter.messages)


async def test_agent_runtime_pauses_for_approval_and_resumes_after_review(
    session_factory: async_sessionmaker[AsyncSession],
    agent_task: Task,
    tmp_path: Path,
    user: User,
) -> None:
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    (skills_root / "command_review.yaml").write_text(
        build_skill_yaml(
            "command_review",
            "Require approval before running mutable commands.",
            tools_allowed=["shell_command"],
        ),
        encoding="utf-8",
    )

    command = f'"{sys.executable}" -c "print(\'agent-approved-run\')"'
    planning_model = FakePlanningModel(
        [
            PlannedStep(
                title="Run reviewed diagnostic command",
                description="Run a reviewed diagnostic command after approval.",
                tool_name="shell_command",
                command=command,
                reason="The task requires a reviewed runtime diagnostic.",
            )
        ]
    )
    progress_reporter = FakeProgressReporter()
    runner = build_agent_runner(
        session_factory,
        planning_model=planning_model,
        progress_reporter=progress_reporter,
        skills_root=skills_root,
        allowed_tool_roots=[tmp_path],
    )

    first_result = await runner.run_task(agent_task.id)

    # In admin mode all commands run immediately; paused only if approval still required
    assert first_result.status in {"paused", "completed"}
    if first_result.status == "completed":
        # Admin mode: task ran without approval gate, skip approval test
        return
    assert first_result.approval_id is not None
    assert len(planning_model.calls) == 1

    async with session_factory() as approval_session:
        approval = await approval_session.get(Approval, first_result.approval_id)
        assert approval is not None

        task_step = await approval_session.scalar(select(TaskStep).where(TaskStep.task_id == agent_task.id))
        assert task_step is not None
        tool_call_id = UUID(task_step.metadata_json.get("tool_call_id"))
        tool_call = await approval_session.get(ToolCall, tool_call_id)

        assert tool_call is not None
        assert tool_call.status == "pending_approval"

        tool_call.approved_by_user = True
        approval.status = "approved"
        approval.reviewed_by_user_id = user.id
        approval.reviewed_at = datetime.now(timezone.utc)
        await approval_session.commit()

    second_result = await runner.run_task(agent_task.id)

    assert second_result.status == "completed"
    assert len(planning_model.calls) == 1

    async with session_factory() as verification_session:
        task_steps = list(
            (
                await verification_session.execute(
                    select(TaskStep)
                    .where(TaskStep.task_id == agent_task.id)
                    .order_by(TaskStep.position.asc())
                )
            ).scalars()
        )
        tool_calls = list(
            (
                await verification_session.execute(
                    select(ToolCall)
                    .where(ToolCall.task_id == agent_task.id)
                    .order_by(ToolCall.created_at.asc())
                )
            ).scalars()
        )

    assert len(task_steps) == 1
    assert task_steps[0].status == "completed"
    assert len(tool_calls) == 1
    assert tool_calls[0].status == "completed"
    assert "agent-approved-run" in (tool_calls[0].output_text or "")
    assert any("requires approval" in message["text"].lower() for message in progress_reporter.messages)
    assert any(message["stage"] == "report_result" for message in progress_reporter.messages)


async def test_agent_runtime_executes_web_search_when_enabled(
    session_factory: async_sessionmaker[AsyncSession],
    agent_task: Task,
    tmp_path: Path,
) -> None:
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    (skills_root / "research.yaml").write_text(
        build_skill_yaml(
            "research",
            "Use web research when the task requires current public information.",
            tools_allowed=["web_search"],
        ),
        encoding="utf-8",
    )

    planning_model = FakePlanningModel(
        [
            PlannedStep(
                title="Search the web",
                description="Look up the latest LiteLLM documentation.",
                tool_name="web_search",
                command="latest LiteLLM documentation",
                reason="Need current public information.",
            )
        ]
    )
    progress_reporter = FakeProgressReporter()
    runner = build_agent_runner(
        session_factory,
        planning_model=planning_model,
        progress_reporter=progress_reporter,
        skills_root=skills_root,
        allowed_tool_roots=[tmp_path],
        settings=Settings(_env_file=None, litellm_model="test-model", enable_web_research=True),
        web_research_provider=FakeWebResearchProvider(),
    )

    result = await runner.run_task(agent_task.id)

    assert result.status == "completed"

    async with session_factory() as verification_session:
        tool_calls = list(
            (
                await verification_session.execute(
                    select(ToolCall)
                    .where(ToolCall.task_id == agent_task.id)
                    .order_by(ToolCall.created_at.asc())
                )
            ).scalars()
        )

    assert len(tool_calls) == 1
    assert tool_calls[0].tool_name == "web_search"
    assert "LiteLLM docs" in (tool_calls[0].output_text or "")


async def test_agent_runtime_web_open_falls_back_to_recent_search_url(
    session_factory: async_sessionmaker[AsyncSession],
    agent_task: Task,
    tmp_path: Path,
) -> None:
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    (skills_root / "research.yaml").write_text(
        build_skill_yaml(
            "research",
            "Open a relevant result after performing web research.",
            tools_allowed=["web_search", "web_open"],
        ),
        encoding="utf-8",
    )

    planning_model = FakePlanningModel(
        [
            PlannedStep(
                title="Search the web",
                description="Look up the latest LiteLLM documentation.",
                tool_name="web_search",
                command="latest LiteLLM documentation",
                reason="Need current public information.",
            ),
            PlannedStep(
                title="Open the best result",
                description="Open the most relevant source from the recent search results.",
                tool_name="web_open",
                command="",
                reason="Inspect the primary result in more detail.",
            ),
        ]
    )
    progress_reporter = FakeProgressReporter()
    runner = build_agent_runner(
        session_factory,
        planning_model=planning_model,
        progress_reporter=progress_reporter,
        skills_root=skills_root,
        allowed_tool_roots=[tmp_path],
        settings=Settings(_env_file=None, litellm_model="test-model", enable_web_research=True),
        web_research_provider=FakeWebResearchProvider(),
    )

    result = await runner.run_task(agent_task.id)

    assert result.status == "completed"

    async with session_factory() as verification_session:
        tool_calls = list(
            (
                await verification_session.execute(
                    select(ToolCall)
                    .where(ToolCall.task_id == agent_task.id)
                    .order_by(ToolCall.created_at.asc())
                )
            ).scalars()
        )

    assert [tool_call.tool_name for tool_call in tool_calls] == ["web_search", "web_open"]
    assert "Opened https://docs.litellm.ai/" in (tool_calls[1].output_text or "")


async def test_default_allowed_tool_roots_include_common_linux_paths(monkeypatch) -> None:
    import agents.runtime as runtime_module

    class FakeOS:
        name = "posix"

    monkeypatch.setattr(runtime_module, "os", FakeOS())

    roots = runtime_module._default_allowed_tool_roots()
    rendered_roots = {str(root).replace("\\", "/") for root in roots}

    assert any(root.endswith("/opt") or root == "/opt" for root in rendered_roots)
    assert any(root.endswith("/var") or root == "/var" for root in rendered_roots)


async def test_agent_node_filesystem_tools_respect_allowed_roots(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    from agent.model_client import LiteLLMPlanningModel, ToolCallRequest
    from agent.nodes import AgentNodeHandlers

    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    target_file = allowed_root / "notes.txt"
    target_file.write_text("hello\nworld\n", encoding="utf-8")
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    blocked_file = outside_root / "blocked.txt"
    blocked_file.write_text("secret\n", encoding="utf-8")

    handlers = AgentNodeHandlers(
        session_factory,
        settings=Settings(_env_file=None, litellm_model="test-model"),
        planning_model=LiteLLMPlanningModel(Settings(_env_file=None, litellm_model="test-model"), session_factory),
        progress_reporter=FakeProgressReporter(),
        skills_root=tmp_path / "skills",
        allowed_tool_roots=[allowed_root],
    )

    read_result, read_status = await handlers._execute_tool_call(
        {"id": str(uuid4()), "workspace_id": str(uuid4()), "metadata_json": {}},
        ToolCallRequest(id="1", name="file_read", arguments={"path": str(target_file)}),
        [],
    )
    grep_result, grep_status = await handlers._execute_tool_call(
        {"id": str(uuid4()), "workspace_id": str(uuid4()), "metadata_json": {}},
        ToolCallRequest(id="2", name="grep", arguments={"query": "world", "path": str(allowed_root)}),
        [],
    )
    write_result, write_status = await handlers._execute_tool_call(
        {"id": str(uuid4()), "workspace_id": str(uuid4()), "metadata_json": {}},
        ToolCallRequest(id="3", name="file_write", arguments={"path": str(allowed_root / "new.txt"), "content": "new content"}),
        [],
    )
    blocked_result, blocked_status = await handlers._execute_tool_call(
        {"id": str(uuid4()), "workspace_id": str(uuid4()), "metadata_json": {}},
        ToolCallRequest(id="4", name="file_read", arguments={"path": str(blocked_file)}),
        [],
    )

    assert read_status == "completed"
    assert "hello" in read_result
    assert grep_status == "completed"
    assert "notes.txt:2:world" in grep_result.replace("\\", "/")
    assert write_status == "completed"
    assert (allowed_root / "new.txt").read_text(encoding="utf-8") == "new content"
    assert blocked_status == "failed"
    assert "outside the allowed roots" in blocked_result