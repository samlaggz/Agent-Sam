from __future__ import annotations

import subprocess
from pathlib import Path

from scripts import install as install_module


def _skip_dependency_bootstrap(env_values, **kwargs):
    return env_values


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
    monkeypatch.setattr(install_module, "_prepare_dependency_services", _skip_dependency_bootstrap)
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
    monkeypatch.setattr(install_module, "_prepare_dependency_services", _skip_dependency_bootstrap)

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


def test_run_install_interactive_reprompts_when_database_url_keeps_placeholder(tmp_path: Path, monkeypatch) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / ".env.example").write_text(
        "APP_ENV=production\n"
        "DATABASE_URL=postgresql+psycopg://agent_sam_user:CHANGE_ME@127.0.0.1:5432/agent_sam\n"
        "REDIS_URL=redis://127.0.0.1:6379/0\n"
        "QDRANT_URL=http://127.0.0.1:6333\n"
        "ENABLED_GATEWAYS=cli\n"
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
            "",
            "postgresql+psycopg://agent_sam_user:real@127.0.0.1:5432/agent_sam",
            "",
            "",
            "cli",
            "2",
        ]
        + [""] * 40
    )

    monkeypatch.setattr(install_module, "detect_os_name", lambda: "Linux")
    monkeypatch.setattr(install_module, "python_version_text", lambda: "3.11.9")
    monkeypatch.setattr(install_module, "_prepare_dependency_services", _skip_dependency_bootstrap)

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
        secret_prompt=lambda text: "openai-secret",
        output=outputs.append,
        command_runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout="ok", stderr=""),
    )

    env_text = (target / ".env").read_text(encoding="utf-8")

    assert exit_code == 0
    assert "DATABASE_URL=postgresql+psycopg://agent_sam_user:real@127.0.0.1:5432/agent_sam" in env_text
    assert any("DATABASE_URL must be set to a real value and cannot keep the placeholder." == message for message in outputs)


def test_run_install_interactive_normalizes_wrapped_dependency_urls(tmp_path: Path, monkeypatch) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / ".env.example").write_text(
        "APP_ENV=production\n"
        "DATABASE_URL=postgresql+psycopg://agent_sam_user:CHANGE_ME@127.0.0.1:5432/agent_sam\n"
        "REDIS_URL=redis://127.0.0.1:6379/0\n"
        "QDRANT_URL=http://127.0.0.1:6333\n"
        "ENABLED_GATEWAYS=cli\n"
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
            "[postgresql+psycopg://agent_sam_user:real@127.0.0.1:5432/agent_sam]",
            '"redis://127.0.0.1:6379/0"',
            "'[http://127.0.0.1:6333]'",
            "cli",
            "2",
        ]
        + [""] * 40
    )

    monkeypatch.setattr(install_module, "detect_os_name", lambda: "Linux")
    monkeypatch.setattr(install_module, "python_version_text", lambda: "3.11.9")
    monkeypatch.setattr(install_module, "_prepare_dependency_services", _skip_dependency_bootstrap)

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
        secret_prompt=lambda text: "openai-secret",
        output=outputs.append,
        command_runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout="ok", stderr=""),
    )

    env_text = (target / ".env").read_text(encoding="utf-8")

    assert exit_code == 0
    assert "DATABASE_URL=postgresql+psycopg://agent_sam_user:real@127.0.0.1:5432/agent_sam" in env_text
    assert "REDIS_URL=redis://127.0.0.1:6379/0" in env_text
    assert "QDRANT_URL=http://127.0.0.1:6333" in env_text


