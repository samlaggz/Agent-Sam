import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from db.models import User, Workspace
from gateways.base import GatewayConfigurationError
from gateways.cli.gateway import CLIGateway
from gateways.registry import build_enabled_gateway_registry, build_gateway
from gateways.telegram.bot import TelegramGateway


pytestmark = pytest.mark.asyncio


def build_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "enabled_gateways": (),
        "gateway_debug_logging": False,
        "telegram_bot_token": "",
        "whatsapp_provider": "twilio",
        "whatsapp_twilio_account_sid": "",
        "whatsapp_twilio_auth_token": "",
        "whatsapp_twilio_from": "",
        "whatsapp_meta_access_token": "",
        "whatsapp_meta_phone_number_id": "",
        "whatsapp_meta_verify_token": "",
        "discord_bot_token": "",
        "slack_bot_token": "",
        "slack_signing_secret": "",
        "webhook_gateway_secret": "",
        "default_workspace_id": None,
        "default_user_id": None,
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


async def test_gateway_registry_loads_enabled_gateways(
    session_factory: async_sessionmaker[AsyncSession],
    workspace: Workspace,
    user: User,
) -> None:
    settings = build_settings(
        enabled_gateways=("telegram", "cli"),
        telegram_bot_token="test-token",
        default_workspace_id=workspace.id,
        default_user_id=user.id,
    )

    registry = build_enabled_gateway_registry(settings, session_factory=session_factory)

    assert registry.names == ("telegram", "cli")
    assert isinstance(registry.get("telegram"), TelegramGateway)
    assert isinstance(registry.get("cli"), CLIGateway)


async def test_settings_parse_enabled_gateways_from_env_string(monkeypatch) -> None:
    monkeypatch.setenv("ENABLED_GATEWAYS", "telegram,cli")

    settings = Settings(_env_file=None)

    assert settings.enabled_gateways == ("telegram", "cli")


async def test_telegram_gateway_requires_token(
    session_factory: async_sessionmaker[AsyncSession],
    workspace: Workspace,
    user: User,
) -> None:
    settings = build_settings(
        default_workspace_id=workspace.id,
        default_user_id=user.id,
    )

    with pytest.raises(GatewayConfigurationError, match="Run python -m gateways.setup"):
        build_gateway("telegram", settings, session_factory=session_factory)


@pytest.mark.parametrize(
    ("gateway_name", "settings_overrides", "expected_message"),
    [
        ("whatsapp", {}, "WHATSAPP_TWILIO_ACCOUNT_SID"),
        ("discord", {}, "DISCORD_BOT_TOKEN"),
        ("slack", {"slack_bot_token": "xoxb-test"}, "SLACK_SIGNING_SECRET"),
        ("webhook", {}, "WEBHOOK_GATEWAY_SECRET"),
    ],
)
async def test_placeholder_gateways_validate_configuration(
    gateway_name: str,
    settings_overrides: dict[str, object],
    expected_message: str,
    session_factory: async_sessionmaker[AsyncSession],
    workspace: Workspace,
    user: User,
) -> None:
    settings = build_settings(
        enabled_gateways=(gateway_name,),
        default_workspace_id=workspace.id,
        default_user_id=user.id,
        **settings_overrides,
    )

    with pytest.raises(GatewayConfigurationError, match=expected_message):
        build_enabled_gateway_registry(settings, session_factory=session_factory)