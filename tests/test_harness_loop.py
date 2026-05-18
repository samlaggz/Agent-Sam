from __future__ import annotations

from uuid import uuid4

import pytest

from agent.graph import AgentRunResult
from app.config import Settings
from db.repositories import append_event, create_task
from harness.actions import ApprovalAction, FinishAction, InstallLibraryAction
from harness.events import EventStore
from harness.loop import HarnessLoop
from harness.model_adapter import ModelAdapter, ModelRequest, ModelResponse
from harness.tool_executor import ToolExecutor
from harness.workspace import WorkspaceManager


class ScriptedModelAdapter(ModelAdapter):
    def __init__(self, responses) -> None:
        self._responses = list(responses)

    async def next_action(self, request: ModelRequest) -> ModelResponse:
        action = self._responses.pop(0)
        return ModelResponse(model="scripted", content=action.model_dump_json(), action=action)


@pytest.mark.asyncio
async def test_harness_loop_stops_on_finish_action(session_factory, session, workspace, tmp_path) -> None:
    task = await create_task(session, workspace_id=workspace.id, title="Finish me")
    settings = Settings(agent_workspaces_dir=str(tmp_path / "workspaces"))
    manager = WorkspaceManager(settings, session_factory)
    event_store = EventStore(session_factory)
    executor = ToolExecutor(settings, session_factory, event_store)
    loop = HarnessLoop(settings, session_factory, manager, event_store, ScriptedModelAdapter([FinishAction(summary="done")]), executor)

    result = await loop.run_task(task_id=task.id, agent_slug="coding_agent", initial_user_message="do it")

    assert isinstance(result, AgentRunResult)
    assert result.status == "completed"
    assert result.final_summary == "done"


@pytest.mark.asyncio
async def test_harness_loop_pauses_on_approval(session_factory, session, workspace, tmp_path) -> None:
    task = await create_task(session, workspace_id=workspace.id, title="Needs approval")
    settings = Settings(agent_workspaces_dir=str(tmp_path / "workspaces"), agent_require_approval_for_installs=True)
    manager = WorkspaceManager(settings, session_factory)
    event_store = EventStore(session_factory)
    executor = ToolExecutor(settings, session_factory, event_store)
    loop = HarnessLoop(
        settings,
        session_factory,
        manager,
        event_store,
        ScriptedModelAdapter([InstallLibraryAction(ecosystem="python", package_name="requests", reason="Need it")]),
        executor,
    )

    result = await loop.run_task(task_id=task.id, agent_slug="coding_agent", initial_user_message="install it")

    assert result.status == "paused"
    assert result.approval_id is not None


@pytest.mark.asyncio
async def test_harness_loop_resumes_from_history(session_factory, session, workspace, tmp_path) -> None:
    task = await create_task(session, workspace_id=workspace.id, title="Resume task")
    settings = Settings(agent_workspaces_dir=str(tmp_path / "workspaces"))
    manager = WorkspaceManager(settings, session_factory)
    event_store = EventStore(session_factory)
    executor = ToolExecutor(settings, session_factory, event_store)
    await append_event(session, task_id=task.id, agent_run_id=None, agent_slug="coding_agent", event_type="user_message", content="Continue")
    loop = HarnessLoop(settings, session_factory, manager, event_store, ScriptedModelAdapter([FinishAction(summary="resumed")]), executor)

    result = await loop.run_task(task_id=task.id, agent_slug="coding_agent")

    assert result.status == "completed"
    assert result.final_summary == "resumed"