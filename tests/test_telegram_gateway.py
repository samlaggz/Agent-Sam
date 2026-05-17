from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

import pytest
from telegram.error import Conflict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from db.models import User, Workspace
from agent.progress import TelegramProgressAdapter
from gateways.base import GatewayConfigurationError, GatewayResponse
from gateways.registry import build_gateway
from gateways.runtime_logging import SecretRedactionFilter, TelegramShutdownNoiseFilter, configure_gateway_logging
from gateways.telegram.bot import SUPPORTED_COMMANDS, TelegramGateway


class FakeGatewayService:
    async def handle_incoming_message(self, incoming) -> GatewayResponse:
        return GatewayResponse(text=f"echo:{incoming.text}")


class FakeApprovalGatewayService:
    def __init__(self) -> None:
        self.received_texts: list[str] = []

    async def handle_incoming_message(self, incoming) -> GatewayResponse:
        self.received_texts.append(incoming.text)
        if incoming.text.startswith("/approve "):
            approval_id = incoming.text.split(" ", 1)[1]
            return GatewayResponse(text=f"Approval {approval_id} marked as approved.", approval_id=uuid4(), metadata={"approval_id": approval_id})
        return GatewayResponse(text="Needs approval.", approval_id=uuid4(), metadata={"approval_id": "approval-123"})


def build_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "enabled_gateways": ("telegram",),
        "gateway_debug_logging": False,
        "telegram_bot_token": "",
        "default_workspace_id": uuid4(),
        "default_user_id": uuid4(),
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


async def test_telegram_gateway_missing_token_raises_clear_error(
    session_factory: async_sessionmaker[AsyncSession],
    workspace: Workspace,
    user: User,
) -> None:
    settings = build_settings(default_workspace_id=workspace.id, default_user_id=user.id)

    with pytest.raises(GatewayConfigurationError, match="Run python -m gateways.setup"):
        build_gateway("telegram", settings, session_factory=session_factory)


async def test_telegram_gateway_registers_all_expected_command_handlers() -> None:
    gateway = TelegramGateway(
        service=FakeGatewayService(),  # type: ignore[arg-type]
        token="123456:ABCDEF_test_token",
        workspace_id=uuid4(),
        user_id=uuid4(),
    )

    handlers = gateway._application.handlers[0]
    registered_commands = {
        command_name
        for handler in handlers
        if hasattr(handler, "commands")
        for command_name in handler.commands
    }

    assert set(SUPPORTED_COMMANDS) <= registered_commands
    assert any(handler.__class__.__name__ == "MessageHandler" for handler in handlers)


async def test_telegram_gateway_stop_suppresses_cancelled_error() -> None:
    gateway = TelegramGateway(
        service=FakeGatewayService(),  # type: ignore[arg-type]
        token="123456:ABCDEF_test_token",
        workspace_id=uuid4(),
        user_id=uuid4(),
    )

    class FakeUpdater:
        async def stop(self) -> None:
            raise asyncio.CancelledError

    class FakeApplication:
        def __init__(self) -> None:
            self.updater = FakeUpdater()

        async def stop(self) -> None:
            raise asyncio.CancelledError

        async def shutdown(self) -> None:
            raise asyncio.CancelledError

    gateway._application = FakeApplication()  # type: ignore[assignment]
    gateway._started = True

    await gateway.stop()

    assert gateway._started is False


async def test_telegram_gateway_conflict_stops_cleanly_once(caplog: pytest.LogCaptureFixture) -> None:
    gateway = TelegramGateway(
        service=FakeGatewayService(),  # type: ignore[arg-type]
        token="123456:ABCDEF_test_token",
        workspace_id=uuid4(),
        user_id=uuid4(),
    )

    class FakeUpdater:
        def __init__(self) -> None:
            self.stop_calls = 0

        async def start_polling(self, *, error_callback=None) -> None:
            assert error_callback is not None
            error_callback(Conflict("Conflict: terminated by other getUpdates request"))
            error_callback(Conflict("Conflict: terminated by other getUpdates request"))

        async def stop(self) -> None:
            self.stop_calls += 1

    class FakeApplication:
        def __init__(self) -> None:
            self.updater = FakeUpdater()
            self.stop_calls = 0
            self.shutdown_calls = 0

        async def initialize(self) -> None:
            return None

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            self.stop_calls += 1

        async def shutdown(self) -> None:
            self.shutdown_calls += 1

    fake_application = FakeApplication()
    gateway._application = fake_application  # type: ignore[assignment]
    caplog.set_level(logging.ERROR)

    await gateway.start()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert gateway._started is False
    assert fake_application.updater.stop_calls == 1
    assert fake_application.stop_calls == 1
    assert fake_application.shutdown_calls == 1
    assert caplog.text.count("Telegram polling conflict:") == 1


