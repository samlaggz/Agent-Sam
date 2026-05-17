from __future__ import annotations

import argparse
import ast
import getpass
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.config import Settings
from gateways.base import GatewayConfigurationError
from gateways.registry import build_enabled_gateway_registry
from gateways.runtime_logging import redact_sensitive_text
from scripts.env_writer import ensure_env_file as ensure_env_document
from scripts.env_writer import load_env_values as load_env_document_values
from scripts.env_writer import update_env_file


AVAILABLE_GATEWAYS = (
    "telegram",
    "cli",
    "whatsapp",
    "discord",
    "slack",
    "webhook",
)

ENV_LINE_RE = re.compile(r"^(?P<prefix>\s*(?:export\s+)?)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=.*$")
SAFE_ENV_VALUE_RE = re.compile(r"^[A-Za-z0-9_./:@,+\-]+$")

PromptFunc = Callable[[str], str]
OutputFunc = Callable[[str], None]


@dataclass(frozen=True)
class WizardIO:
    prompt: PromptFunc
    secret_prompt: PromptFunc
    output: OutputFunc


@dataclass(frozen=True)
class GatewayConfigCheck:
    name: str
    ok: bool
    detail: str


def run() -> None:
    parser = argparse.ArgumentParser(description="Configure enabled gateways and secrets in .env.")
    parser.add_argument(
        "--env-path",
        type=Path,
        default=None,
        help="Optional path to the .env file to update. Defaults to the repository root .env.",
    )
    parser.add_argument("--show-enabled", action="store_true", help="Show enabled gateways without printing secrets.")
    parser.add_argument(
        "--disable",
        default="",
        help="Comma-separated gateway names to disable from ENABLED_GATEWAYS.",
    )
    parser.add_argument("--test-config", action="store_true", help="Validate the current gateway configuration.")
    arguments = parser.parse_args()
    env_path = arguments.env_path or _default_env_path()

    if arguments.show_enabled:
        show_enabled_gateways(env_path)

    if arguments.disable.strip():
        disable_gateways(env_path, _normalize_gateway_names(arguments.disable))

    if arguments.test_config:
        test_gateway_config(env_path)

    if not any((arguments.show_enabled, arguments.disable.strip(), arguments.test_config)):
        run_setup_wizard(env_path)


def run_setup_wizard(
    env_path: Path | None = None,
    *,
    prompt: PromptFunc = input,
    secret_prompt: PromptFunc = getpass.getpass,
    output: OutputFunc = print,
) -> Path:
    wizard_io = WizardIO(prompt=prompt, secret_prompt=secret_prompt, output=output)
    resolved_env_path = env_path or _default_env_path()
    env_lines = _load_env_lines(resolved_env_path)
    env_values = _parse_env_values(env_lines)

    _print_available_gateways(wizard_io)
    enabled_gateways = _prompt_enabled_gateways(wizard_io, env_values)

    updates = _collect_gateway_updates(enabled_gateways, env_values, wizard_io)
    updates["ENABLED_GATEWAYS"] = ",".join(enabled_gateways)

    _write_env_updates(resolved_env_path, env_lines, updates)

    wizard_io.output(f"Updated {resolved_env_path.name}.")
    wizard_io.output("Next commands:")
    wizard_io.output("python -m gateways.runner")
    wizard_io.output("python -m gateways.cli.main")
    wizard_io.output("python -m gateways.telegram.main")
    return resolved_env_path


def ensure_env_file(env_path: Path | None = None) -> tuple[Path, bool]:
    resolved_env_path = env_path or _default_env_path()
    return ensure_env_document(resolved_env_path)


def load_env_values(env_path: Path | None = None) -> dict[str, str]:
    resolved_env_path = env_path or _default_env_path()
    return load_env_document_values(resolved_env_path)


def update_env_values(env_path: Path | None, updates: dict[str, str]) -> Path:
    resolved_env_path = env_path or _default_env_path()
    update_env_file(resolved_env_path, updates)
    return resolved_env_path


def show_enabled_gateways(env_path: Path | None = None, *, output: OutputFunc = print) -> tuple[str, ...]:
    enabled_gateways = _normalize_gateway_names(load_env_values(env_path).get("ENABLED_GATEWAYS", ""))
    if enabled_gateways:
        output("Enabled gateways:")
        for gateway_name in enabled_gateways:
            output(f"- {gateway_name}")
    else:
        output("Enabled gateways: none")
    return enabled_gateways


