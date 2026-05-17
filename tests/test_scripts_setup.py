from __future__ import annotations

import subprocess
from pathlib import Path

from scripts import setup as scripts_setup_module


def test_run_setup_local_mode_updates_seeded_ids_and_gateway_env(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("DATABASE_URL=postgresql+psycopg://example\nREDIS_URL=redis://localhost:6379/0\nQDRANT_URL=http://localhost:6333\n", encoding="utf-8")

    prompt_values = iter(["y", "cli", ""])
    outputs: list[str] = []

    def fake_command_runner(command, *, cwd, capture_output):
        command_text = " ".join(command)
        if command[:2] == ["docker", "version"]:
            return subprocess.CompletedProcess(command, 0, stdout="Docker ok", stderr="")
        if "scripts/seed_dev.py" in command_text:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "Development seed complete.\n"
                    "DEFAULT_USER_ID=d4b40f06-bf47-4794-b57e-973f5533e9a2\n"
                    "DEFAULT_WORKSPACE_ID=215ea9fb-71a7-4357-84e1-bf750febda77\n"
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    exit_code = scripts_setup_module.run_setup(
        mode="local",
        env_path=env_path,
        prompt=lambda text: next(prompt_values),
        secret_prompt=lambda text: "",
        output=outputs.append,
        command_runner=fake_command_runner,
    )

    updated_text = env_path.read_text(encoding="utf-8")

    assert exit_code == 0
    assert "DEFAULT_USER_ID=d4b40f06-bf47-4794-b57e-973f5533e9a2" in updated_text
    assert "DEFAULT_WORKSPACE_ID=215ea9fb-71a7-4357-84e1-bf750febda77" in updated_text
    assert "ENABLED_GATEWAYS=cli" in updated_text
    assert any("Agent_Sam Setup Wizard" in message for message in outputs)
    assert any("python -m scripts.doctor" in message for message in outputs)


def test_run_setup_cli_mode_reports_database_fix_when_unreachable(tmp_path: Path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("DATABASE_URL=postgresql+psycopg://example\nREDIS_URL=redis://localhost:6379/0\nQDRANT_URL=http://localhost:6333\n", encoding="utf-8")
    outputs: list[str] = []

    async def fake_database_is_reachable(settings) -> bool:
        return False

    monkeypatch.setattr(scripts_setup_module, "_database_is_reachable", fake_database_is_reachable)

    exit_code = scripts_setup_module.run_setup(
        mode="cli",
        env_path=env_path,
        prompt=lambda text: "",
        secret_prompt=lambda text: "",
        output=outputs.append,
        command_runner=lambda command, *, cwd, capture_output: subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )

    assert exit_code == 1
    assert any("Database is not ready. Run:" in message for message in outputs)
    assert any("python -m scripts.setup" in message for message in outputs)


def test_run_setup_linux_mode_prints_production_deployment_commands(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    outputs: list[str] = []

    exit_code = scripts_setup_module.run_setup(
        mode="linux",
        env_path=env_path,
        prompt=lambda text: "",
        secret_prompt=lambda text: "",
        output=outputs.append,
        command_runner=lambda command, *, cwd, capture_output: subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )

    assert exit_code == 0
    assert any("bash deployment/linux/install-host-assets.sh /opt/agent-sam" in message for message in outputs)
    assert any("python -m scripts.doctor --production" in message for message in outputs)
    assert any("sudo systemctl enable agent-api agent-worker agent-telegram" in message for message in outputs)


def test_run_setup_dry_run_previews_hermes_menu() -> None:
    outputs: list[str] = []

    exit_code = scripts_setup_module.run_setup(dry_run=True, output=outputs.append)

    assert exit_code == 0
    assert outputs[0] == "Agent_Sam Setup Wizard"
    assert any("Dry run menu preview:" in message for message in outputs)
    assert any("4. Configure model routing" in message for message in outputs)


def test_configure_llm_provider_hides_secret_output(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("LITELLM_MODEL=openai/gpt-4o-mini\n", encoding="utf-8")
    outputs: list[str] = []
    prompt_values = iter(["2"])
    wizard_io = scripts_setup_module.SetupIO(
        prompt=lambda text: next(prompt_values),
        secret_prompt=lambda text: "super-secret-key",
        output=outputs.append,
    )

    scripts_setup_module._configure_llm_provider(env_path, wizard_io, dry_run=False)

    env_text = env_path.read_text(encoding="utf-8")

    assert "OPENAI_API_KEY=super-secret-key" in env_text
    assert all("super-secret-key" not in message for message in outputs)