import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from db.models import Approval, Message, Task, ToolCall, User, Workspace
from db.repositories import create_task
from gateways.base import GatewayChat, GatewayUser, IncomingGatewayMessage
from gateways.service import AgentGatewayService
from tools.shell_command import ShellCommandRequest, ShellCommandTool


pytestmark = pytest.mark.asyncio


def build_gateway_service(
    session_factory: async_sessionmaker[AsyncSession],
    workspace: Workspace,
    user: User,
    *,
    settings: Settings | None = None,
    model_router=None,
) -> AgentGatewayService:
    del workspace, user
    return AgentGatewayService(session_factory, settings=settings, model_router=model_router)


class FakeModelRouter:
    def __init__(self, responses: list[str] | None = None, *, error: Exception | None = None) -> None:
        self._responses = responses or []
        self._error = error
        self.requests = []

    async def run_completion(self, request):
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        content = self._responses.pop(0) if self._responses else ""
        return SimpleNamespace(content=content)


def build_incoming_message(
    workspace: Workspace,
    user: User,
    text: str,
    *,
    gateway_name: str = "cli",
) -> IncomingGatewayMessage:
    return IncomingGatewayMessage(
        workspace_id=workspace.id,
        user_id=user.id,
        gateway_name=gateway_name,
        gateway_user=GatewayUser(
            gateway_user_id=f"{gateway_name}-user-1",
            username="agent_sam_tester",
            display_name="Agent Sam Tester",
        ),
        gateway_chat=GatewayChat(
            gateway_chat_id=f"{gateway_name}-chat-1",
            title="Gateway Test Chat",
            chat_type="direct",
        ),
        text=text,
        gateway_message_id="101",
    )


async def test_handle_text_message_persists_message_and_creates_task(
    session_factory: async_sessionmaker[AsyncSession],
    workspace: Workspace,
    user: User,
) -> None:
    service = build_gateway_service(session_factory, workspace, user)
    incoming = build_incoming_message(
        workspace,
        user,
        "Investigate the nightly sync failures\nThe failures started after the last deploy.",
    )

    response = await service.handle_incoming_message(incoming)

    assert response.task_id is not None
    assert "Task created." in response.text

    async with session_factory() as session:
        db_message = await session.scalar(select(Message).where(Message.content == incoming.text))
        assert db_message is not None
        assert db_message.metadata_json["gateway_name"] == "cli"
        assert db_message.metadata_json["gateway_user_id"] == "cli-user-1"
        assert db_message.task_id == response.task_id

        task = await session.get(Task, db_message.task_id)
        assert task is not None
        assert task.description == incoming.text
        assert task.created_by_user_id == user.id
        assert task.workspace_id == workspace.id


async def test_handle_text_message_returns_inline_chat_reply_without_creating_task(
    session_factory: async_sessionmaker[AsyncSession],
    workspace: Workspace,
    user: User,
) -> None:
    model_router = FakeModelRouter(responses=["Hi there. Ask a question or use /new when you want queued work."])
    service = build_gateway_service(
        session_factory,
        workspace,
        user,
        settings=Settings(openrouter_api_key="test-key"),
        model_router=model_router,
    )
    incoming = build_incoming_message(workspace, user, "hello")

    response = await service.handle_incoming_message(incoming)

    assert response.task_id is None
    assert response.text == "Hi there. Ask a question or use /new when you want queued work."
    assert len(model_router.requests) == 1

    async with session_factory() as session:
        db_message = await session.scalar(select(Message).where(Message.content == incoming.text))
        assert db_message is not None
        assert db_message.task_id is None
        task_count = len((await session.scalars(select(Task))).all())
        assert task_count == 0


async def test_handle_text_message_reports_web_access_when_enabled(
    session_factory: async_sessionmaker[AsyncSession],
    workspace: Workspace,
    user: User,
) -> None:
    service = build_gateway_service(
        session_factory,
        workspace,
        user,
        settings=Settings(_env_file=None, enable_web_research=True, openrouter_api_key=""),
    )

    response = await service.handle_incoming_message(build_incoming_message(workspace, user, "do you have internet access"))

    assert response.task_id is None
    assert "Yes. I have web research enabled" in response.text


async def test_handle_text_message_capability_question_stays_chat_even_when_action_words_exist(
    session_factory: async_sessionmaker[AsyncSession],
    workspace: Workspace,
    user: User,
) -> None:
    service = build_gateway_service(
        session_factory,
        workspace,
        user,
        settings=Settings(_env_file=None, enable_web_research=True),
    )

    response = await service.handle_incoming_message(
        build_incoming_message(workspace, user, "Can you check do you have internet access?")
    )

    assert response.task_id is None
    assert "web research enabled" in response.text.lower()


