from __future__ import annotations

import argparse
import asyncio
import getpass
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.asyncio_compat import configure_windows_event_loop_policy
from app.config import Settings
from gateways.setup import ensure_env_file, test_gateway_config, update_env_values
from scripts.common import DEFAULT_NEXT_COMMANDS, ENV_PATH, LOCAL_DEV_FIX_COMMAND, PROJECT_ROOT, detect_os_name, format_command, parse_seed_output, print_next_commands, python_version_text, run_subprocess
from scripts.env_writer import load_env_values, update_env_file
from scripts.install import INSTALL_TOKEN_ENV_VAR, LLM_PROVIDER_OPTIONS, PRODUCTION_TARGET, _resolve_specialist_runtime_updates, build_one_line_install_command


PromptFunc = Callable[[str], str]
OutputFunc = Callable[[str], None]
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class SetupIO:
    prompt: PromptFunc
    secret_prompt: PromptFunc
    output: OutputFunc


def run() -> None:
    parser = argparse.ArgumentParser(description="Run the Agent_Sam setup wizard.")
    parser.add_argument("--mode", choices=("local", "linux", "cli"), default=None)
    parser.add_argument("--env-path", type=Path, default=ENV_PATH)
    parser.add_argument("--dry-run", action="store_true", help="Preview setup actions without changing files or running commands.")
    parser.add_argument("--production", action="store_true", help="Open the Linux production setup path directly.")
    arguments = parser.parse_args()
    selected_mode = arguments.mode or ("linux" if arguments.production else None)
    raise SystemExit(run_setup(mode=selected_mode, env_path=arguments.env_path, dry_run=arguments.dry_run))


def run_setup(
    *,
    mode: str | None = None,
    env_path: Path = ENV_PATH,
    dry_run: bool = False,
    prompt: PromptFunc = input,
    secret_prompt: PromptFunc = getpass.getpass,
    output: OutputFunc = print,
    command_runner: CommandRunner = run_subprocess,
) -> int:
    configure_windows_event_loop_policy()
    wizard_io = SetupIO(prompt=prompt, secret_prompt=secret_prompt, output=output)
    project_root = PROJECT_ROOT
    resolved_env_path, created_env = ensure_env_file(env_path)

    output("Agent_Sam Setup Wizard")
    output(f"OS: {detect_os_name()}")
    output(f"Python: {python_version_text()}")
    output(f"Project root: {project_root}")
    output(f".env: {'created from .env.example' if created_env else 'found'}")

    if mode == "local":
        return _run_local_development_setup(resolved_env_path, wizard_io, command_runner, dry_run=dry_run)
    if mode == "cli":
        return _run_cli_only_setup(resolved_env_path, wizard_io, command_runner, dry_run=dry_run)
    if mode == "linux":
        return _run_linux_production_setup(resolved_env_path, wizard_io, dry_run=dry_run)

    if dry_run:
        _preview_menu(output)
        return 0
    return _run_interactive_menu(resolved_env_path, wizard_io, command_runner)


def _preview_menu(output: OutputFunc) -> None:
    output("Dry run menu preview:")
    output("1. Local development")
    output("2. Linux production")
    output("3. Configure gateways")
    output("4. Configure model routing")
    output("5. Run migrations")
    output("6. Seed default user/workspace")
    output("7. Run doctor")
    output("8. Exit")
    output("Dry run: no files were changed and no commands were executed.")


