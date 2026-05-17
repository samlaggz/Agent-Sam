from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from db.session import AsyncSessionLocal
from gateways.base import Gateway, GatewayConfigurationError
from gateways.service import AgentGatewayService


@dataclass(frozen=True)
class GatewayBuildContext:
    settings: Settings
    service: AgentGatewayService
    session_factory: async_sessionmaker[AsyncSession]
    workspace_id: UUID
    user_id: UUID


class GatewayRegistry:
    def __init__(self, gateways: dict[str, Gateway]) -> None:
        self._gateways = dict(gateways)

    @property
    def gateways(self) -> tuple[Gateway, ...]:
        return tuple(self._gateways.values())

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._gateways.keys())

    def get(self, name: str) -> Gateway:
        return self._gateways[name]


GatewayBuilder = Callable[[GatewayBuildContext], Gateway]


def build_enabled_gateway_registry(
    settings: Settings,
    *,
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
) -> GatewayRegistry:
    service = AgentGatewayService(session_factory)
    gateways: dict[str, Gateway] = {}
    for name in settings.enabled_gateways:
        gateways[name] = _build_gateway(name, settings, session_factory=session_factory, service=service)
    return GatewayRegistry(gateways)


def build_gateway(
    name: str,
    settings: Settings,
    *,
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
) -> Gateway:
    return _build_gateway(
        name,
        settings,
        session_factory=session_factory,
        service=AgentGatewayService(session_factory),
    )


def _build_gateway(
    name: str,
    settings: Settings,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    service: AgentGatewayService,
) -> Gateway:
    normalized_name = name.strip().lower()
    builder = _get_gateway_builders().get(normalized_name)
    if builder is None:
        supported = ", ".join(sorted(_get_gateway_builders().keys()))
        raise GatewayConfigurationError(
            f"Unsupported gateway '{normalized_name}'. Supported gateways: {supported}."
        )

    workspace_id, user_id = require_gateway_context(settings, normalized_name)
    context = GatewayBuildContext(
        settings=settings,
        service=service,
        session_factory=session_factory,
        workspace_id=workspace_id,
        user_id=user_id,
    )
    return builder(context)


def require_gateway_context(settings: Settings, gateway_name: str) -> tuple[UUID, UUID]:
    if settings.default_workspace_id is None:
        raise GatewayConfigurationError(
            f"{gateway_name} gateway requires DEFAULT_WORKSPACE_ID to be set."
        )
    if settings.default_user_id is None:
        raise GatewayConfigurationError(
            f"{gateway_name} gateway requires DEFAULT_USER_ID to be set."
        )
    return settings.default_workspace_id, settings.default_user_id


def _require_non_empty(setting_value: str, *, env_var_name: str, gateway_name: str) -> str:
    if setting_value.strip():
        return setting_value
    raise GatewayConfigurationError(
        f"{gateway_name} gateway requires {env_var_name} to be set."
    )


def _build_telegram_gateway(context: GatewayBuildContext) -> Gateway:
    from gateways.telegram.bot import TelegramGateway

    token = context.settings.telegram_bot_token.strip()
    if not token:
        raise GatewayConfigurationError("TELEGRAM_BOT_TOKEN is missing. Run python -m gateways.setup")
    return TelegramGateway(
        service=context.service,
        token=token,
        workspace_id=context.workspace_id,
        user_id=context.user_id,
        debug_logging=context.settings.gateway_debug_logging,
    )


def _build_cli_gateway(context: GatewayBuildContext) -> Gateway:
    from gateways.cli.gateway import CLIGateway

    return CLIGateway(
        service=context.service,
        workspace_id=context.workspace_id,
        user_id=context.user_id,
        debug_logging=context.settings.gateway_debug_logging,
    )


def _build_whatsapp_gateway(context: GatewayBuildContext) -> Gateway:
    from gateways.whatsapp.gateway import WhatsAppGateway

    provider = context.settings.whatsapp_provider
    if provider == "twilio":
        _require_non_empty(
            context.settings.whatsapp_twilio_account_sid,
            env_var_name="WHATSAPP_TWILIO_ACCOUNT_SID",
            gateway_name="whatsapp",
        )
        _require_non_empty(
            context.settings.whatsapp_twilio_auth_token,
            env_var_name="WHATSAPP_TWILIO_AUTH_TOKEN",
            gateway_name="whatsapp",
        )
        _require_non_empty(
            context.settings.whatsapp_twilio_from,
            env_var_name="WHATSAPP_TWILIO_FROM",
            gateway_name="whatsapp",
        )
    elif provider == "meta":
        _require_non_empty(
            context.settings.whatsapp_meta_access_token,
            env_var_name="WHATSAPP_META_ACCESS_TOKEN",
            gateway_name="whatsapp",
        )
        _require_non_empty(
            context.settings.whatsapp_meta_phone_number_id,
            env_var_name="WHATSAPP_META_PHONE_NUMBER_ID",
            gateway_name="whatsapp",
        )
        _require_non_empty(
            context.settings.whatsapp_meta_verify_token,
            env_var_name="WHATSAPP_META_VERIFY_TOKEN",
            gateway_name="whatsapp",
        )
    else:
        raise GatewayConfigurationError(
            "whatsapp gateway requires WHATSAPP_PROVIDER to be either 'twilio' or 'meta'."
        )

    return WhatsAppGateway(provider=provider)


def _build_discord_gateway(context: GatewayBuildContext) -> Gateway:
    from gateways.discord.gateway import DiscordGateway

    _require_non_empty(
        context.settings.discord_bot_token,
        env_var_name="DISCORD_BOT_TOKEN",
        gateway_name="discord",
    )
    return DiscordGateway()


def _build_slack_gateway(context: GatewayBuildContext) -> Gateway:
    from gateways.slack.gateway import SlackGateway

    _require_non_empty(
        context.settings.slack_bot_token,
        env_var_name="SLACK_BOT_TOKEN",
        gateway_name="slack",
    )
    _require_non_empty(
        context.settings.slack_signing_secret,
        env_var_name="SLACK_SIGNING_SECRET",
        gateway_name="slack",
    )
    return SlackGateway()


def _build_webhook_gateway(context: GatewayBuildContext) -> Gateway:
    from gateways.webhook.gateway import WebhookGateway

    _require_non_empty(
        context.settings.webhook_gateway_secret,
        env_var_name="WEBHOOK_GATEWAY_SECRET",
        gateway_name="webhook",
    )
    return WebhookGateway()


def _get_gateway_builders() -> dict[str, GatewayBuilder]:
    return {
        "telegram": _build_telegram_gateway,
        "whatsapp": _build_whatsapp_gateway,
        "discord": _build_discord_gateway,
        "slack": _build_slack_gateway,
        "cli": _build_cli_gateway,
        "webhook": _build_webhook_gateway,
    }