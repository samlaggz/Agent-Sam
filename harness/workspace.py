from __future__ import annotations

from datetime import datetime, timezone
import shutil
import subprocess
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from db.models import WorkspaceRuntime
from harness.exceptions import WorkspaceBoundaryError


class WorkspacePaths(BaseModel):
    task_id: UUID
    root: Path
    repo: Path
    downloads: Path
    screenshots: Path
    source_cache: Path
    logs: Path
    patches: Path
    events_jsonl: Path


class WorkspaceManager:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        *,
        root_dir: str | Path | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._root = Path(root_dir or settings.agent_workspaces_dir).resolve()
        self._snapshots_root = (self._root / ".snapshots").resolve()

    @property
    def root_dir(self) -> Path:
        return self._root

    async def create_task_workspace(self, task_id: UUID) -> WorkspacePaths:
        layout = self._layout(task_id)
        for path in (
            layout.root,
            layout.repo,
            layout.downloads,
            layout.screenshots,
            layout.source_cache,
            layout.logs,
            layout.patches,
        ):
            path.mkdir(parents=True, exist_ok=True)
        layout.events_jsonl.touch(exist_ok=True)
        self._snapshots_root.mkdir(parents=True, exist_ok=True)
        await self._upsert_workspace_record(task_id=task_id, path=layout.root, status="active")
        return layout

    async def get_task_workspace(self, task_id: UUID) -> WorkspacePaths:
        layout = self._layout(task_id)
        if not layout.root.exists():
            return await self.create_task_workspace(task_id)
        await self._touch_workspace_record(task_id)
        return layout

    async def cleanup_workspace(self, task_id: UUID) -> None:
        layout = self._layout(task_id)
        if layout.root.exists():
            shutil.rmtree(layout.root)
        await self._upsert_workspace_record(task_id=task_id, path=layout.root, status="deleted")

    async def snapshot_workspace(self, task_id: UUID) -> str:
        layout = await self.get_task_workspace(task_id)
        snapshot_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        snapshot_dir = self._snapshots_root / f"task_{task_id}" / snapshot_id
        snapshot_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(layout.root, snapshot_dir, dirs_exist_ok=False)
        await self._touch_workspace_record(task_id)
        return snapshot_id

    async def restore_snapshot(self, task_id: UUID, snapshot_id: str) -> WorkspacePaths:
        layout = self._layout(task_id)
        snapshot_dir = self._snapshots_root / f"task_{task_id}" / snapshot_id
        if not snapshot_dir.exists():
            raise FileNotFoundError(f"Snapshot {snapshot_id} does not exist for task {task_id}.")
        if layout.root.exists():
            shutil.rmtree(layout.root)
        shutil.copytree(snapshot_dir, layout.root)
        await self._upsert_workspace_record(task_id=task_id, path=layout.root, status="restored")
        return layout

    async def copy_repo_to_workspace(self, repo_path: str | Path, task_id: UUID) -> Path:
        layout = await self.create_task_workspace(task_id)
        source = Path(repo_path).resolve()
        destination = layout.repo
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination)
        await self._touch_workspace_record(task_id)
        return destination

    async def clone_repo_to_workspace(self, repo_url: str, task_id: UUID) -> Path:
        layout = await self.create_task_workspace(task_id)
        if any(layout.repo.iterdir()):
            raise FileExistsError(f"Workspace repo directory already contains files: {layout.repo}")
        subprocess.run(
            ["git", "clone", repo_url, str(layout.repo)],
            capture_output=True,
            text=True,
            check=True,
        )
        await self._touch_workspace_record(task_id)
        return layout.repo

    def enforce_path_allowed(self, path: str | Path) -> Path:
        resolved = Path(path).resolve()
        try:
            resolved.relative_to(self._root)
        except ValueError as exc:
            raise WorkspaceBoundaryError(f"Path is outside the managed workspaces root: {resolved}") from exc
        return resolved

    def _layout(self, task_id: UUID) -> WorkspacePaths:
        task_root = (self._root / f"task_{task_id}").resolve()
        return WorkspacePaths(
            task_id=task_id,
            root=task_root,
            repo=task_root / "repo",
            downloads=task_root / "downloads",
            screenshots=task_root / "screenshots",
            source_cache=task_root / "source_cache",
            logs=task_root / "logs",
            patches=task_root / "patches",
            events_jsonl=task_root / "events.jsonl",
        )

    async def _upsert_workspace_record(self, *, task_id: UUID, path: Path, status: str) -> None:
        if self._session_factory is None:
            return
        async with self._session_factory() as session:
            statement = select(WorkspaceRuntime).where(WorkspaceRuntime.task_id == task_id)
            record = await session.scalar(statement)
            if record is None:
                record = WorkspaceRuntime(task_id=task_id, path=str(path), status=status)
                session.add(record)
            else:
                record.path = str(path)
                record.status = status
                record.last_used_at = datetime.now(timezone.utc)
            await session.commit()

    async def _touch_workspace_record(self, task_id: UUID) -> None:
        if self._session_factory is None:
            return
        async with self._session_factory() as session:
            statement = select(WorkspaceRuntime).where(WorkspaceRuntime.task_id == task_id)
            record = await session.scalar(statement)
            if record is None:
                return
            record.last_used_at = datetime.now(timezone.utc)
            await session.commit()