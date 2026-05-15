import logging

from app.config import get_settings
from gateways.telegram.bot import build_application


def run() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN must be set before starting the Telegram gateway.")
    if settings.default_workspace_id is None:
        raise RuntimeError("DEFAULT_WORKSPACE_ID must be set before starting the Telegram gateway.")
    if settings.default_user_id is None:
        raise RuntimeError("DEFAULT_USER_ID must be set before starting the Telegram gateway.")

    logging.basicConfig(level=logging.INFO)
    application = build_application(settings)
    # TODO: support webhook delivery for hardened production deployments.
    application.run_polling()
