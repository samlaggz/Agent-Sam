from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.models import Message, Task, User, Workspace
from gateways.base import GatewayResponse
from gateways.cli.gateway import DATABASE_NOT_READY_MESSAGE, CLIGateway
from gateways.service import AgentGatewayService


pytestmark = pytest.mark.asyncio


class FakeGatewayService:
    def __init__(
        self,
        responses: list[GatewayResponse] | None = None,
        *,
        error: Exception | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        self._responses = responses or []
        self._error = error
        self._delay_seconds = delay_seconds
        self.received_texts: list[str] = []

    async def handle_incoming_message(self, incoming) -> GatewayResponse:
        self.received_texts.append(incoming.text)
        if self._delay_seconds > 0:
            await asyncio.sleep(self._delay_seconds)
        if self._error is not None:
            raise self._error
        if self._responses:
            return self._responses.pop(0)
        return GatewayResponse(text=f"echo:{incoming.text}")


async def test_cli_gateway_sends_text_into_shared_service(
    session_factory: async_sessionmaker[AsyncSession],
    workspace: Workspace,
    user: User,
) -> None:
    outputs: list[str] = []
    gateway = CLIGateway(
        service=AgentGatewayService(session_factory),
        workspace_id=workspace.id,
        user_id=user.id,
        output_writer=outputs.append,
    )

    response = await gateway.handle_cli_input("Create a task from CLI")

    assert response is not None
    assert response.task_id is not None
    assert any("Got it" in output or "Task ID:" in output for output in outputs)

    async with session_factory() as session:
        db_message = await session.scalar(select(Message).where(Message.content == "Create a task from CLI"))
        db_task = await session.get(Task, response.task_id)
        assert db_message is not None
        assert db_message.metadata_json["gateway_name"] == "cli"
        assert db_task is not None


async def test_cli_gateway_exit_command_does_not_create_task(
    session_factory: async_sessionmaker[AsyncSession],
    workspace: Workspace,
    user: User,
) -> None:
    outputs: list[str] = []
    gateway = CLIGateway(
        service=AgentGatewayService(session_factory),
        workspace_id=workspace.id,
        user_id=user.id,
        output_writer=outputs.append,
    )

    response = await gateway.handle_cli_input("/exit")

    assert response is None
    assert outputs == []

    async with session_factory() as session:
        count = len((await session.scalars(select(Task))).all())
        assert count == 0


async def test_cli_gateway_keeps_running_after_normal_message_and_queue_command(
    workspace: Workspace,
    user: User,
) -> None:
    outputs: list[str] = []
    responses = [GatewayResponse(text="hello-response"), GatewayResponse(text="queue-response")]
    service = FakeGatewayService(responses=responses)
    inputs = iter(["hello", "/queue", "/exit"])
    gateway = CLIGateway(
        service=service,  # type: ignore[arg-type]
        workspace_id=workspace.id,
        user_id=user.id,
        input_reader=lambda prompt: next(inputs),
        output_writer=outputs.append,
    )

    await gateway.start()
    await gateway.wait_until_closed()

    assert service.received_texts == ["hello", "/queue"]
    assert outputs == [
        "Agent_Sam CLI gateway ready",
        "Chat normally for replies, use /new to queue work, or /exit to stop.",
        "hello-response",
        "queue-response",
    ]


async def test_cli_gateway_quit_command_stops_cleanly(
    workspace: Workspace,
    user: User,
) -> None:
    outputs: list[str] = []
    service = FakeGatewayService()
    inputs = iter(["/quit"])
    gateway = CLIGateway(
        service=service,  # type: ignore[arg-type]
        workspace_id=workspace.id,
        user_id=user.id,
        input_reader=lambda prompt: next(inputs),
        output_writer=outputs.append,
    )

    await gateway.start()
    await gateway.wait_until_closed()

    assert service.received_texts == []
    assert outputs == [
        "Agent_Sam CLI gateway ready",
        "Chat normally for replies, use /new to queue work, or /exit to stop.",
    ]


async def test_cli_gateway_surfaces_database_errors_clearly_and_stays_alive(
    workspace: Workspace,
    user: User,
) -> None:
    outputs: list[str] = []
    service = FakeGatewayService(error=OperationalError("SELECT 1", {}, Exception("db down")))
    inputs = iter(["hello", "/exit"])
    gateway = CLIGateway(
        service=service,  # type: ignore[arg-type]
        workspace_id=workspace.id,
        user_id=user.id,
        input_reader=lambda prompt: next(inputs),
        output_writer=outputs.append,
    )

    await gateway.start()
    await gateway.wait_until_closed()

    assert service.received_texts == ["hello"]
    assert outputs == [
        "Agent_Sam CLI gateway ready",
        "Chat normally for replies, use /new to queue work, or /exit to stop.",
        DATABASE_NOT_READY_MESSAGE,
    ]


async def test_cli_gateway_times_out_stalled_requests_and_stays_alive(
    workspace: Workspace,
    user: User,
) -> None:
    outputs: list[str] = []
    service = FakeGatewayService(delay_seconds=0.05)
    inputs = iter(["hello", "/exit"])
    gateway = CLIGateway(
        service=service,  # type: ignore[arg-type]
        workspace_id=workspace.id,
        user_id=user.id,
        input_reader=lambda prompt: next(inputs),
        output_writer=outputs.append,
        request_timeout_seconds=0.01,
    )

    await gateway.start()
    await gateway.wait_until_closed()

    assert service.received_texts == ["hello"]
    assert outputs == [
        "Agent_Sam CLI gateway ready",
        "Chat normally for replies, use /new to queue work, or /exit to stop.",
        DATABASE_NOT_READY_MESSAGE,
    ]