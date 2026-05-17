from __future__ import annotations

import logging
import re


TELEGRAM_BOT_URL_RE = re.compile(r"(https://api\.telegram\.org/)bot[^/\s]+", re.IGNORECASE)
TELEGRAM_TOKEN_ASSIGNMENT_RE = re.compile(r"(TELEGRAM_BOT_TOKEN\s*[=:]\s*)\S+", re.IGNORECASE)
TELEGRAM_CANCELLED_SHUTDOWN_MESSAGE = (
    "Fetching updates was aborted due to CancelledError(). Suppressing exception to ensure graceful shutdown."
)


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_sensitive_text(str(record.getMessage()))
        record.args = ()
        return True


class TelegramShutdownNoiseFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not (
            record.name == "telegram.ext.Application"
            and TELEGRAM_CANCELLED_SHUTDOWN_MESSAGE in str(record.getMessage())
        )


def configure_gateway_logging(*, debug_logging: bool) -> None:
    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        if not any(isinstance(existing_filter, SecretRedactionFilter) for existing_filter in handler.filters):
            handler.addFilter(SecretRedactionFilter())
        if not any(isinstance(existing_filter, TelegramShutdownNoiseFilter) for existing_filter in handler.filters):
            handler.addFilter(TelegramShutdownNoiseFilter())

    third_party_level = logging.DEBUG if debug_logging else logging.WARNING
    for logger_name in ("httpx", "httpcore", "telegram", "telegram.ext"):
        logging.getLogger(logger_name).setLevel(third_party_level)


def redact_sensitive_text(text: str) -> str:
    redacted = TELEGRAM_BOT_URL_RE.sub(r"\1bot<redacted>", text)
    return TELEGRAM_TOKEN_ASSIGNMENT_RE.sub(r"\1<redacted>", redacted)