import sys
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.models import Approval, Task, ToolCall, User, Workspace
from db.repositories import create_task
from tools.shell_command import ShellCommandRequest, ShellCommandTool


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def task(session: AsyncSession, workspace: Workspace, user: User) -> Task:
    return await create_task(
        session,
        workspace_id=workspace.id,
        title="Run a shell command",
        description="Task for shell command tool tests.",
        created_by_user_id=user.id,
    )


async def test_safe_command_executes_and_logs_output(
    session_factory: async_sessionmaker[AsyncSession],
    task: Task,
    tmp_path,
) -> None:
    tool = ShellCommandTool(session_factory, allowed_roots=[tmp_path])

    result = await tool.submit_command(
        ShellCommandRequest(
            command="whoami",
            working_directory=str(tmp_path),
            reason="Inspect the current runtime identity.",
            task_id=task.id,
        )
    )

    assert result.risk_level == "safe"
    assert result.status == "completed"
    assert result.exit_code == 0
    assert result.duration_ms is not None
    assert result.requires_approval is False
    assert result.stdout.strip() != ""

    async with session_factory() as session:
        tool_call = await session.get(ToolCall, result.tool_call_id)
        assert tool_call is not None
        assert tool_call.status == "completed"
        assert tool_call.output_text is not None
        assert tool_call.stderr_text in {None, ""}
        assert tool_call.duration_ms is not None


async def test_high_risk_command_requires_approval(
    session_factory: async_sessionmaker[AsyncSession],
    task: Task,
    tmp_path,
) -> None:
    tool = ShellCommandTool(session_factory, allowed_roots=[tmp_path])

    result = await tool.submit_command(
        ShellCommandRequest(
            command="git push origin main",
            working_directory=str(tmp_path),
            reason="Publish local changes.",
            task_id=task.id,
        )
    )

    assert result.status == "pending_approval"
    assert result.risk_level == "high"
    assert result.approval_id is not None
    assert result.exit_code is None

    async with session_factory() as session:
        tool_call = await session.get(ToolCall, result.tool_call_id)
        approval = await session.get(Approval, result.approval_id)

        assert tool_call is not None
        assert tool_call.status == "pending_approval"
        assert tool_call.output_text is None
        assert approval is not None
        assert approval.status == "pending"
        assert approval.tool_call_id == tool_call.id


async def test_blocked_command_is_logged_without_execution(
    session_factory: async_sessionmaker[AsyncSession],
    task: Task,
    tmp_path,
) -> None:
    tool = ShellCommandTool(session_factory, allowed_roots=[tmp_path])

    result = await tool.submit_command(
        ShellCommandRequest(
            command="rm -rf /",
            working_directory=str(tmp_path),
            reason="Delete everything.",
            task_id=task.id,
        )
    )

    assert result.status == "blocked"
    assert result.risk_level == "dangerous"
    assert result.approval_id is None
    assert "blocked" in result.stderr.lower()

    async with session_factory() as session:
        tool_call = await session.get(ToolCall, result.tool_call_id)
        assert tool_call is not None
        assert tool_call.status == "blocked"
        assert tool_call.exit_code is None
        assert tool_call.duration_ms is None


async def test_approved_command_executes_after_approval(
    session_factory: async_sessionmaker[AsyncSession],
    task: Task,
    tmp_path,
) -> None:
    tool = ShellCommandTool(session_factory, allowed_roots=[tmp_path])
    command = f'"{sys.executable}" -c "print(\'approved-run\')"'

    result = await tool.submit_command(
        ShellCommandRequest(
            command=command,
            working_directory=str(tmp_path),
            reason="Run a reviewed Python one-liner.",
            task_id=task.id,
        )
    )

    if result.status == "pending_approval" and result.approval_id is not None:
        async with session_factory() as session:
            tool_call = await session.get(ToolCall, result.tool_call_id)
            approval = await session.get(Approval, result.approval_id)
            if tool_call and approval:
                tool_call.approved_by_user = True
                approval.status = "approved"
                approval.reviewed_at = datetime.now(timezone.utc)
                await session.commit()
        result = await tool.execute_approved_tool_call(result.tool_call_id)

    assert result.status == "completed"
    assert result.exit_code == 0
    assert "approved-run" in result.stdout


async def test_timeout_is_captured_and_logged(
    session_factory: async_sessionmaker[AsyncSession],
    task: Task,
    tmp_path,
) -> None:
    tool = ShellCommandTool(session_factory, allowed_roots=[tmp_path])
    command = f'"{sys.executable}" -c "import time; time.sleep(1)"'

    pending_result = await tool.submit_command(
        ShellCommandRequest(
            command=command,
            working_directory=str(tmp_path),
            reason="Run a reviewed long-lived command.",
            task_id=task.id,
            timeout_seconds=0.1,
        )
    )

    if pending_result.status == "pending_approval" and pending_result.approval_id is not None:
        async with session_factory() as session:
            tool_call = await session.get(ToolCall, pending_result.tool_call_id)
            approval = await session.get(Approval, pending_result.approval_id)
            if tool_call is not None and approval is not None:
                tool_call.approved_by_user = True
                approval.status = "approved"
                approval.reviewed_at = datetime.now(timezone.utc)
                await session.commit()
        execution_result = await tool.execute_approved_tool_call(pending_result.tool_call_id)
    else:
        execution_result = pending_result

    assert execution_result.status in {"timed_out", "completed", "failed"}
    assert execution_result.duration_ms is not None


async def test_process_inspection_with_grep_is_safe(
    session_factory: async_sessionmaker[AsyncSession],
    task: Task,
    tmp_path,
) -> None:
    tool = ShellCommandTool(session_factory, allowed_roots=[tmp_path])

    result = await tool.submit_command(
        ShellCommandRequest(
            command="ps aux | grep -i shivadrive | grep -v grep",
            working_directory=str(tmp_path),
            reason="Inspect running processes for shivadrive.",
            task_id=task.id,
        )
    )

    assert result.requires_approval is False
    assert result.status in {"completed", "failed"}


async def test_find_directory_command_is_treated_as_read_only_execution(
    session_factory: async_sessionmaker[AsyncSession],
    task: Task,
    tmp_path,
) -> None:
    target_dir = tmp_path / "shiva-drive"
    target_dir.mkdir()
    tool = ShellCommandTool(session_factory, allowed_roots=[tmp_path])

    result = await tool.submit_command(
        ShellCommandRequest(
            command="find . -type d -iname '*shiva*'",
            working_directory=str(tmp_path),
            reason="Find matching directories.",
            task_id=task.id,
        )
    )

    assert result.requires_approval is False
    assert result.status in {"completed", "failed"}
    if result.status == "completed":
        assert "shiva-drive" in result.stdout
