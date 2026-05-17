from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

import pytest
from telegram.error import Conflict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from db.models import User, Workspace
from gateways.base import GatewayConfigurationError, GatewayResponse
from gateways.registry import build_gateway
from gateways.runtime_logging import SecretRedactionFilter, TelegramShutdownNoiseFilter, configure_gateway_logging
from gateways.telegram.bot import SUPPORTED_COMMANDS, TelegramGateway


class FakeGatewayService:
    async def handle_incoming_message(self, incoming) -> GatewayResponse:
        return GatewayResponse(text=f"echo:{incoming.text}")


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