def disable_gateways(
    env_path: Path | None,
    gateways_to_disable: tuple[str, ...],
    *,
    output: OutputFunc = print,
) -> tuple[str, ...]:
    resolved_env_path, _ = ensure_env_file(env_path)
    env_values = load_env_values(resolved_env_path)
    current_gateways = _normalize_gateway_names(env_values.get("ENABLED_GATEWAYS", ""))
    disabled_set = set(gateways_to_disable)
    remaining_gateways = tuple(name for name in current_gateways if name not in disabled_set)
    update_env_values(resolved_env_path, {"ENABLED_GATEWAYS": ",".join(remaining_gateways)})
    if gateways_to_disable:
        output("Disabled gateways: " + ", ".join(gateways_to_disable))
    show_enabled_gateways(resolved_env_path, output=output)
    return remaining_gateways


def test_gateway_config(env_path: Path | None = None, *, output: OutputFunc = print) -> tuple[GatewayConfigCheck, ...]:
    resolved_env_path, _ = ensure_env_file(env_path)
    checks = _build_gateway_config_checks(resolved_env_path)
    for check in checks:
        prefix = "[OK]" if check.ok else "[FAIL]"
        output(f"{prefix} {check.name}: {check.detail}")
    if not checks:
        output("[FAIL] enabled_gateways: No gateways are enabled.")
    return checks


def _default_env_path() -> Path:
    return Path(__file__).resolve().parent.parent / ".env"


def _load_env_lines(env_path: Path) -> list[str]:
    if env_path.exists():
        return env_path.read_text(encoding="utf-8").splitlines(keepends=True)

    example_path = env_path.parent / ".env.example"
    if example_path.exists():
        return example_path.read_text(encoding="utf-8").splitlines(keepends=True)

    return []