def test_prepare_dependency_services_starts_bundled_local_stack_and_rewrites_env(tmp_path: Path, monkeypatch) -> None:
    outputs: list[str] = []
    commands: list[list[str]] = []
    target = tmp_path / "target"
    target.mkdir()
    env_values = {
        "DATABASE_URL": "postgresql+psycopg://agent_sam_user:custom@127.0.0.1:5432/agent_sam",
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        "QDRANT_URL": "http://127.0.0.1:6333",
    }

    monkeypatch.setattr(install_module, "_tcp_endpoint_reachable", lambda host, port, timeout_seconds=1.0: False)
    monkeypatch.setattr(install_module, "_wait_for_local_dependency_services", lambda values, output, timeout_seconds=60.0: True)
    monkeypatch.setattr(install_module, "_ensure_docker_available", lambda **kwargs: True)

    updated_env_values = install_module._prepare_dependency_services(
        env_values,
        target=target,
        options=install_module.InstallOptions(
            target=target,
            dry_run=False,
            non_interactive=True,
            skip_nginx=True,
            skip_start=True,
            local=False,
            production=True,
        ),
        prompt=lambda text: "",
        output=outputs.append,
        command_runner=lambda command, **kwargs: commands.append(list(command)) or subprocess.CompletedProcess(command, 0, stdout="ok", stderr=""),
        is_root=True,
    )

    assert updated_env_values is not None
    assert updated_env_values["DATABASE_URL"] == install_module.BUNDLED_LOCAL_DATABASE_URL
    assert updated_env_values["REDIS_URL"] == install_module.BUNDLED_LOCAL_REDIS_URL
    assert updated_env_values["QDRANT_URL"] == install_module.BUNDLED_LOCAL_QDRANT_URL
    assert any(command[:6] == ["docker", "compose", "-f", f"{target.as_posix()}/docker-compose.yml", "up", "-d"] for command in commands)
    assert any("Using bundled local dependency stack defaults for DATABASE_URL, REDIS_URL, QDRANT_URL." == message for message in outputs)


def test_prepare_dependency_services_accepts_wrapped_loopback_database_url(tmp_path: Path, monkeypatch) -> None:
    outputs: list[str] = []
    commands: list[list[str]] = []
    target = tmp_path / "target"
    target.mkdir()
    env_values = {
        "DATABASE_URL": "[postgresql+psycopg://agent_sam_user:custom@127.0.0.1:5432/agent_sam]",
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        "QDRANT_URL": "http://127.0.0.1:6333",
    }

    monkeypatch.setattr(install_module, "_tcp_endpoint_reachable", lambda host, port, timeout_seconds=1.0: False)
    monkeypatch.setattr(install_module, "_wait_for_local_dependency_services", lambda values, output, timeout_seconds=60.0: True)
    monkeypatch.setattr(install_module, "_ensure_docker_available", lambda **kwargs: True)

    updated_env_values = install_module._prepare_dependency_services(
        env_values,
        target=target,
        options=install_module.InstallOptions(
            target=target,
            dry_run=False,
            non_interactive=True,
            skip_nginx=True,
            skip_start=True,
            local=False,
            production=True,
        ),
        prompt=lambda text: "",
        output=outputs.append,
        command_runner=lambda command, **kwargs: commands.append(list(command)) or subprocess.CompletedProcess(command, 0, stdout="ok", stderr=""),
        is_root=True,
    )

    assert updated_env_values is not None
    assert updated_env_values["DATABASE_URL"] == install_module.BUNDLED_LOCAL_DATABASE_URL
    assert any(command[:6] == ["docker", "compose", "-f", f"{target.as_posix()}/docker-compose.yml", "up", "-d"] for command in commands)
    assert all("Dependency endpoints are not loopback-only; skipping bundled local dependency bootstrap." != message for message in outputs)


def test_prepare_dependency_services_starts_only_missing_services(tmp_path: Path, monkeypatch) -> None:
    outputs: list[str] = []
    commands: list[list[str]] = []
    target = tmp_path / "target"
    target.mkdir()
    env_values = {
        "DATABASE_URL": "postgresql+psycopg://agent_sam_user:custom@127.0.0.1:5432/agent_sam",
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        "QDRANT_URL": "http://127.0.0.1:6333",
    }

    def fake_reachable(host: str, port: int, timeout_seconds: float = 1.0) -> bool:
        return port == 6379

    monkeypatch.setattr(install_module, "_tcp_endpoint_reachable", fake_reachable)
    monkeypatch.setattr(install_module, "_wait_for_local_dependency_services", lambda values, output, timeout_seconds=60.0: True)
    monkeypatch.setattr(install_module, "_ensure_docker_available", lambda **kwargs: True)

    updated_env_values = install_module._prepare_dependency_services(
        env_values,
        target=target,
        options=install_module.InstallOptions(
            target=target,
            dry_run=False,
            non_interactive=True,
            skip_nginx=True,
            skip_start=True,
            local=False,
            production=True,
        ),
        prompt=lambda text: "",
        output=outputs.append,
        command_runner=lambda command, **kwargs: commands.append(list(command)) or subprocess.CompletedProcess(command, 0, stdout="ok", stderr=""),
        is_root=True,
    )

    assert updated_env_values is not None
    assert updated_env_values["DATABASE_URL"] == install_module.BUNDLED_LOCAL_DATABASE_URL
    assert updated_env_values["REDIS_URL"] == env_values["REDIS_URL"]
    assert updated_env_values["QDRANT_URL"] == install_module.BUNDLED_LOCAL_QDRANT_URL
    assert any(command == ["docker", "compose", "-f", f"{target.as_posix()}/docker-compose.yml", "up", "-d", "postgres", "qdrant"] for command in commands)
    assert all("redis" not in command[6:] for command in commands if command[:6] == ["docker", "compose", "-f", f"{target.as_posix()}/docker-compose.yml", "up", "-d"])
    assert any("Using bundled local dependency stack defaults for DATABASE_URL, QDRANT_URL." == message for message in outputs)


