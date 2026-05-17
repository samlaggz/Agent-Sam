from __future__ import annotations

import subprocess
from pathlib import Path

from scripts import install as install_module


def test_run_install_production_dry_run_prints_actions_without_touching_target(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".env.example").write_text(
        "APP_ENV=production\n"
        "ENABLED_GATEWAYS=telegram\n"
        "LITELLM_MODEL=openai/gpt-4o-mini\n",
        encoding="utf-8",
    )
    target = tmp_path / "target"
    outputs: list[str] = []

    exit_code = install_module.run_install(
        install_module.InstallOptions(
            target=target,
            dry_run=True,
            non_interactive=True,
            skip_nginx=False,
            skip_start=False,
            local=False,
            production=True,
        ),
        repo_root=repo_root,
        output=outputs.append,
        command_runner=lambda command, **kwargs: (_ for _ in ()).throw(AssertionError("dry run should not execute commands")),
    )

    assert exit_code == 0
    assert not target.exists()
    assert any("install-host-assets.sh" in message for message in outputs)
    assert any("Dry run: would ensure" in message for message in outputs)
    assert any("Dry run: would configure LLM provider" in message for message in outputs)


def test_run_install_non_interactive_requires_llm_secret(tmp_path: Path, monkeypatch) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".env.example").write_text(
        "APP_ENV=production\n"
        "DATABASE_URL=postgresql+psycopg://agent_sam_user:CHANGE_ME@127.0.0.1:5432/agent_sam\n"
        "REDIS_URL=redis://127.0.0.1:6379/0\n"
        "QDRANT_URL=http://127.0.0.1:6333\n"
        "ENABLED_GATEWAYS=telegram\n"
        "LITELLM_MODEL=openai/gpt-4o-mini\n",
        encoding="utf-8",
    )
    target = tmp_path / "target"
    target.mkdir()
    outputs: list[str] = []
    commands: list[list[str]] = []

    monkeypatch.setattr(install_module, "detect_os_name", lambda: "Linux")
    monkeypatch.setattr(install_module, "python_version_text", lambda: "3.11.9")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://agent_sam_user:real@127.0.0.1:5432/agent_sam")

    exit_code = install_module.run_install(
        install_module.InstallOptions(
            target=target,
            dry_run=False,
            non_interactive=True,
            skip_nginx=True,
            skip_start=True,
            local=False,
            production=True,
        ),
        repo_root=repo_root,
        output=outputs.append,
        command_runner=lambda command, **kwargs: commands.append(list(command)) or subprocess.CompletedProcess(command, 0, stdout="ok", stderr=""),
    )

    assert exit_code == 1
    assert commands[:2]
    assert not (target / ".env").exists()


def test_run_install_interactive_writes_env_without_printing_secret(tmp_path: Path, monkeypatch) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / ".env.example").write_text(
        "APP_ENV=production\n"
        "DATABASE_URL=postgresql+psycopg://agent_sam_user:CHANGE_ME@127.0.0.1:5432/agent_sam\n"
        "REDIS_URL=redis://127.0.0.1:6379/0\n"
        "QDRANT_URL=http://127.0.0.1:6333\n"
        "ENABLED_GATEWAYS=telegram\n"
        "LITELLM_MODEL=openai/gpt-4o-mini\n",
        encoding="utf-8",
    )
    target = tmp_path / "target"
    target.mkdir()
    outputs: list[str] = []
    prompt_values = iter(
        [
            "",
            "",
            "",
            "postgresql+psycopg://agent_sam_user:real@127.0.0.1:5432/agent_sam",
            "redis://127.0.0.1:6379/0",
            "http://127.0.0.1:6333",
            "telegram",
            "1",
            "",
        ]
        + [""] * 26
    )
    secret_values = iter(["telegram-secret", "openrouter-secret"])

    monkeypatch.setattr(install_module, "detect_os_name", lambda: "Linux")
    monkeypatch.setattr(install_module, "python_version_text", lambda: "3.11.9")

    exit_code = install_module.run_install(
        install_module.InstallOptions(
            target=target,
            dry_run=False,
            non_interactive=False,
            skip_nginx=True,
            skip_start=True,
            local=False,
            production=True,
        ),
        repo_root=repo_root,
        prompt=lambda text: next(prompt_values),
        secret_prompt=lambda text: next(secret_values),
        output=outputs.append,
        command_runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout="ok", stderr=""),
    )

    env_text = (target / ".env").read_text(encoding="utf-8")

    assert exit_code == 0
    assert "TELEGRAM_BOT_TOKEN=telegram-secret" in env_text
    assert "OPENROUTER_API_KEY=openrouter-secret" in env_text
    assert "LITELLM_MODEL=openrouter/openai/gpt-4.1-mini" in env_text
    assert all("telegram-secret" not in message for message in outputs)
    assert all("openrouter-secret" not in message for message in outputs)


def test_build_one_line_install_command_renders_remote_bootstrap_command() -> None:
    command = install_module.build_one_line_install_command(
        target=Path("/srv/agent-sam"),
        install_nginx=False,
        start_services=False,
    )

    assert command.startswith("bash <(curl -fsSL https://raw.githubusercontent.com/samlaggz/Agent-Sam/main/install.sh)")
    assert "--target /srv/agent-sam" in command
    assert "--skip-nginx" in command
    assert "--skip-start" in command


def test_build_one_line_install_command_renders_private_github_api_command() -> None:
    command = install_module.build_one_line_install_command(
        token_env_var="AGENT_SAM_GITHUB_TOKEN",
    )

    assert "https://api.github.com/repos/samlaggz/Agent-Sam/contents/install.sh?ref=main" in command
    assert "Authorization: Bearer ${AGENT_SAM_GITHUB_TOKEN}" in command
    assert "Accept: application/vnd.github.raw" in command
    assert command.startswith("bash <(curl -fsSL")