import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from uuid import UUID

from app.config import Settings
from db.models import Approval, Message, Task, ToolCall, User, Workspace
from gateways.service import AgentGatewayService, IncomingGatewayMessage


pytestmark = pytest.mark.asyncio


def build_gateway_service(
    session_factory: async_sessionmaker[AsyncSession],
    workspace: Workspace,
    user: User,
) -> AgentGatewayService:
    settings = Settings(
        telegram_bot_token="test-token",
        default_workspace_id=workspace.id,
        default_user_id=user.id,
    )
    return AgentGatewayService(settings, session_factory)


async def test_handle_text_message_persists_message_and_creates_task(
    session_factory: async_sessionmaker[AsyncSession],
    workspace: Workspace,
    user: User,
) -> None:
    service = build_gateway_service(session_factory, workspace, user)
    incoming = IncomingGatewayMessage(
        text="Investigate the nightly sync failures\nThe failures started after the last deploy.",
        source="telegram",
        source_user_id="telegram-user-1",
        source_chat_id="telegram-chat-1",
        source_message_id="101",
        source_username="agent_sam_tester",
    )

    response = await service.handle_text_message(incoming)

    assert "Task created." in response

    async with session_factory() as session:
        db_message = await session.scalar(select(Message).where(Message.content == incoming.text))
        assert db_message is not None
        assert db_message.metadata_json["source"] == "telegram"
        assert db_message.task_id is not None

        task = await session.get(Task, db_message.task_id)
        assert task is not None
        assert task.description == incoming.text
        assert task.created_by_user_id == user.id
        assert task.workspace_id == workspace.id


async def test_handle_new_queue_and_status_commands(
    session_factory: async_sessionmaker[AsyncSession],
    workspace: Workspace,
    user: User,
) -> None:
    service = build_gateway_service(session_factory, workspace, user)
    create_request = IncomingGatewayMessage(text="/new Review deployment logs", source="telegram")

    create_response = await service.handle_command("new", create_request, ["Review", "deployment", "logs"])
    created_task_id = create_response.split("\n", maxsplit=2)[1].split(": ", maxsplit=1)[1]

    queue_response = await service.handle_command(
        "queue",
        IncomingGatewayMessage(text="/queue", source="telegram"),
        [],
    )
    status_response = await service.handle_command(
        "status",
        IncomingGatewayMessage(text=f"/status {created_task_id}", source="telegram"),
        [created_task_id],
    )

    assert "Task created." in create_response
    assert "Queue status" in queue_response
    assert "Pending: 1" in queue_response
    assert f"Task {created_task_id}" in status_response
    assert "Title: Review deployment logs" in status_response


async def test_handle_prioritize_pause_resume_and_cancel_commands(
    session_factory: async_sessionmaker[AsyncSession],
    workspace: Workspace,
    user: User,
) -> None:
    service = build_gateway_service(session_factory, workspace, user)
    creation_response = await service.handle_command(
        "new",
        IncomingGatewayMessage(text="/new Triage support backlog", source="telegram"),
        ["Triage", "support", "backlog"],
    )
    task_id = UUID(creation_response.split("\n", maxsplit=2)[1].split(": ", maxsplit=1)[1])

    prioritize_response = await service.handle_command(
        "prioritize",
        IncomingGatewayMessage(text=f"/prioritize {task_id} urgent", source="telegram"),
        [str(task_id), "urgent"],
    )
    pause_response = await service.handle_command(
        "pause",
        IncomingGatewayMessage(text=f"/pause {task_id}", source="telegram"),
        [str(task_id)],
    )
    resume_response = await service.handle_command(
        "resume",
        IncomingGatewayMessage(text=f"/resume {task_id}", source="telegram"),
        [str(task_id)],
    )
    cancel_response = await service.handle_command(
        "cancel",
        IncomingGatewayMessage(text=f"/cancel {task_id}", source="telegram"),
        [str(task_id)],
    )

    assert "reprioritized to urgent" in prioritize_response
    assert "Current status: paused" in pause_response
    assert "Current status: pending" in resume_response
    assert "Current status: cancelled" in cancel_response

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

    response = await service.handle_command(
        "approve",
        IncomingGatewayMessage(text=f"/approve {approval.id}", source="telegram"),
        [str(approval.id)],
    )

    assert f"Approval {approval.id} marked as approved." == response

    async with session_factory() as session:
        updated_approval = await session.get(Approval, approval.id)
        updated_tool_call = await session.get(ToolCall, tool_call.id)

        assert updated_approval is not None
        assert updated_approval.status == "approved"
        assert updated_approval.reviewed_by_user_id == user.id
        assert updated_approval.reviewed_at is not None
        assert updated_tool_call is not None
        assert updated_tool_call.approved_by_user is True