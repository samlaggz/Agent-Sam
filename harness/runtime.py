from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from harness.exceptions import PolicyViolationError, WorkspaceBoundaryError
from harness.file_editor import FileEditor
from harness.workspace import WorkspacePaths
from tools.shell_command import DEFAULT_TIMEOUT_SECONDS, ShellCommandRequest, ShellCommandTool


class CommandResult(BaseModel):
    command: str
    cwd: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    duration_ms: int | None = None
    status: str = "completed"
    tool_call_id: UUID | None = None
    approval_id: UUID | None = None


class Runtime:
    async def run_shell(self, command: str, *, cwd: str | None = None, env: dict[str, str] | None = None, timeout: float | None = None) -> CommandResult:
        raise NotImplementedError

    def read_file(self, path: str) -> str:
        raise NotImplementedError

    def write_file(self, path: str, content: str) -> dict[str, Any]:
        raise NotImplementedError

    def apply_patch(self, patch: str) -> dict[str, Any]:
        raise NotImplementedError

    def list_files(self, path: str | None = None) -> list[str]:
        raise NotImplementedError

    async def install_library(self, package: str, *, ecosystem: str, version: str | None = None, scope: str = "workspace") -> CommandResult:
        raise NotImplementedError

    async def clone_repo(self, url: str, *, destination: str | None = None) -> CommandResult:
        raise NotImplementedError

    def get_workspace_path(self) -> Path:
        raise NotImplementedError

    async def cleanup(self) -> None:
        raise NotImplementedError


class LocalRuntime(Runtime):
    def __init__(
        self,
        settings: Settings,
        workspace: WorkspacePaths,
        *,
        task_id: UUID | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._settings = settings
        self._workspace = workspace
        self._task_id = task_id
        self._session_factory = session_factory
        self._editor = FileEditor(workspace.root, workspace.patches)
        self._shell_tool = None
        if session_factory is not None and task_id is not None:
            self._shell_tool = ShellCommandTool(
                session_factory,
                allowed_roots=[workspace.root],
                default_timeout_seconds=float(settings.agent_shell_timeout_seconds),
            )

    async def run_shell(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        self._ensure_not_root()
        working_directory = self._resolve_cwd(cwd)
        if self._shell_tool is not None and self._task_id is not None:
            result = await self._shell_tool.submit_command(
                ShellCommandRequest(
                    command=command,
                    working_directory=str(working_directory),
                    reason="Harness runtime shell execution.",
                    task_id=self._task_id,
                    timeout_seconds=float(timeout or self._settings.agent_shell_timeout_seconds),
                )
            )
            return CommandResult(
                command=command,
                cwd=str(working_directory),
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
                duration_ms=result.duration_ms,
                status=result.status,
                tool_call_id=result.tool_call_id,
                approval_id=result.approval_id,
            )

        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(working_directory),
            env={**os.environ, **(env or {})},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timeout_value = float(timeout or DEFAULT_TIMEOUT_SECONDS)
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout_value)
        except asyncio.TimeoutError:
            process.kill()
            stdout_bytes, stderr_bytes = await process.communicate()
            return CommandResult(
                command=command,
                cwd=str(working_directory),
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=(stderr_bytes.decode("utf-8", errors="replace") + "\nCommand timed out.").strip(),
                exit_code=process.returncode,
                status="timed_out",
            )
        return CommandResult(
            command=command,
            cwd=str(working_directory),
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            exit_code=process.returncode,
            status="completed" if process.returncode == 0 else "failed",
        )

    def read_file(self, path: str) -> str:
        return self._editor.read_file(path)

    def write_file(self, path: str, content: str) -> dict[str, Any]:
        return self._editor.write_file(path, content)

    def apply_patch(self, patch: str) -> dict[str, Any]:
        return self._editor.apply_patch(patch)

    def list_files(self, path: str | None = None) -> list[str]:
        target = self._resolve_cwd(path)
        files: list[str] = []
        for child in sorted(target.rglob("*")):
            if child.is_file():
                files.append(str(child.relative_to(self._workspace.root)))
        return files

    async def install_library(
        self,
        package: str,
        *,
        ecosystem: str,
        version: str | None = None,
        scope: str = "workspace",
    ) -> CommandResult:
        package_spec = f"{package}=={version}" if version and ecosystem == "python" else package
        if ecosystem == "python":
            command = f'python -m pip install {package_spec}'
        elif ecosystem == "node":
            command = f'npm install {package_spec}'
        elif ecosystem == "system":
            command = f'apt-get install -y {package_spec}'
        else:
            command = package_spec
        if scope == "global":
            raise PolicyViolationError("Global installs are blocked by the runtime.")
        return await self.run_shell(command, cwd=str(self._workspace.repo))

    async def clone_repo(self, url: str, *, destination: str | None = None) -> CommandResult:
        repo_destination = self._resolve_cwd(destination or str(self._workspace.repo))
        if repo_destination.exists() and any(repo_destination.iterdir()):
            raise WorkspaceBoundaryError(f"Destination is not empty: {repo_destination}")
        command = f'git clone {url} "{repo_destination}"'
        return await self.run_shell(command, cwd=str(self._workspace.root))

    def get_workspace_path(self) -> Path:
        return self._workspace.root

    async def cleanup(self) -> None:
        return None

    def _resolve_cwd(self, path: str | None) -> Path:
        raw = Path(path) if path is not None else self._workspace.repo
        resolved = raw.resolve() if raw.is_absolute() else (self._workspace.root / raw).resolve()
        try:
            resolved.relative_to(self._workspace.root)
        except ValueError as exc:
            raise WorkspaceBoundaryError(f"Path is outside the workspace: {resolved}") from exc
        return resolved

    def _ensure_not_root(self) -> None:
        if os.name == "nt":
            return
        geteuid = getattr(os, "geteuid", None)
        if callable(geteuid) and geteuid() == 0:
            raise PolicyViolationError("Harness runtime refuses to execute as root.")