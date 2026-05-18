from __future__ import annotations

from uuid import uuid4

import pytest

from app.config import Settings
from db.repositories import create_task
from harness.actions import FileWriteAction, InstallLibraryAction, ShellAction
from harness.events import EventStore
from harness.observations import ApprovalObservation, ErrorObservation, FileObservation
from harness.tool_executor import ToolExecutor
from harness.workspace import WorkspaceManager


@pytest.mark.asyncio
async def test_tool_executor_requires_approval_for_installs(session_factory, session, workspace) -> None:
    task = await create_task(session, workspace_id=workspace.id, title="Install requests")
    settings = Settings(agent_workspaces_dir="./workspaces", agent_require_approval_for_installs=True)
    event_store = EventStore(session_factory)
    manager = WorkspaceManager(settings, session_factory, root_dir=workspace.slug)
    layout = await manager.create_task_workspace(task.id)
    executor = ToolExecutor(settings, session_factory, event_store)

    observation = await executor.execute_action(
        InstallLibraryAction(ecosystem="python", package_name="requests", reason="Needed for task"),
        task_id=task.id,
        agent_run_id=None,
        agent_slug="coding_agent",
        workspace=layout,
    )

    assert isinstance(observation, ApprovalObservation)
    assert observation.status == "pending"


@pytest.mark.asyncio
async def test_tool_executor_blocks_dangerous_command(session_factory, session, workspace, tmp_path) -> None:
    task = await create_task(session, workspace_id=workspace.id, title="Danger")
    settings = Settings(agent_workspaces_dir=str(tmp_path / "workspaces"))
    event_store = EventStore(session_factory)
    manager = WorkspaceManager(settings, session_factory)
    layout = await manager.create_task_workspace(task.id)
    executor = ToolExecutor(settings, session_factory, event_store)

    observation = await executor.execute_action(
        ShellAction(command="rm -rf /", reason="bad"),
        task_id=task.id,
        agent_run_id=None,
        agent_slug="coding_agent",
        workspace=layout,
    )

    assert isinstance(observation, ErrorObservation)
    assert "blocked" in observation.error_message.lower()


@pytest.mark.asyncio
async def test_tool_executor_writes_file_inside_workspace(session_factory, session, workspace, tmp_path) -> None:
    task = await create_task(session, workspace_id=workspace.id, title="Write file")
    settings = Settings(agent_workspaces_dir=str(tmp_path / "workspaces"))
    event_store = EventStore(session_factory)
    manager = WorkspaceManager(settings, session_factory)
    layout = await manager.create_task_workspace(task.id)
    executor = ToolExecutor(settings, session_factory, event_store)

    observation = await executor.execute_action(
        FileWriteAction(path="repo/hello.txt", content="hi\n"),
        task_id=task.id,
        agent_run_id=None,
        agent_slug="coding_agent",
        workspace=layout,
    )

    assert isinstance(observation, FileObservation)
    assert observation.changed is True