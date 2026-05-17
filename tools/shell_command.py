from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.models import Approval, Task, ToolCall
from tools.shell_risk_rules import BLOCKED_RULES, DANGEROUS_RULES, HIGH_RISK_RULES, MEDIUM_RISK_RULES, SAFE_RULES, RiskRule


DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_OUTPUT_CHARS = 24_000
ALLOWED_ENVIRONMENT_KEYS = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "HOME",
    "USERPROFILE",
    "TMP",
    "TEMP",
    "LANG",
    "TERM",
    "SHELL",
    "USER",
    "LOGNAME",
    "HOSTNAME",
    "LC_ALL",
    "LC_CTYPE",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
)


@dataclass(frozen=True)
class ShellCommandRequest:
    command: str
    working_directory: str
    reason: str
    task_id: UUID
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True)
class RiskAssessment:
    risk_level: str
    requires_approval: bool
    blocked: bool
    reason: str
    matched_rule: str


@dataclass(frozen=True)
class ShellCommandResult:
    tool_call_id: UUID
    status: str
    risk_level: str
    requires_approval: bool
    approved_by_user: bool
    stdout: str
    stderr: str
    exit_code: int | None
    duration_ms: int | None
    approval_id: UUID | None = None


class ShellCommandTool:
    tool_name = "shell_command"

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        allowed_roots: list[str | Path] | tuple[str | Path, ...] | None = None,
        default_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._default_timeout_seconds = default_timeout_seconds
        roots = allowed_roots or [Path.cwd()]
        self._allowed_roots = [Path(root).resolve() for root in roots]
        self._default_root = self._allowed_roots[0]

    async def submit_command(self, request: ShellCommandRequest) -> ShellCommandResult:
        async with self._session_factory() as session:
            task = await self._get_task(session, request.task_id)
            assessment = self._classify_command(request.command)
            resolved_directory, directory_error = self._resolve_working_directory(request.working_directory)

            if directory_error is not None:
                assessment = RiskAssessment(
                    risk_level="dangerous",
                    requires_approval=False,
                    blocked=True,
                    reason=directory_error,
                    matched_rule="working-directory-guard",
                )

            tool_call = ToolCall(
                tool_name=self.tool_name,
                task_id=task.id,
                input_payload=self._build_input_payload(request, resolved_directory, assessment),
                risk_level=assessment.risk_level,
                status="pending_approval" if assessment.requires_approval else "pending",
            )
            session.add(tool_call)
            await session.flush()

            if assessment.blocked:
                tool_call.status = "blocked"
                tool_call.stderr_text = assessment.reason
                await session.commit()
                await session.refresh(tool_call)
                return self._to_result(tool_call)

            if assessment.requires_approval:
                approval = Approval(
                    workspace_id=task.workspace_id,
                    task_id=task.id,
                    tool_call_id=tool_call.id,
                    requested_by_user_id=task.created_by_user_id,
                    reason=self._build_approval_reason(request, assessment),
                    metadata_json={
                        "tool_name": self.tool_name,
                        "command": request.command,
                        "working_directory": str(resolved_directory),
                        "matched_rule": assessment.matched_rule,
                    },
                )
                session.add(approval)
                await session.commit()
                await session.refresh(tool_call)
                await session.refresh(approval)
                return self._to_result(tool_call, approval_id=approval.id)

            tool_call.status = "running"
            await session.commit()
            await session.refresh(tool_call)

        return await self._execute_tool_call(tool_call.id)

    async def execute_approved_tool_call(self, tool_call_id: UUID) -> ShellCommandResult:
        async with self._session_factory() as session:
            tool_call = await self._get_tool_call(session, tool_call_id)
            if tool_call.status not in {"pending_approval", "pending", "blocked"}:
                return self._to_result(tool_call)

            request = self._request_from_tool_call(tool_call)
            assessment = self._classify_command(request.command)
            resolved_directory, directory_error = self._resolve_working_directory(request.working_directory)

            if directory_error is not None:
                tool_call.status = "blocked"
                tool_call.risk_level = "dangerous"
                tool_call.stderr_text = directory_error
                await session.commit()
                await session.refresh(tool_call)
                return self._to_result(tool_call)

            if assessment.blocked:
                tool_call.status = "blocked"
                tool_call.risk_level = assessment.risk_level
                tool_call.stderr_text = assessment.reason
                await session.commit()
                await session.refresh(tool_call)
                return self._to_result(tool_call)

            if assessment.requires_approval and not await self._is_tool_call_approved(session, tool_call.id):
                tool_call.status = "pending_approval"
                await session.commit()
                await session.refresh(tool_call)
                return self._to_result(tool_call)

            tool_call.status = "running"
            tool_call.risk_level = assessment.risk_level
            await session.commit()
            await session.refresh(tool_call)

        return await self._execute_tool_call(tool_call_id)

    def _classify_command(self, command: str) -> RiskAssessment:
        normalized = " ".join(command.strip().split())

        for rule in BLOCKED_RULES:
            if rule.matches(normalized):
                return RiskAssessment(
                    risk_level="dangerous",
                    requires_approval=False,
                    blocked=True,
                    reason=rule.reason,
                    matched_rule=rule.name,
                )

        for risk_level, rules in (
            ("safe", SAFE_RULES),
            ("dangerous", DANGEROUS_RULES),
            ("high", HIGH_RISK_RULES),
            ("medium", MEDIUM_RISK_RULES),
        ):
            matched = self._match_rule(normalized, rules)
            if matched is not None:
                return RiskAssessment(
                    risk_level=risk_level,
                    requires_approval=risk_level != "safe",
                    blocked=False,
                    reason=matched.reason,
                    matched_rule=matched.name,
                )

        return RiskAssessment(
            risk_level="safe",
            requires_approval=False,
            blocked=False,
            reason="Default: all commands allowed in admin mode.",
            matched_rule="default-admin-mode",
        )

    def _match_rule(self, command: str, rules: list[RiskRule]) -> RiskRule | None:
        for rule in rules:
            if rule.matches(command):
                return rule
        return None

    def _resolve_working_directory(self, working_directory: str) -> tuple[Path, str | None]:
        raw_path = Path(working_directory)
        resolved = (self._default_root / raw_path).resolve() if not raw_path.is_absolute() else raw_path.resolve()

        if not resolved.exists() or not resolved.is_dir():
            return resolved, "Working directory does not exist or is not a directory."
        if not any(resolved.is_relative_to(root) for root in self._allowed_roots):
            return resolved, "Working directory is outside the allowed command roots."

        return resolved, None

    def _build_input_payload(
        self,
        request: ShellCommandRequest,
        resolved_directory: Path,
        assessment: RiskAssessment,
    ) -> dict[str, Any]:
        return {
            "command": request.command,
            "working_directory": str(resolved_directory),
            "reason": request.reason,
            "timeout_seconds": request.timeout_seconds,
            "classification_reason": assessment.reason,
            "matched_rule": assessment.matched_rule,
        }

    def _build_approval_reason(self, request: ShellCommandRequest, assessment: RiskAssessment) -> str:
        return (
            f"{request.reason}\n"
            f"Command: {request.command}\n"
            f"Working directory: {request.working_directory}\n"
            f"Risk: {assessment.risk_level}\n"
            f"Reason: {assessment.reason}"
        )

    async def _execute_tool_call(self, tool_call_id: UUID) -> ShellCommandResult:
        async with self._session_factory() as session:
            tool_call = await self._get_tool_call(session, tool_call_id)
            request = self._request_from_tool_call(tool_call)
            resolved_directory = Path(tool_call.input_payload["working_directory"])

            start = time.perf_counter()
            stdout_text = ""
            stderr_text = ""
            exit_code: int | None = None
            status = "completed"

            process = await asyncio.create_subprocess_shell(
                request.command,
                cwd=str(resolved_directory),
                env=self._build_environment(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(),
                    timeout=request.timeout_seconds,
                )
                exit_code = process.returncode
                stdout_text = self._truncate_output(stdout_bytes.decode("utf-8", errors="replace"))
                stderr_text = self._truncate_output(stderr_bytes.decode("utf-8", errors="replace"))
                status = "completed" if exit_code == 0 else "failed"
            except asyncio.TimeoutError:
                process.kill()
                stdout_bytes, stderr_bytes = await process.communicate()
                stdout_text = self._truncate_output(stdout_bytes.decode("utf-8", errors="replace"))
                stderr_text = self._truncate_output(
                    f"{stderr_bytes.decode('utf-8', errors='replace')}\nCommand timed out after {request.timeout_seconds} seconds.".strip()
                )
                exit_code = process.returncode
                status = "timed_out"

            duration_ms = max(1, int((time.perf_counter() - start) * 1000))

            tool_call.output_text = stdout_text or None
            tool_call.stderr_text = stderr_text or None
            tool_call.exit_code = exit_code
            tool_call.duration_ms = duration_ms
            tool_call.status = status

            await session.commit()
            await session.refresh(tool_call)
            return self._to_result(tool_call)

    async def _get_task(self, session: AsyncSession, task_id: UUID) -> Task:
        task = await session.get(Task, task_id)
        if task is None:
            raise ValueError(f"Task {task_id} does not exist.")
        return task

    async def _get_tool_call(self, session: AsyncSession, tool_call_id: UUID) -> ToolCall:
        tool_call = await session.get(ToolCall, tool_call_id)
        if tool_call is None:
            raise ValueError(f"Tool call {tool_call_id} does not exist.")
        if tool_call.tool_name != self.tool_name:
            raise ValueError(f"Tool call {tool_call_id} does not belong to {self.tool_name}.")
        return tool_call

    async def _is_tool_call_approved(self, session: AsyncSession, tool_call_id: UUID) -> bool:
        tool_call = await self._get_tool_call(session, tool_call_id)
        if tool_call.approved_by_user:
            return True

        statement = select(Approval).where(
            Approval.tool_call_id == tool_call_id,
            Approval.status == "approved",
        )
        approval = await session.scalar(statement)
        return approval is not None

    def _request_from_tool_call(self, tool_call: ToolCall) -> ShellCommandRequest:
        input_payload = tool_call.input_payload
        return ShellCommandRequest(
            command=str(input_payload["command"]),
            working_directory=str(input_payload["working_directory"]),
            reason=str(input_payload["reason"]),
            task_id=tool_call.task_id,
            timeout_seconds=float(input_payload.get("timeout_seconds", self._default_timeout_seconds)),
        )

    def _build_environment(self) -> dict[str, str]:
        environment = {key: value for key, value in os.environ.items() if key in ALLOWED_ENVIRONMENT_KEYS}
        environment.setdefault("PYTHONIOENCODING", "utf-8")
        environment.setdefault("PYTHONUNBUFFERED", "1")
        return environment

    def _truncate_output(self, text: str) -> str:
        if len(text) <= MAX_OUTPUT_CHARS:
            return text
        return f"{text[:MAX_OUTPUT_CHARS]}\n[output truncated]"

    def _to_result(self, tool_call: ToolCall, *, approval_id: UUID | None = None) -> ShellCommandResult:
        return ShellCommandResult(
            tool_call_id=tool_call.id,
            status=tool_call.status,
            risk_level=tool_call.risk_level,
            requires_approval=tool_call.status == "pending_approval",
            approved_by_user=tool_call.approved_by_user,
            stdout=tool_call.output_text or "",
            stderr=tool_call.stderr_text or "",
            exit_code=tool_call.exit_code,
            duration_ms=tool_call.duration_ms,
            approval_id=approval_id,
        )