def _run_interactive_menu(
    env_path: Path,
    wizard_io: SetupIO,
    command_runner: CommandRunner,
) -> int:
    while True:
        wizard_io.output("Choose an action:")
        wizard_io.output("1. Local development")
        wizard_io.output("2. Linux production")
        wizard_io.output("3. Configure gateways")
        wizard_io.output("4. Configure model routing")
        wizard_io.output("5. Run migrations")
        wizard_io.output("6. Seed default user/workspace")
        wizard_io.output("7. Run doctor")
        wizard_io.output("8. Exit")
        selection = wizard_io.prompt("Select action (1-8): ").strip()
        if selection == "1":
            return _run_local_development_setup(env_path, wizard_io, command_runner, dry_run=False)
        if selection == "2":
            return _run_linux_production_setup(env_path, wizard_io, dry_run=False)
        if selection == "3":
            _configure_gateway_defaults(env_path, wizard_io, dry_run=False)
            continue
        if selection == "4":
            _configure_model_routing(env_path, wizard_io, dry_run=False)
            continue
        if selection == "5":
            _run_command(command_runner, [sys.executable, "-m", "alembic", "upgrade", "head"], wizard_io, dry_run=False)
            continue
        if selection == "6":
            _seed_default_ids(env_path, wizard_io, command_runner, dry_run=False)
            continue
        if selection == "7":
            production_doctor = _prompt_yes_no(wizard_io, "Run production doctor?", default=False)
            doctor_command = [sys.executable, "-m", "scripts.doctor"]
            if production_doctor:
                doctor_command.append("--production")
            _run_command(command_runner, doctor_command, wizard_io, dry_run=False)
            continue
        if selection == "8":
            wizard_io.output("Exiting setup wizard.")
            return 0
        wizard_io.output("Choose a number from 1 to 8.")


def _run_local_development_setup(
    env_path: Path,
    wizard_io: SetupIO,
    command_runner: CommandRunner,
    *,
    dry_run: bool,
) -> int:
    if dry_run:
        wizard_io.output("Dry run: would verify Docker, install editable dependencies, run migrations, seed dev data, and configure gateways.")
        print_next_commands(wizard_io.output, DEFAULT_NEXT_COMMANDS)
        return 0

    docker_ready = _docker_is_available(command_runner)
    if docker_ready:
        wizard_io.output("Docker: available")
        if _prompt_yes_no(wizard_io, "Run docker compose up -d postgres redis qdrant now?", default=True):
            result = _run_command(
                command_runner,
                ["docker", "compose", "up", "-d", "postgres", "redis", "qdrant"],
                wizard_io,
                dry_run=False,
            )
            if result.returncode != 0:
                return 1
    else:
        wizard_io.output("Docker: unavailable or daemon not ready")
        wizard_io.output(f"Run: {LOCAL_DEV_FIX_COMMAND}")
        return 1

    for command in (
        [sys.executable, "-m", "pip", "install", "-e", ".[dev]"],
        [sys.executable, "-m", "alembic", "upgrade", "head"],
    ):
        result = _run_command(command_runner, command, wizard_io, dry_run=False)
        if result.returncode != 0:
            return 1

    if _seed_default_ids(env_path, wizard_io, command_runner, dry_run=False) != 0:
        return 1

    _configure_gateway_defaults(env_path, wizard_io, dry_run=False)
    _configure_model_routing(env_path, wizard_io, dry_run=False)
    test_gateway_config(env_path, output=wizard_io.output)
    print_next_commands(wizard_io.output, DEFAULT_NEXT_COMMANDS)
    return 0


def _run_cli_only_setup(
    env_path: Path,
    wizard_io: SetupIO,
    command_runner: CommandRunner,
    *,
    dry_run: bool,
) -> int:
    if dry_run:
        wizard_io.output("Dry run: would set ENABLED_GATEWAYS=cli and seed defaults if the database is reachable.")
        wizard_io.output("Next command:")
        wizard_io.output("python -m gateways.cli.main")
        return 0

    update_env_values(env_path, {"ENABLED_GATEWAYS": "cli"})
    settings = Settings(_env_file=env_path)
    if settings.default_workspace_id is None or settings.default_user_id is None:
        if asyncio.run(_database_is_reachable(settings)):
            if _seed_default_ids(env_path, wizard_io, command_runner, dry_run=False) != 0:
                return 1
        else:
            wizard_io.output(f"Database is not ready. Run: {LOCAL_DEV_FIX_COMMAND}")
            wizard_io.output("Then rerun python -m scripts.setup and choose CLI-only test mode.")
            return 1

    test_gateway_config(env_path, output=wizard_io.output)
    wizard_io.output("Next command:")
    wizard_io.output("python -m gateways.cli.main")
    return 0