async def test_handle_text_message_uses_fallback_chat_reply_when_no_model_is_available(
    session_factory: async_sessionmaker[AsyncSession],
    workspace: Workspace,
    user: User,
) -> None:
    service = build_gateway_service(
        session_factory,
        workspace,
        user,
        settings=Settings(
            openrouter_api_key="",
            litellm_api_key="",
            openai_api_key="",
            anthropic_api_key="",
            gemini_api_key="",
            ollama_base_url="",
        ),
    )

    response = await service.handle_incoming_message(build_incoming_message(workspace, user, "what is this"))

    assert response.task_id is None
    assert "Agent Sam gateway chat" in response.text


async def test_handle_text_message_creates_task_for_explicit_work_request_question(
    session_factory: async_sessionmaker[AsyncSession],
    workspace: Workspace,
    user: User,
) -> None:
    service = build_gateway_service(session_factory, workspace, user)

    response = await service.handle_incoming_message(
        build_incoming_message(workspace, user, "Can you fix the broken nginx deploy?")
    )

    assert response.task_id is not None
    assert "Task created." in response.text
    assert "Planned agent: server_ops_agent" in response.text


async def test_handle_text_message_creates_research_task_and_sets_telegram_progress_metadata(
    session_factory: async_sessionmaker[AsyncSession],
    workspace: Workspace,
    user: User,
) -> None:
    service = build_gateway_service(
        session_factory,
        workspace,
        user,
        settings=Settings(_env_file=None, enable_web_research=True),
    )
    incoming = build_incoming_message(
        workspace,
        user,
        "Can you check on the internet for the latest LiteLLM docs?",
        gateway_name="telegram",
    )

    response = await service.handle_incoming_message(incoming)

    assert response.task_id is not None
    assert "Planned agent: research_agent" in response.text

    async with session_factory() as session:
        task = await session.get(Task, response.task_id)
        assert task is not None
        assert task.metadata_json["source"] == "telegram"
        assert task.metadata_json["source_chat_id"] == "telegram-chat-1"
        assert task.metadata_json["source_user_id"] == "telegram-user-1"


async def test_followup_question_uses_recent_task_from_same_chat(
    session_factory: async_sessionmaker[AsyncSession],
    workspace: Workspace,
    user: User,
) -> None:
    service = build_gateway_service(
        session_factory,
        workspace,
        user,
        settings=Settings(_env_file=None, enable_web_research=True),
    )
    incoming = build_incoming_message(
        workspace,
        user,
        "check the internet for the latest LiteLLM docs",
        gateway_name="telegram",
    )

    create_response = await service.handle_incoming_message(incoming)

    async with session_factory() as session:
        task = await session.get(Task, create_response.task_id)
        assert task is not None
        task.status = "running"
        session.add(
            Message(
                workspace_id=workspace.id,
                user_id=user.id,
                task_id=task.id,
                role="assistant",
                content="Task completed: check the internet for the latest LiteLLM docs\nFound official docs and summary.",
                metadata_json={"transport": "telegram", "source_chat_id": "telegram-chat-1", "stage": "report_result"},
            )
        )
        await session.commit()

    followup_response = await service.handle_incoming_message(
        build_incoming_message(workspace, user, "Any update?", gateway_name="telegram")
    )

    assert followup_response.task_id is None
    assert "Latest task:" in followup_response.text
    assert "Found official docs and summary." in followup_response.text


async def test_handle_new_queue_and_status_commands(
    session_factory: async_sessionmaker[AsyncSession],
    workspace: Workspace,
    user: User,
) -> None:
    service = build_gateway_service(session_factory, workspace, user)
    create_response = await service.handle_incoming_message(
        build_incoming_message(workspace, user, "/new Review deployment logs", gateway_name="telegram")
    )
    assert create_response.task_id is not None

    queue_response = await service.handle_incoming_message(
        build_incoming_message(workspace, user, "/queue", gateway_name="telegram")
    )
    status_response = await service.handle_incoming_message(
        build_incoming_message(
            workspace,
            user,
            f"/status {create_response.task_id}",
            gateway_name="telegram",
        )
    )

    assert "Task created." in create_response.text
    assert "Queue status" in queue_response.text
    assert "Pending: 1" in queue_response.text
    assert f"Task {create_response.task_id}" in status_response.text
    assert "Title: Review deployment logs" in status_response.text