def test_secret_redaction_filter_hides_telegram_tokens(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("httpx.test")
    configure_gateway_logging(debug_logging=False)
    caplog.set_level(logging.INFO, logger="httpx.test")

    logger.info("POST https://api.telegram.org/bot123456:ABCDEF_super_secret/getMe")
    logger.info("TELEGRAM_BOT_TOKEN=123456:ABCDEF_super_secret")

    rendered = "\n".join(caplog.messages)
    assert "ABCDEF_super_secret" not in rendered
    assert "bot<redacted>" in rendered
    assert "TELEGRAM_BOT_TOKEN=<redacted>" in rendered


def test_secret_redaction_filter_mutates_record_message() -> None:
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="POST https://api.telegram.org/bot123456:ABCDEF_super_secret/getMe",
        args=(),
        exc_info=None,
    )

    assert SecretRedactionFilter().filter(record) is True
    assert "ABCDEF_super_secret" not in record.msg
    assert "bot<redacted>" in record.msg


def test_telegram_shutdown_noise_filter_drops_known_cancelled_error_message() -> None:
    record = logging.LogRecord(
        name="telegram.ext.Application",
        level=logging.CRITICAL,
        pathname=__file__,
        lineno=1,
        msg="Fetching updates was aborted due to CancelledError(). Suppressing exception to ensure graceful shutdown.",
        args=(),
        exc_info=None,
    )

    assert TelegramShutdownNoiseFilter().filter(record) is False


async def test_telegram_gateway_builds_approval_button_markup() -> None:
    gateway = TelegramGateway(
        service=FakeGatewayService(),  # type: ignore[arg-type]
        token="123456:ABCDEF_test_token",
        workspace_id=uuid4(),
        user_id=uuid4(),
    )

    markup = gateway._build_reply_markup(GatewayResponse(text="Approve this", metadata={"approval_id": "approval-123"}))

    assert markup is not None
    assert markup.inline_keyboard[0][0].callback_data == "approve:approval-123"


async def test_telegram_gateway_callback_query_sends_approve_command() -> None:
    service = FakeApprovalGatewayService()
    gateway = TelegramGateway(
        service=service,  # type: ignore[arg-type]
        token="123456:ABCDEF_test_token",
        workspace_id=uuid4(),
        user_id=uuid4(),
    )

    class FakeMessage:
        def __init__(self) -> None:
            self.message_id = 77
            self.replies: list[str] = []

        async def reply_text(self, text: str, reply_markup=None) -> None:
            del reply_markup
            self.replies.append(text)

    class FakeCallbackQuery:
        def __init__(self) -> None:
            self.data = "approve:approval-123"
            self.message = FakeMessage()
            self.answered: list[str] = []

        async def answer(self, text: str, show_alert: bool = False) -> None:
            del show_alert
            self.answered.append(text)

        async def edit_message_reply_markup(self, reply_markup=None) -> None:
            del reply_markup

    class FakeUser:
        id = 999
        username = "tester"
        full_name = "Telegram Tester"

    class FakeChat:
        id = 555
        title = "Test Chat"
        type = "private"

    class FakeUpdate:
        def __init__(self) -> None:
            self.callback_query = FakeCallbackQuery()
            self.effective_chat = FakeChat()
            self.effective_user = FakeUser()

    update = FakeUpdate()

    await gateway._handle_callback_query(update, None)  # type: ignore[arg-type]

    assert service.received_texts == ["/approve approval-123"]
    assert update.callback_query.answered == ["Approved"]
    assert update.callback_query.message.replies


@pytest.mark.asyncio
async def test_telegram_progress_adapter_only_sends_final_or_approval_stages(monkeypatch) -> None:
    settings = Settings(_env_file=None, telegram_bot_token="token")
    adapter = TelegramProgressAdapter(settings)
    sent_messages: list[tuple[str, str, object]] = []

    class FakeBot:
        def __init__(self, token: str) -> None:
            self.token = token

        async def send_message(self, chat_id: str, text: str, reply_markup=None) -> None:
            sent_messages.append((chat_id, text, reply_markup))

    monkeypatch.setattr("telegram.Bot", FakeBot)

    class FakeTask:
        metadata_json = {"source": "telegram", "source_chat_id": "123"}

    await adapter.send(FakeTask(), "step update", stage="step_summary")
    await adapter.send(FakeTask(), "needs approval\nApproval ID: 11111111-1111-1111-1111-111111111111", stage="approval_required")
    await adapter.send(FakeTask(), "final result", stage="report_result")

    assert sent_messages[0][0] == "123"
    assert "Approval ID:" in sent_messages[0][1]
    assert sent_messages[0][2] is not None
    assert sent_messages[1] == ("123", "final result", None)