def _run_linux_production_setup(
    env_path: Path,
    wizard_io: SetupIO,
    *,
    dry_run: bool,
) -> int:
    env_values = load_env_values(env_path)
    target_dir = wizard_io.prompt(f"Target directory [{PRODUCTION_TARGET}]: ").strip() if not dry_run else ""
    resolved_target = Path(target_dir or str(PRODUCTION_TARGET))
    rendered_target = resolved_target.as_posix()
    install_nginx = True if dry_run else _prompt_yes_no(wizard_io, "Install nginx configuration?", default=True)
    start_services = True if dry_run else _prompt_yes_no(wizard_io, "Enable and start services after bootstrap?", default=True)

    updates = {
        "APP_ENV": "production",
        "ENVIRONMENT": "production",
        "API_HOST": env_values.get("API_HOST") or "127.0.0.1",
        "API_PORT": env_values.get("API_PORT") or "8000",
    }
    updates.update(_collect_production_gateway_updates(env_values, wizard_io, dry_run=dry_run))
    updates.update(_configure_model_routing(env_path, wizard_io, dry_run=dry_run, persist=False))

    if dry_run:
        wizard_io.output(f"Dry run: would update {env_path} for Linux production")
    else:
        update_env_file(env_path, updates)
        wizard_io.output(f"Updated {env_path} for Linux production.")

    install_command = ["bash", "install.sh"]
    if resolved_target != PRODUCTION_TARGET:
        install_command.extend(["--target", rendered_target])
    if not install_nginx:
        install_command.append("--skip-nginx")
    if not start_services:
        install_command.append("--skip-start")

    wizard_io.output("Production one-line install command:")
    wizard_io.output(
        build_one_line_install_command(
            target=resolved_target,
            install_nginx=install_nginx,
            start_services=start_services,
        )
    )
    wizard_io.output("Private GitHub repo one-line install command:")
    wizard_io.output(f"export {INSTALL_TOKEN_ENV_VAR}=<github_pat_with_repo_read>")
    wizard_io.output(
        build_one_line_install_command(
            target=resolved_target,
            install_nginx=install_nginx,
            start_services=start_services,
            token_env_var=INSTALL_TOKEN_ENV_VAR,
        )
    )
    wizard_io.output("Checkout-based install command:")
    wizard_io.output(format_command(install_command))
    wizard_io.output("Manual fallback commands:")
    wizard_io.output(f"bash deployment/linux/install-host-assets.sh {rendered_target}")
    wizard_io.output(f"sudo rsync -a --delete --exclude '.git' --exclude '.venv' ./ {rendered_target}/")
    wizard_io.output(f"sudo chown -R agentos:agentos {rendered_target}")
    wizard_io.output(f"sudo -iu agentos; cd {rendered_target}; bash deployment/linux/bootstrap-app.sh")
    wizard_io.output("Recommended follow-up commands:")
    wizard_io.output("python -m scripts.doctor --production")
    wizard_io.output("sudo systemctl enable agent-api agent-worker agent-telegram")
    wizard_io.output("sudo systemctl start agent-api agent-worker agent-telegram")
    wizard_io.output("bash deployment/linux/service-control.sh status")
    wizard_io.output("bash deployment/linux/service-control.sh logs")
    return 0


def _configure_gateway_defaults(env_path: Path, wizard_io: SetupIO, *, dry_run: bool) -> dict[str, str]:
    env_values = load_env_values(env_path)
    current_gateways = env_values.get("ENABLED_GATEWAYS") or "telegram"
    if dry_run:
        wizard_io.output(f"Dry run: would configure gateways (current: {current_gateways})")
        return {"ENABLED_GATEWAYS": current_gateways}

    raw_value = wizard_io.prompt(f"Enabled gateways [{current_gateways}]: ").strip() or current_gateways
    normalized: list[str] = []
    seen: set[str] = set()
    for token in raw_value.split(","):
        name = token.strip().lower()
        if not name or name in seen:
            continue
        normalized.append(name)
        seen.add(name)

    updates = {"ENABLED_GATEWAYS": ",".join(normalized)}
    if "telegram" in seen:
        telegram_token = _prompt_optional_secret(
            wizard_io,
            key="TELEGRAM_BOT_TOKEN",
            existing_value=env_values.get("TELEGRAM_BOT_TOKEN", ""),
        )
        updates["TELEGRAM_BOT_TOKEN"] = telegram_token
    update_env_file(env_path, updates)
    wizard_io.output(f"Updated {env_path.name} gateway settings.")
    return updates