async def test_handle_prioritize_pause_resume_and_cancel_commands(
    session_factory: async_sessionmaker[AsyncSession],
    workspace: Workspace,
    user: User,
) -> None:
    service = build_gateway_service(session_factory, workspace, user)
    creation_response = await service.handle_incoming_message(
        build_incoming_message(workspace, user, "/new Triage support backlog", gateway_name="telegram")
    )
    assert creation_response.task_id is not None
    task_id = creation_response.task_id

    prioritize_response = await service.handle_incoming_message(
        build_incoming_message(workspace, user, f"/prioritize {task_id} urgent", gateway_name="telegram")
    )
    pause_response = await service.handle_incoming_message(
        build_incoming_message(workspace, user, f"/pause {task_id}", gateway_name="telegram")
    )
    resume_response = await service.handle_incoming_message(
        build_incoming_message(workspace, user, f"/resume {task_id}", gateway_name="telegram")
    )
    cancel_response = await service.handle_incoming_message(
        build_incoming_message(workspace, user, f"/cancel {task_id}", gateway_name="telegram")
    )

    assert "reprioritized to urgent" in prioritize_response.text
    assert "Current status: paused" in pause_response.text
    assert "Current status: pending" in resume_response.text
    assert "Current status: cancelled" in cancel_response.text

    async with session_factory() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        assert task.priority == "urgent"
        assert task.status == "cancelled"


async def test_handle_approve_command_marks_approval_and_tool_call(
    session_factory: async_sessionmaker[AsyncSession],
    workspace: Workspace,
    user: User,
) -> None:
    service = build_gateway_service(session_factory, workspace, user)

    async with session_factory() as session:
        message = Message(workspace_id=workspace.id, user_id=user.id, content="Need approval", role="user")
        session.add(message)
        await session.commit()
        await session.refresh(message)

        tool_call = ToolCall(message_id=message.id, tool_name="shell.exec", risk_level="high")
        session.add(tool_call)
        await session.commit()
        await session.refresh(tool_call)

        approval = Approval(
            workspace_id=workspace.id,
            tool_call_id=tool_call.id,
            requested_by_user_id=user.id,
            reason="Run a high-risk command",
        )
        session.add(approval)
        await session.commit()
        await session.refresh(approval)

    response = await service.handle_incoming_message(
        build_incoming_message(workspace, user, f"/approve {approval.id}", gateway_name="telegram")
    )

    assert response.approval_id == approval.id
    assert f"Approval {approval.id} marked as approved." == response.text

    async with session_factory() as session:
        updated_approval = await session.get(Approval, approval.id)
        updated_tool_call = await session.get(ToolCall, tool_call.id)

        assert updated_approval is not None
        assert updated_approval.status == "approved"
        assert updated_approval.reviewed_by_user_id == user.id
        assert updated_approval.reviewed_at is not None
        assert updated_tool_call is not None
        assert updated_tool_call.approved_by_user is True


async def test_handle_approve_command_unblocks_pending_shell_command_execution(
    session_factory: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    workspace: Workspace,
    user: User,
    tmp_path,
) -> None:
    service = build_gateway_service(session_factory, workspace, user)
    task = await create_task(
        session,
        workspace_id=workspace.id,
        title="Gateway approval bridge",
        description="Approve and execute a reviewed shell command.",
        created_by_user_id=user.id,
    )
    tool = ShellCommandTool(session_factory, allowed_roots=[tmp_path])
    command = f'"{sys.executable}" -c "print(\'gateway-approved-run\')"'

    pending_result = await tool.submit_command(
        ShellCommandRequest(
            command=command,
            working_directory=str(tmp_path),
            reason="Run an approved diagnostic command.",
            task_id=task.id,
        )
    )

    assert pending_result.approval_id is not None

    approve_response = await service.handle_incoming_message(
        build_incoming_message(workspace, user, f"/approve {pending_result.approval_id}", gateway_name="cli")
    )
    execution_result = await tool.execute_approved_tool_call(pending_result.tool_call_id)

    assert approve_response.approval_id == pending_result.approval_id
    assert execution_result.status == "completed"
    assert execution_result.exit_code == 0
    assert "gateway-approved-run" in execution_result.stdout

    async with session_factory() as verification_session:
        approval = await verification_session.get(Approval, pending_result.approval_id)
        tool_call = await verification_session.get(ToolCall, pending_result.tool_call_id)

        assert approval is not None
        assert approval.status == "approved"
        assert tool_call is not None
        assert tool_call.approved_by_user is True
        assert tool_call.status == "completed"


async def test_gateway_agents_and_models_commands(
    session_factory: async_sessionmaker[AsyncSession],
    workspace: Workspace,
    user: User,
) -> None:
    service = build_gateway_service(session_factory, workspace, user)

    agents_response = await service.handle_incoming_message(
        build_incoming_message(workspace, user, "/agents", gateway_name="cli")
    )
    models_response = await service.handle_incoming_message(
        build_incoming_message(workspace, user, "/models", gateway_name="cli")
    )

    assert "Available agents:" in agents_response.text
    assert "coding_agent" in agents_response.text
    assert "Configured models:" in models_response.text
    assert "openrouter/" in models_response.text