def test_prepare_dependency_services_installs_docker_when_missing_on_linux(tmp_path: Path, monkeypatch) -> None:
    outputs: list[str] = []
    commands: list[list[str]] = []
    target = tmp_path / "target"
    target.mkdir()
    env_values = {
        "DATABASE_URL": "postgresql+psycopg://agent_sam_user:custom@127.0.0.1:5432/agent_sam",
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        "QDRANT_URL": "http://127.0.0.1:6333",
    }
    docker_availability = iter([False, True])

    monkeypatch.setattr(install_module, "detect_os_name", lambda: "Linux")
    monkeypatch.setattr(install_module, "_tcp_endpoint_reachable", lambda host, port, timeout_seconds=1.0: False)
    monkeypatch.setattr(install_module, "_wait_for_local_dependency_services", lambda values, output, timeout_seconds=60.0: True)
    monkeypatch.setattr(install_module, "_docker_available", lambda **kwargs: next(docker_availability))
    monkeypatch.setattr(
        install_module.shutil,
        "which",
        lambda name: {
            "apt-get": "/usr/bin/apt-get",
            "systemctl": "/usr/bin/systemctl",
        }.get(name),
    )

    updated_env_values = install_module._prepare_dependency_services(
        env_values,
        target=target,
        options=install_module.InstallOptions(
            target=target,
            dry_run=False,
            non_interactive=True,
            skip_nginx=True,
            skip_start=True,
            local=False,
            production=True,
        ),
        prompt=lambda text: "",
        output=outputs.append,
        command_runner=lambda command, **kwargs: commands.append(list(command)) or subprocess.CompletedProcess(command, 0, stdout="ok", stderr=""),
        is_root=True,
    )

    assert updated_env_values is not None
    assert any(command[:2] == ["apt-get", "update"] for command in commands)
    assert any(command[:5] == ["apt-get", "install", "-y", "docker.io", "docker-compose-plugin"] for command in commands)
    assert any(command[:4] == ["systemctl", "enable", "--now", "docker"] for command in commands)
    assert any(command[:6] == ["docker", "compose", "-f", f"{target.as_posix()}/docker-compose.yml", "up", "-d"] for command in commands)
    assert any("Installing Docker packages with apt-get." == message for message in outputs)
    assert any("Docker is available." == message for message in outputs)


def test_prepare_dependency_services_fails_cleanly_when_docker_is_unavailable(tmp_path: Path, monkeypatch) -> None:
    outputs: list[str] = []
    target = tmp_path / "target"
    target.mkdir()
    env_values = {
        "DATABASE_URL": "postgresql+psycopg://agent_sam_user:custom@127.0.0.1:5432/agent_sam",
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        "QDRANT_URL": "http://127.0.0.1:6333",
    }

    monkeypatch.setattr(install_module, "_tcp_endpoint_reachable", lambda host, port, timeout_seconds=1.0: False)
    monkeypatch.setattr(install_module, "_ensure_docker_available", lambda **kwargs: False)

    updated_env_values = install_module._prepare_dependency_services(
        env_values,
        target=target,
        options=install_module.InstallOptions(
            target=target,
            dry_run=False,
            non_interactive=True,
            skip_nginx=True,
            skip_start=True,
            local=False,
            production=True,
        ),
        prompt=lambda text: "",
        output=outputs.append,
        command_runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout="ok", stderr=""),
        is_root=True,
    )

    assert updated_env_values is None
    assert any(message.startswith("Local dependency services are not reachable on the configured loopback URLs:") for message in outputs)


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