def _configure_llm_provider(
    env_path: Path,
    wizard_io: SetupIO,
    *,
    dry_run: bool,
    persist: bool = True,
) -> dict[str, str]:
    env_values = load_env_values(env_path)
    current_model = env_values.get("LITELLM_MODEL", "openai/gpt-4o-mini")
    default_choice = _infer_llm_choice(current_model, env_values)

    if dry_run:
        provider_name = LLM_PROVIDER_OPTIONS[default_choice][0]
        wizard_io.output(f"Dry run: would configure LLM provider ({provider_name})")
        return {"LITELLM_MODEL": current_model}

    wizard_io.output("Choose LLM provider:")
    for option, (label, _, _, _) in LLM_PROVIDER_OPTIONS.items():
        wizard_io.output(f"{option}. {label}")
    choice = wizard_io.prompt(f"LLM provider [{default_choice}]: ").strip() or default_choice
    if choice not in LLM_PROVIDER_OPTIONS:
        wizard_io.output("Keeping existing LLM provider configuration.")
        return {"LITELLM_MODEL": current_model}

    label, default_model, secret_key_name, base_url_key_name = LLM_PROVIDER_OPTIONS[choice]
    updates: dict[str, str] = {}
    if choice == "6":
        updates["LITELLM_MODEL"] = wizard_io.prompt(f"LITELLM_MODEL [{current_model}]: ").strip() or current_model
    else:
        updates["LITELLM_MODEL"] = default_model

    if secret_key_name is not None:
        updates[secret_key_name] = _prompt_optional_secret(
            wizard_io,
            key=secret_key_name,
            existing_value=env_values.get(secret_key_name, ""),
        )
    if base_url_key_name is not None:
        default_base_url = "https://openrouter.ai/api/v1" if base_url_key_name == "OPENROUTER_BASE_URL" else "http://127.0.0.1:11434"
        current_base_url = env_values.get(base_url_key_name) or default_base_url
        updates[base_url_key_name] = wizard_io.prompt(f"{base_url_key_name} [{current_base_url}]: ").strip() or current_base_url

    if persist:
        update_env_file(env_path, updates)
        wizard_io.output(f"LLM provider saved: {label}")
    return updates


def _configure_model_routing(
    env_path: Path,
    wizard_io: SetupIO,
    *,
    dry_run: bool,
    persist: bool = True,
) -> dict[str, str]:
    env_values = load_env_values(env_path)
    llm_updates = _configure_llm_provider(env_path, wizard_io, dry_run=dry_run, persist=False)
    specialist_updates = _resolve_specialist_runtime_updates(
        current_values={**env_values, **llm_updates},
        prompt=wizard_io.prompt,
        secret_prompt=wizard_io.secret_prompt,
        non_interactive=False,
        dry_run=dry_run,
        output=wizard_io.output,
    )
    if specialist_updates is None:
        wizard_io.output("Keeping existing specialist routing configuration.")
        return llm_updates

    updates = {**llm_updates, **specialist_updates}
    if persist and not dry_run:
        update_env_file(env_path, updates)
        wizard_io.output("Model routing saved.")
    elif dry_run:
        wizard_io.output("Dry run: would update specialist routing and budget settings.")
    return updates


def _prompt_optional_secret(wizard_io: SetupIO, *, key: str, existing_value: str) -> str:
    prompt_suffix = " [press Enter to keep existing]" if existing_value else " [press Enter to leave unset]"
    secret_value = wizard_io.secret_prompt(f"{key}{prompt_suffix}: ").strip()
    if secret_value:
        wizard_io.output(f"{key}: saved")
        return secret_value
    if existing_value:
        wizard_io.output(f"{key}: saved")
        return existing_value
    wizard_io.output(f"{key} remains unset.")
    return ""


