from pathlib import Path
from uuid import uuid4

from gateways.setup import disable_gateways, run_setup_wizard, show_enabled_gateways, test_gateway_config as run_gateway_config_test


def test_run_setup_wizard_updates_env_and_preserves_existing_values(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# existing comment\n"
        "API_PORT=9000\n"
        "DEFAULT_WORKSPACE_ID=workspace-123\n"
        "DEFAULT_USER_ID=user-456\n"
        "ENABLED_GATEWAYS=cli\n"
        "TELEGRAM_BOT_TOKEN=old-token\n",
        encoding="utf-8",
    )

    prompt_values = iter(["telegram, cli"])
    secret_values = iter(["new-telegram-token"])
    outputs: list[str] = []

    run_setup_wizard(
        env_path,
        prompt=lambda text: next(prompt_values),
        secret_prompt=lambda text: next(secret_values),
        output=outputs.append,
    )

    updated_text = env_path.read_text(encoding="utf-8")

    assert "API_PORT=9000" in updated_text
    assert "DEFAULT_WORKSPACE_ID=workspace-123" in updated_text
    assert "DEFAULT_USER_ID=user-456" in updated_text
    assert "ENABLED_GATEWAYS=telegram,cli" in updated_text
    assert "TELEGRAM_BOT_TOKEN=new-telegram-token" in updated_text
    assert outputs[:7] == [
        "Available gateways:",
        "- telegram",
        "- cli",
        "- whatsapp",
        "- discord",
        "- slack",
        "- webhook",
    ]
    assert "python -m gateways.runner" in outputs
    assert "python -m gateways.cli.main" in outputs
    assert "python -m gateways.telegram.main" in outputs
    assert all("new-telegram-token" not in message for message in outputs)


def test_run_setup_wizard_collects_whatsapp_twilio_values(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("DEFAULT_WORKSPACE_ID=workspace-123\nDEFAULT_USER_ID=user-456\n", encoding="utf-8")

    prompt_values = iter(["whatsapp", "twilio", "AC123456789", "whatsapp:+15551234567"])
    secret_values = iter(["twilio-auth-token"])

    run_setup_wizard(
        env_path,
        prompt=lambda text: next(prompt_values),
        secret_prompt=lambda text: next(secret_values),
        output=lambda text: None,
    )

    updated_text = env_path.read_text(encoding="utf-8")

    assert "ENABLED_GATEWAYS=whatsapp" in updated_text
    assert "WHATSAPP_PROVIDER=twilio" in updated_text
    assert "WHATSAPP_TWILIO_ACCOUNT_SID=AC123456789" in updated_text
    assert "WHATSAPP_TWILIO_AUTH_TOKEN=twilio-auth-token" in updated_text
    assert "WHATSAPP_TWILIO_FROM=whatsapp:+15551234567" in updated_text


def test_run_setup_wizard_collects_meta_discord_slack_and_webhook_values(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("DEFAULT_WORKSPACE_ID=workspace-123\nDEFAULT_USER_ID=user-456\n", encoding="utf-8")

    prompt_values = iter([
        "whatsapp, discord, slack, webhook",
        "meta",
        "phone-number-id-123",
    ])
    secret_values = iter([
        "meta-access-token",
        "meta-verify-token",
        "discord-token",
        "slack-bot-token",
        "slack-signing-secret",
        "webhook-shared-secret",
    ])
    outputs: list[str] = []

    run_setup_wizard(
        env_path,
        prompt=lambda text: next(prompt_values),
        secret_prompt=lambda text: next(secret_values),
        output=outputs.append,
    )

    updated_text = env_path.read_text(encoding="utf-8")

    assert "ENABLED_GATEWAYS=whatsapp,discord,slack,webhook" in updated_text
    assert "WHATSAPP_PROVIDER=meta" in updated_text
    assert "WHATSAPP_META_ACCESS_TOKEN=meta-access-token" in updated_text
    assert "WHATSAPP_META_PHONE_NUMBER_ID=phone-number-id-123" in updated_text
    assert "WHATSAPP_META_VERIFY_TOKEN=meta-verify-token" in updated_text
    assert "DISCORD_BOT_TOKEN=discord-token" in updated_text
    assert "SLACK_BOT_TOKEN=slack-bot-token" in updated_text
    assert "SLACK_SIGNING_SECRET=slack-signing-secret" in updated_text
    assert "WEBHOOK_GATEWAY_SECRET=webhook-shared-secret" in updated_text
    assert all("token" not in message.lower() or "Available gateways:" in message for message in outputs)


def test_show_enabled_gateways_lists_names_without_printing_secrets(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "ENABLED_GATEWAYS=telegram,cli\n"
        "TELEGRAM_BOT_TOKEN=super-secret-token\n",
        encoding="utf-8",
    )
    outputs: list[str] = []

    enabled_gateways = show_enabled_gateways(env_path, output=outputs.append)

    assert enabled_gateways == ("telegram", "cli")
    assert outputs == ["Enabled gateways:", "- telegram", "- cli"]
    assert all("super-secret-token" not in message for message in outputs)


def test_disable_gateways_updates_enabled_list_and_preserves_other_values(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "ENABLED_GATEWAYS=telegram,cli,webhook\n"
        "TELEGRAM_BOT_TOKEN=keep-me\n"
        "WEBHOOK_GATEWAY_SECRET=keep-webhook\n",
        encoding="utf-8",
    )

    remaining = disable_gateways(env_path, ("telegram", "webhook"), output=lambda text: None)
    updated_text = env_path.read_text(encoding="utf-8")

    assert remaining == ("cli",)
    assert "ENABLED_GATEWAYS=cli" in updated_text
    assert "TELEGRAM_BOT_TOKEN=keep-me" in updated_text
    assert "WEBHOOK_GATEWAY_SECRET=keep-webhook" in updated_text


def test_test_gateway_config_reports_ok_for_cli_and_fail_for_missing_token(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    workspace_id = str(uuid4())
    user_id = str(uuid4())
    env_path.write_text(
        "ENABLED_GATEWAYS=cli,telegram\n"
        f"DEFAULT_WORKSPACE_ID={workspace_id}\n"
        f"DEFAULT_USER_ID={user_id}\n",
        encoding="utf-8",
    )
    outputs: list[str] = []

    checks = run_gateway_config_test(env_path, output=outputs.append)

    assert len(checks) == 2
    assert any(check.name == "cli" and check.ok for check in checks)
    assert any(check.name == "telegram" and not check.ok for check in checks)
    assert any(message.startswith("[OK] cli:") for message in outputs)
    assert any("Run python -m gateways.setup" in message for message in outputs)