def _parse_env_values(lines: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in lines:
        match = ENV_LINE_RE.match(line)
        if match is None:
            continue
        key = match.group("key")
        raw_value = line.split("=", maxsplit=1)[1].strip()
        values[key] = _decode_env_value(raw_value)
    return values


def _decode_env_value(raw_value: str) -> str:
    stripped = raw_value.strip()
    if not stripped:
        return ""
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
        try:
            decoded = ast.literal_eval(stripped)
        except (SyntaxError, ValueError):
            return stripped[1:-1]
        if isinstance(decoded, str):
            return decoded
    return stripped


def _print_available_gateways(wizard_io: WizardIO) -> None:
    wizard_io.output("Available gateways:")
    for gateway_name in AVAILABLE_GATEWAYS:
        wizard_io.output(f"- {gateway_name}")


def _prompt_enabled_gateways(wizard_io: WizardIO, env_values: dict[str, str]) -> tuple[str, ...]:
    current_value = env_values.get("ENABLED_GATEWAYS", "")
    current_enabled = _normalize_gateway_names(current_value)
    current_text = ", ".join(current_enabled) if current_enabled else "none"

    while True:
        raw_value = wizard_io.prompt(
            f"Which gateways should be enabled? (comma-separated, current: {current_text}) "
        )
        if not raw_value.strip() and current_enabled:
            return current_enabled

        enabled_gateways = _normalize_gateway_names(raw_value)
        invalid_gateways = [name for name in enabled_gateways if name not in AVAILABLE_GATEWAYS]
        if invalid_gateways:
            wizard_io.output(
                "Unknown gateway names: "
                + ", ".join(invalid_gateways)
                + ". Choose from: "
                + ", ".join(AVAILABLE_GATEWAYS)
            )
            continue
        if not enabled_gateways:
            wizard_io.output("Select at least one gateway.")
            continue
        return enabled_gateways


def _normalize_gateway_names(raw_value: str) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for token in re.split(r"[\s,]+", raw_value.strip()):
        name = token.strip().lower()
        if not name or name in seen:
            continue
        normalized.append(name)
        seen.add(name)
    return tuple(normalized)


def _collect_gateway_updates(
    enabled_gateways: tuple[str, ...],
    env_values: dict[str, str],
    wizard_io: WizardIO,
) -> dict[str, str]:
    updates: dict[str, str] = {}

    if "telegram" in enabled_gateways:
        updates["TELEGRAM_BOT_TOKEN"] = _prompt_value(
            wizard_io,
            env_var_name="TELEGRAM_BOT_TOKEN",
            env_values=env_values,
            secret=True,
        )

    if "whatsapp" in enabled_gateways:
        provider = _prompt_whatsapp_provider(wizard_io, env_values)
        updates["WHATSAPP_PROVIDER"] = provider
        if provider == "twilio":
            updates["WHATSAPP_TWILIO_ACCOUNT_SID"] = _prompt_value(
                wizard_io,
                env_var_name="WHATSAPP_TWILIO_ACCOUNT_SID",
                env_values=env_values,
                secret=False,
            )
            updates["WHATSAPP_TWILIO_AUTH_TOKEN"] = _prompt_value(
                wizard_io,
                env_var_name="WHATSAPP_TWILIO_AUTH_TOKEN",
                env_values=env_values,
                secret=True,
            )
            updates["WHATSAPP_TWILIO_FROM"] = _prompt_value(
                wizard_io,
                env_var_name="WHATSAPP_TWILIO_FROM",
                env_values=env_values,
                secret=False,
            )
        else:
            updates["WHATSAPP_META_ACCESS_TOKEN"] = _prompt_value(
                wizard_io,
                env_var_name="WHATSAPP_META_ACCESS_TOKEN",
                env_values=env_values,
                secret=True,
            )
            updates["WHATSAPP_META_PHONE_NUMBER_ID"] = _prompt_value(
                wizard_io,
                env_var_name="WHATSAPP_META_PHONE_NUMBER_ID",
                env_values=env_values,
                secret=False,
            )
            updates["WHATSAPP_META_VERIFY_TOKEN"] = _prompt_value(
                wizard_io,
                env_var_name="WHATSAPP_META_VERIFY_TOKEN",
                env_values=env_values,
                secret=True,
            )

    if "discord" in enabled_gateways:
        updates["DISCORD_BOT_TOKEN"] = _prompt_value(
            wizard_io,
            env_var_name="DISCORD_BOT_TOKEN",
            env_values=env_values,
            secret=True,
        )

    if "slack" in enabled_gateways:
        updates["SLACK_BOT_TOKEN"] = _prompt_value(
            wizard_io,
            env_var_name="SLACK_BOT_TOKEN",
            env_values=env_values,
            secret=True,
        )
        updates["SLACK_SIGNING_SECRET"] = _prompt_value(
            wizard_io,
            env_var_name="SLACK_SIGNING_SECRET",
            env_values=env_values,
            secret=True,
        )

    if "webhook" in enabled_gateways:
        updates["WEBHOOK_GATEWAY_SECRET"] = _prompt_value(
            wizard_io,
            env_var_name="WEBHOOK_GATEWAY_SECRET",
            env_values=env_values,
            secret=True,
        )

    return updates


def _build_gateway_config_checks(env_path: Path) -> tuple[GatewayConfigCheck, ...]:
    try:
        settings = Settings(_env_file=env_path)
    except Exception as exc:
        return (GatewayConfigCheck(name="settings", ok=False, detail=redact_sensitive_text(str(exc))),)

    if not settings.enabled_gateways:
        return ()

    checks: list[GatewayConfigCheck] = []
    for gateway_name in settings.enabled_gateways:
        try:
            build_enabled_gateway_registry(Settings(_env_file=env_path, enabled_gateways=(gateway_name,)))
        except (GatewayConfigurationError, Exception) as exc:
            checks.append(
                GatewayConfigCheck(
                    name=gateway_name,
                    ok=False,
                    detail=redact_sensitive_text(str(exc)),
                )
            )
        else:
            checks.append(GatewayConfigCheck(name=gateway_name, ok=True, detail="configuration looks valid"))
    return tuple(checks)


def _prompt_whatsapp_provider(wizard_io: WizardIO, env_values: dict[str, str]) -> str:
    current_provider = env_values.get("WHATSAPP_PROVIDER", "twilio") or "twilio"
    while True:
        provider = wizard_io.prompt(
            f"WhatsApp provider (twilio/meta, current: {current_provider}) "
        ).strip().lower()
        if not provider:
            provider = current_provider
        if provider in {"twilio", "meta"}:
            return provider
        wizard_io.output("Choose either 'twilio' or 'meta'.")


def _prompt_value(
    wizard_io: WizardIO,
    *,
    env_var_name: str,
    env_values: dict[str, str],
    secret: bool,
) -> str:
    existing_value = env_values.get(env_var_name, "")
    prompt_suffix = " [press Enter to keep existing]" if existing_value else ""
    prompt_text = f"{env_var_name}{prompt_suffix}: "
    prompt_func = wizard_io.secret_prompt if secret else wizard_io.prompt

    while True:
        value = prompt_func(prompt_text).strip()
        if value:
            return value
        if existing_value:
            return existing_value
        wizard_io.output(f"{env_var_name} is required.")


def _write_env_updates(env_path: Path, lines: list[str], updates: dict[str, str]) -> None:
    del lines
    update_env_file(env_path, updates)


def _format_env_value(value: str) -> str:
    if value == "":
        return ""
    if SAFE_ENV_VALUE_RE.fullmatch(value):
        return value
    escaped_value = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped_value}"'


if __name__ == "__main__":
    run()