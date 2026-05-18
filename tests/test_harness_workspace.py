from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from app.config import Settings
from harness.exceptions import WorkspaceBoundaryError
from harness.workspace import WorkspaceManager


@pytest.mark.asyncio
async def test_workspace_manager_creates_snapshot_and_restores(tmp_path, session_factory) -> None:
    settings = Settings(agent_workspaces_dir=str(tmp_path / "workspaces"))
    manager = WorkspaceManager(settings, session_factory)
    task_id = uuid4()

    layout = await manager.create_task_workspace(task_id)
    file_path = layout.repo / "hello.txt"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("before", encoding="utf-8")

    snapshot_id = await manager.snapshot_workspace(task_id)
    file_path.write_text("after", encoding="utf-8")
    await manager.restore_snapshot(task_id, snapshot_id)

    restored_layout = await manager.get_task_workspace(task_id)
    assert (restored_layout.repo / "hello.txt").read_text(encoding="utf-8") == "before"


@pytest.mark.asyncio
async def test_workspace_manager_blocks_paths_outside_root(tmp_path, session_factory) -> None:
    settings = Settings(agent_workspaces_dir=str(tmp_path / "workspaces"))
    manager = WorkspaceManager(settings, session_factory)
    with pytest.raises(WorkspaceBoundaryError):
        manager.enforce_path_allowed(Path(tmp_path).parent / "escape.txt")