def _infer_llm_choice(current_model: str, env_values: dict[str, str]) -> str:
    normalized = current_model.lower()
    if normalized.startswith("openrouter/") or env_values.get("OPENROUTER_API_KEY", ""):
        return "1"
    if normalized.startswith("anthropic/"):
        return "3"
    if normalized.startswith("gemini/") or normalized.startswith("google/"):
        return "4"
    if normalized.startswith("ollama/"):
        return "5"
    if env_values.get("LITELLM_API_KEY", ""):
        return "6"
    return "2"


def _collect_production_gateway_updates(env_values: dict[str, str], wizard_io: SetupIO, *, dry_run: bool) -> dict[str, str]:
    current_gateways = env_values.get("ENABLED_GATEWAYS") or "telegram"
    if dry_run:
        wizard_io.output(f"Dry run: would configure production gateways [{current_gateways}]")
        return {"ENABLED_GATEWAYS": current_gateways}

    raw_value = wizard_io.prompt(f"Production gateways [{current_gateways}]: ").strip() or current_gateways
    normalized = []
    seen: set[str] = set()
    for token in raw_value.split(","):
        name = token.strip().lower()
        if not name or name in seen:
            continue
        normalized.append(name)
        seen.add(name)

    updates = {"ENABLED_GATEWAYS": ",".join(normalized)}
    if "telegram" in seen:
        updates["TELEGRAM_BOT_TOKEN"] = _prompt_optional_secret(
            wizard_io,
            key="TELEGRAM_BOT_TOKEN",
            existing_value=env_values.get("TELEGRAM_BOT_TOKEN", ""),
        )
    return updates


def _seed_default_ids(
    env_path: Path,
    wizard_io: SetupIO,
    command_runner: CommandRunner,
    *,
    dry_run: bool,
) -> int:
    if dry_run:
        wizard_io.output("Dry run: would run scripts/seed_dev.py and write DEFAULT_USER_ID / DEFAULT_WORKSPACE_ID to .env")
        return 0

    seed_result = _run_command(command_runner, [sys.executable, "scripts/seed_dev.py"], wizard_io, dry_run=False)
    if seed_result.returncode != 0:
        return 1

    default_user_id, default_workspace_id = parse_seed_output(seed_result.stdout or "")
    if default_user_id and default_workspace_id:
        update_env_values(
            env_path,
            {
                "DEFAULT_USER_ID": default_user_id,
                "DEFAULT_WORKSPACE_ID": default_workspace_id,
            },
        )
        wizard_io.output("Wrote DEFAULT_USER_ID and DEFAULT_WORKSPACE_ID to .env")
    return 0


def _run_command(
    command_runner: CommandRunner,
    command: Sequence[str],
    wizard_io: SetupIO,
    *,
    dry_run: bool,
) -> subprocess.CompletedProcess[str]:
    wizard_io.output(f"Running: {format_command(command)}")
    if dry_run:
        wizard_io.output("Dry run: command not executed")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    result = command_runner(command, cwd=PROJECT_ROOT, capture_output=True)
    if result.stdout:
        wizard_io.output(result.stdout.strip())
    if result.returncode != 0:
        if result.stderr:
            wizard_io.output(result.stderr.strip())
        wizard_io.output(f"Command failed: {format_command(command)}")
    return result


def _docker_is_available(command_runner: CommandRunner) -> bool:
    if shutil.which("docker") is None:
        return False
    result = command_runner(["docker", "version"], cwd=PROJECT_ROOT, capture_output=True)
    return result.returncode == 0


def _prompt_yes_no(wizard_io: SetupIO, question: str, *, default: bool) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        raw_value = wizard_io.prompt(f"{question} {suffix} ").strip().lower()
        if not raw_value:
            return default
        if raw_value in {"y", "yes"}:
            return True
        if raw_value in {"n", "no"}:
            return False
        wizard_io.output("Answer yes or no.")


async def _database_is_reachable(settings: Settings) -> bool:
    engine = create_async_engine(settings.database_url, future=True, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        await engine.dispose()


if __name__ == "__main__":
    run()