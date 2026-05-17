from __future__ import annotations

import asyncio
import argparse
import getpass
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

from scripts.common import PROJECT_ROOT, detect_os_name, format_command, python_version_text, run_subprocess
from scripts.env_writer import ensure_env_file, load_env_values, update_env_file
from scripts.nginx_installer import install_nginx_config


PromptFunc = Callable[[str], str]
OutputFunc = Callable[[str], None]
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]

PRODUCTION_TARGET = Path("/opt/agent-sam")
APP_USER = "agentos"
INSTALL_REPO_SLUG = "samlaggz/Agent-Sam"
INSTALL_REPO_REF = "main"
INSTALL_TOKEN_ENV_VAR = "AGENT_SAM_GITHUB_TOKEN"
BUNDLED_LOCAL_DATABASE_URL = "postgresql+psycopg://agent_sam:agent_sam@127.0.0.1:5432/agent_sam"
BUNDLED_LOCAL_REDIS_URL = "redis://127.0.0.1:6379/0"
BUNDLED_LOCAL_QDRANT_URL = "http://127.0.0.1:6333"
AVAILABLE_GATEWAYS = ("telegram", "cli", "webhook")
DEPENDENCY_SERVICE_CONFIG = {
    "DATABASE_URL": ("postgres", BUNDLED_LOCAL_DATABASE_URL, 5432),
    "REDIS_URL": ("redis", BUNDLED_LOCAL_REDIS_URL, 6379),
    "QDRANT_URL": ("qdrant", BUNDLED_LOCAL_QDRANT_URL, 6333),
}
LLM_PROVIDER_OPTIONS = {
    "1": ("OpenRouter", "openrouter/openai/gpt-4.1-mini", "OPENROUTER_API_KEY", "OPENROUTER_BASE_URL"),
    "2": ("OpenAI", "openai/gpt-4o-mini", "OPENAI_API_KEY", None),
    "3": ("Anthropic", "anthropic/claude-3-5-sonnet-latest", "ANTHROPIC_API_KEY", None),
    "4": ("Gemini", "gemini/gemini-1.5-flash", "GEMINI_API_KEY", None),
    "5": ("Ollama/local", "ollama/llama3.2", None, "OLLAMA_BASE_URL"),
    "6": ("Custom LiteLLM model", "", "LITELLM_API_KEY", None),
}
SPECIALIST_MODEL_DEFAULTS = {
    "DEFAULT_MODEL": "openrouter/openai/gpt-4.1-mini",
    "ROUTER_AGENT_MODEL": "openrouter/openai/gpt-4.1-mini",
    "CODING_AGENT_MODEL": "openrouter/openai/gpt-4.1-mini",
    "CODING_AGENT_ESCALATION_MODEL": "openrouter/anthropic/claude-3.7-sonnet",
    "TESTING_AGENT_MODEL": "openrouter/google/gemini-2.0-flash-001",
    "TESTING_AGENT_ESCALATION_MODEL": "openrouter/openai/gpt-4.1",
    "RESEARCH_AGENT_MODEL": "openrouter/google/gemini-2.0-flash-001",
    "RESEARCH_AGENT_ESCALATION_MODEL": "openrouter/anthropic/claude-3.7-sonnet",
    "PLANNING_AGENT_MODEL": "openrouter/openai/gpt-4.1-mini",
    "PLANNING_AGENT_ESCALATION_MODEL": "openrouter/anthropic/claude-3.7-sonnet",
    "SERVER_OPS_AGENT_MODEL": "openrouter/openai/gpt-4.1-mini",
    "SERVER_OPS_AGENT_ESCALATION_MODEL": "openrouter/anthropic/claude-3.7-sonnet",
    "GRAPHICS_AGENT_MODEL": "openrouter/google/gemini-2.0-flash-001",
    "GRAPHICS_AGENT_ESCALATION_MODEL": "openrouter/anthropic/claude-3.7-sonnet",
    "DATA_AGENT_MODEL": "openrouter/openai/gpt-4.1-mini",
    "DATA_AGENT_ESCALATION_MODEL": "openrouter/anthropic/claude-3.7-sonnet",
    "QA_AGENT_MODEL": "openrouter/openai/gpt-4.1-mini",
    "QA_AGENT_ESCALATION_MODEL": "openrouter/anthropic/claude-3.7-sonnet",
}
SPECIALIST_NUMERIC_DEFAULTS = {
    "MAX_COST_PER_TASK_USD": "0.25",
    "DAILY_MODEL_BUDGET_USD": "5.0",
}
SPECIALIST_BOOL_DEFAULTS = {
    "ALLOW_MODEL_ESCALATION": "true",
    "ALLOW_SKILL_AUTO_PROPOSAL": "true",
    "ALLOW_SKILL_AUTO_ACTIVATION": "false",
    "ALLOW_SUB_AGENT_PROPOSAL": "true",
    "ALLOW_SUB_AGENT_AUTO_CREATION": "false",
    "ENABLE_WEB_RESEARCH": "false",
}


@dataclass(frozen=True)
class InstallOptions:
    target: Path
    dry_run: bool
    non_interactive: bool
    skip_nginx: bool
    skip_start: bool
    local: bool
    production: bool


class StepPrinter:
    def __init__(self, total_steps: int, output: OutputFunc) -> None:
        self._total_steps = total_steps
        self._output = output
        self._current_step = 0

    def step(self, message: str) -> None:
        self._current_step += 1
        self._output(f"[{self._current_step}/{self._total_steps}] {message}")


def build_one_line_install_command(
    *,
    target: Path = PRODUCTION_TARGET,
    install_nginx: bool = True,
    start_services: bool = True,
    repo_slug: str = INSTALL_REPO_SLUG,
    repo_ref: str = INSTALL_REPO_REF,
    token_env_var: str | None = None,
) -> str:
    if token_env_var:
        script_url = f"https://api.github.com/repos/{repo_slug}/contents/install.sh?ref={repo_ref}"
        download_command = (
            f'curl -fsSL -H "Authorization: Bearer ${{{token_env_var}}}" '
            f'-H "Accept: application/vnd.github.raw" {shlex.quote(script_url)}'
        )
    else:
        script_url = f"https://raw.githubusercontent.com/{repo_slug}/{repo_ref}/install.sh"
        download_command = f"curl -fsSL {shlex.quote(script_url)}"
    installer_args: list[str] = []

    if target != PRODUCTION_TARGET:
        installer_args.extend(["--target", target.as_posix()])
    if not install_nginx:
        installer_args.append("--skip-nginx")
    if not start_services:
        installer_args.append("--skip-start")

    rendered_args = " ".join(shlex.quote(argument) for argument in installer_args)
    if rendered_args:
        return f"bash <({download_command}) {rendered_args}"
    return f"bash <({download_command})"


def run() -> None:
    parser = argparse.ArgumentParser(description="Install Agent_Sam locally or on a Linux production host.")
    parser.add_argument("--target", type=Path, default=PRODUCTION_TARGET)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--skip-nginx", action="store_true")
    parser.add_argument("--skip-start", action="store_true")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--production", action="store_true")
    arguments = parser.parse_args()
    options = InstallOptions(
        target=arguments.target,
        dry_run=arguments.dry_run,
        non_interactive=arguments.non_interactive,
        skip_nginx=arguments.skip_nginx,
        skip_start=arguments.skip_start,
        local=arguments.local,
        production=arguments.production or not arguments.local,
    )
    raise SystemExit(run_install(options))


def run_install(
    options: InstallOptions,
    *,
    repo_root: Path = PROJECT_ROOT,
    prompt: PromptFunc = input,
    secret_prompt: PromptFunc = getpass.getpass,
    output: OutputFunc = print,
    command_runner: CommandRunner = run_subprocess,
) -> int:
    if options.local:
        return _run_local_install(options, output=output)
    return _run_production_install(
        options,
        repo_root=repo_root,
        prompt=prompt,
        secret_prompt=secret_prompt,
        output=output,
        command_runner=command_runner,
    )


def _run_local_install(options: InstallOptions, *, output: OutputFunc) -> int:
    output("Agent_Sam local install delegates to the Hermes-style setup wizard.")
    if options.dry_run:
        output("Dry run: would run python -m scripts.setup --mode local")
        return 0

    from scripts import setup as setup_module

    return setup_module.run_setup(mode="local")


def _run_production_install(
    options: InstallOptions,
    *,
    repo_root: Path,
    prompt: PromptFunc,
    secret_prompt: PromptFunc,
    output: OutputFunc,
    command_runner: CommandRunner,
) -> int:
    printer = StepPrinter(total_steps=11, output=output)
    env_path = options.target / ".env"
    example_path = repo_root / ".env.example"
    is_root = _is_root_user()
    rendered_target = options.target.as_posix()
    rendered_env_path = env_path.as_posix()

    install_nginx_choice = not options.skip_nginx
    start_services_choice = not options.skip_start
    if not options.non_interactive and not options.dry_run:
        if not options.skip_nginx:
            install_nginx_choice = _prompt_yes_no(prompt, "Install nginx config if nginx is present?", default=True)
        if not options.skip_start:
            start_services_choice = _prompt_yes_no(prompt, "Enable and start services after bootstrap?", default=True)

    printer.step("Checking OS")
    os_name = detect_os_name()
    output(f"Detected OS: {os_name}")
    if os_name != "Linux" and not options.dry_run:
        output("Linux production install must run on a Linux host. Use --dry-run to preview elsewhere.")
        return 1

    printer.step("Checking Python and privilege model")
    output(f"Python: {python_version_text()}")
    if tuple(int(part) for part in python_version_text().split(".")[:2]) < (3, 11) and not options.dry_run:
        output("Python 3.11 or newer is required for Linux production installation.")
        return 1
    if not is_root and shutil.which("sudo") is None and not options.dry_run:
        output("sudo is required when running the installer from a non-root account.")
        return 1
    output(
        "Installer privilege mode: root host setup, app bootstrap as agentos"
        if is_root
        else "Installer privilege mode: sudo for host setup, app bootstrap as agentos"
    )

    printer.step("Installing host assets")
    host_setup_command = ["bash", str(repo_root / "deployment/linux/install-host-assets.sh"), rendered_target]
    if options.dry_run:
        host_setup_command.insert(2, "--dry-run")
    if options.skip_nginx:
        host_setup_command.insert(2, "--skip-nginx")
    if _run_with_output(
        host_setup_command,
        command_runner=command_runner,
        output=output,
        dry_run=options.dry_run,
    ).returncode != 0:
        return 1

    printer.step("Syncing repository to target")
    sync_command = _privileged_command(
        [
            "rsync",
            "-a",
            "--delete",
            "--exclude",
            ".git",
            "--exclude",
            ".venv",
            f"{repo_root}{os.sep}",
            f"{rendered_target}/",
        ],
        is_root=is_root,
    )
    if _run_with_output(sync_command, command_runner=command_runner, output=output, dry_run=options.dry_run).returncode != 0:
        return 1
    chown_command = _privileged_command(["chown", "-R", f"{APP_USER}:{APP_USER}", rendered_target], is_root=is_root)
    if _run_with_output(chown_command, command_runner=command_runner, output=output, dry_run=options.dry_run).returncode != 0:
        return 1

    printer.step("Writing environment configuration")
    env_updates = _collect_production_env_updates(
        env_path=env_path,
        example_path=example_path,
        options=options,
        prompt=prompt,
        secret_prompt=secret_prompt,
        output=output,
        dry_run=options.dry_run,
    )
    if env_updates is None:
        return 1
    if not options.dry_run:
        ensure_env_file(env_path, example_path=example_path)
        update_env_file(env_path, env_updates, example_path=example_path, preserve_existing_values=False)

    printer.step("Preparing dependency services")
    prepared_env_updates = _prepare_dependency_services(
        env_updates,
        target=options.target,
        options=options,
        prompt=prompt,
        output=output,
        command_runner=command_runner,
        is_root=is_root,
    )
    if prepared_env_updates is None:
        return 1
    env_updates = prepared_env_updates
    if not options.dry_run:
        update_env_file(env_path, env_updates, example_path=example_path, preserve_existing_values=False)

    printer.step("Bootstrapping the application as agentos")
    bootstrap_command = _command_as_user(
        APP_USER,
        f"cd {shlex.quote(rendered_target)} && bash deployment/linux/bootstrap-app.sh",
        is_root=is_root,
    )
    if _run_with_output(bootstrap_command, command_runner=command_runner, output=output, dry_run=options.dry_run).returncode != 0:
        return 1

    printer.step("Installing nginx configuration")
    if install_nginx_choice and not options.skip_nginx:
        server_name = _resolve_server_name(
            env_values=load_env_values(env_path, example_path=example_path) if env_path.exists() else {},
            non_interactive=options.non_interactive,
            prompt=prompt,
            dry_run=options.dry_run,
        )
        if server_name is None:
            return 1
        if not install_nginx_config(
            source_config=options.target / "deployment/nginx/agent-api.conf",
            server_name=server_name,
            dry_run=options.dry_run,
            output=output,
            command_runner=command_runner,
            privileged_prefix=_privileged_prefix(is_root),
        ):
            return 1
    else:
        output("Skipping nginx configuration.")

    printer.step("Running production doctor")
    doctor_command = _command_as_user(
        APP_USER,
        (
            f"cd {shlex.quote(str(options.target))} && "
            f"python3 -m scripts.doctor --production --env-path {shlex.quote(rendered_env_path)} --app-dir {shlex.quote(rendered_target)}"
        ),
        is_root=is_root,
    )
    if _run_with_output(doctor_command, command_runner=command_runner, output=output, dry_run=options.dry_run).returncode != 0:
        return 1

    printer.step("Enabling and starting services")
    if start_services_choice and not options.skip_start:
        for command in (
            _privileged_command(["systemctl", "enable", "agent-api", "agent-worker", "agent-telegram"], is_root=is_root),
            _privileged_command(["systemctl", "start", "agent-api", "agent-worker", "agent-telegram"], is_root=is_root),
            _privileged_command(["systemctl", "status", "agent-api", "agent-worker", "agent-telegram", "--no-pager"], is_root=is_root),
        ):
            if _run_with_output(command, command_runner=command_runner, output=output, dry_run=options.dry_run).returncode != 0:
                return 1
    else:
        output("Skipping service enable/start.")

    printer.step("Checking API health")
    if start_services_choice and not options.skip_start and not options.dry_run:
        if not _check_api_health(output=output):
            return 1
    else:
        output("Skipping API health check.")

    printer.step("Printing final report")
    output("Agent_Sam install summary")
    output(f"Target: {rendered_target}")
    output(f"Environment file: {rendered_env_path}")
    output("Host user: agentos")
    output("Services started." if start_services_choice and not options.skip_start else "Services not started.")
    output("Installer finished successfully.")
    return 0


def _collect_production_env_updates(
    *,
    env_path: Path,
    example_path: Path,
    options: InstallOptions,
    prompt: PromptFunc,
    secret_prompt: PromptFunc,
    output: OutputFunc,
    dry_run: bool,
) -> dict[str, str] | None:
    current_values = load_env_values(env_path, example_path=example_path)
    env_updates: dict[str, str] = {}

    required_plain_keys = {
        "APP_ENV": current_values.get("APP_ENV") or "production",
        "API_HOST": current_values.get("API_HOST") or "127.0.0.1",
        "API_PORT": current_values.get("API_PORT") or "8000",
        "DATABASE_URL": current_values.get("DATABASE_URL") or "postgresql+psycopg://agent_sam_user:CHANGE_ME@127.0.0.1:5432/agent_sam",
        "REDIS_URL": current_values.get("REDIS_URL") or "redis://127.0.0.1:6379/0",
        "QDRANT_URL": current_values.get("QDRANT_URL") or "http://127.0.0.1:6333",
    }

    if dry_run:
        output(f"Dry run: would ensure {env_path} exists from {example_path}")
    for key, default_value in required_plain_keys.items():
        resolved_value = _resolve_value(
            key=key,
            current_value=current_values.get(key, ""),
            default_value=default_value,
            prompt=prompt,
            non_interactive=options.non_interactive,
            dry_run=dry_run,
            output=output,
        )
        if resolved_value is None:
            output(f"{key} must be set to a non-placeholder value for production installation.")
            return None
        env_updates[key] = resolved_value

    gateways_value = _resolve_gateways(
        current_value=current_values.get("ENABLED_GATEWAYS", "telegram"),
        prompt=prompt,
        non_interactive=options.non_interactive,
        dry_run=dry_run,
        output=output,
    )
    if gateways_value is None:
        return None
    env_updates["ENABLED_GATEWAYS"] = gateways_value

    enabled_gateways = {token.strip().lower() for token in gateways_value.split(",") if token.strip()}
    if "telegram" in enabled_gateways:
        telegram_token = _resolve_secret(
            key="TELEGRAM_BOT_TOKEN",
            current_value=current_values.get("TELEGRAM_BOT_TOKEN", ""),
            prompt=secret_prompt,
            non_interactive=options.non_interactive,
            dry_run=dry_run,
            output=output,
        )
        if telegram_token is None:
            output("TELEGRAM_BOT_TOKEN is required when telegram is enabled.")
            return None
        env_updates["TELEGRAM_BOT_TOKEN"] = telegram_token

    llm_updates = _resolve_llm_updates(
        current_values=current_values,
        prompt=prompt,
        secret_prompt=secret_prompt,
        non_interactive=options.non_interactive,
        dry_run=dry_run,
        output=output,
    )
    if llm_updates is None:
        return None
    env_updates.update(llm_updates)
    specialist_updates = _resolve_specialist_runtime_updates(
        current_values={**current_values, **env_updates},
        prompt=prompt,
        secret_prompt=secret_prompt,
        non_interactive=options.non_interactive,
        dry_run=dry_run,
        output=output,
    )
    if specialist_updates is None:
        return None
    env_updates.update(specialist_updates)
    return env_updates


def _resolve_gateways(
    *,
    current_value: str,
    prompt: PromptFunc,
    non_interactive: bool,
    dry_run: bool,
    output: OutputFunc,
) -> str | None:
    default_value = current_value or "telegram"
    if dry_run:
        output(f"Dry run: would confirm gateways (default: {default_value})")
        return default_value

    if non_interactive:
        resolved = os.environ.get("ENABLED_GATEWAYS", default_value).strip()
    else:
        resolved = prompt(f"ENABLED_GATEWAYS [{default_value}]: ").strip() or default_value

    normalized = []
    seen: set[str] = set()
    for token in resolved.split(","):
        name = token.strip().lower()
        if not name or name in seen:
            continue
        if name not in AVAILABLE_GATEWAYS:
            output(f"Unsupported gateway: {name}. Choose from: {', '.join(AVAILABLE_GATEWAYS)}")
            return None
        normalized.append(name)
        seen.add(name)
    if not normalized:
        output("At least one gateway must be enabled.")
        return None
    return ",".join(normalized)


def _resolve_llm_updates(
    *,
    current_values: dict[str, str],
    prompt: PromptFunc,
    secret_prompt: PromptFunc,
    non_interactive: bool,
    dry_run: bool,
    output: OutputFunc,
) -> dict[str, str] | None:
    default_choice = _infer_llm_provider_choice(current_values)
    if dry_run:
        provider_name = LLM_PROVIDER_OPTIONS[default_choice][0]
        output(f"Dry run: would configure LLM provider ({provider_name})")
        return {"LITELLM_MODEL": current_values.get("LITELLM_MODEL") or LLM_PROVIDER_OPTIONS[default_choice][1] or "openai/gpt-4o-mini"}

    if non_interactive:
        choice = os.environ.get("AGENT_SAM_LLM_PROVIDER", default_choice).strip() or default_choice
    else:
        output("Choose LLM provider:")
        for option, (label, _, _, _) in LLM_PROVIDER_OPTIONS.items():
            output(f"{option}. {label}")
        choice = prompt(f"LLM provider [{default_choice}]: ").strip() or default_choice

    if choice not in LLM_PROVIDER_OPTIONS:
        output("Choose a valid LLM provider option.")
        return None

    label, default_model, secret_key_name, base_url_key_name = LLM_PROVIDER_OPTIONS[choice]
    updates: dict[str, str] = {}
    if choice == "6":
        model_default = current_values.get("LITELLM_MODEL") or "openai/gpt-4o-mini"
        model_value = _resolve_value(
            key="LITELLM_MODEL",
            current_value=current_values.get("LITELLM_MODEL", ""),
            default_value=model_default,
            prompt=prompt,
            non_interactive=non_interactive,
            dry_run=False,
            output=output,
        )
        if model_value is None:
            return None
        updates["LITELLM_MODEL"] = model_value
    else:
        updates["LITELLM_MODEL"] = default_model

    if secret_key_name is not None:
        secret_value = _resolve_secret(
            key=secret_key_name,
            current_value=current_values.get(secret_key_name, ""),
            prompt=secret_prompt,
            non_interactive=non_interactive,
            dry_run=False,
            output=output,
        )
        if secret_value is None:
            return None
        updates[secret_key_name] = secret_value

    if base_url_key_name is not None:
        base_url = _resolve_value(
            key=base_url_key_name,
            current_value=current_values.get(base_url_key_name, ""),
            default_value=current_values.get(base_url_key_name) or "http://127.0.0.1:11434",
            prompt=prompt,
            non_interactive=non_interactive,
            dry_run=False,
            output=output,
        )
        if base_url is None:
            return None
        updates[base_url_key_name] = base_url
    output(f"LLM provider: {label}")
    return updates


def _infer_llm_provider_choice(current_values: dict[str, str]) -> str:
    model = current_values.get("LITELLM_MODEL", "").lower()
    if model.startswith("openrouter/") or current_values.get("OPENROUTER_API_KEY", ""):
        return "1"
    if model.startswith("anthropic/"):
        return "3"
    if model.startswith("gemini/") or model.startswith("google/"):
        return "4"
    if model.startswith("ollama/"):
        return "5"
    if current_values.get("LITELLM_API_KEY", ""):
        return "6"
    return "2"


def _resolve_specialist_runtime_updates(
    *,
    current_values: dict[str, str],
    prompt: PromptFunc,
    secret_prompt: PromptFunc,
    non_interactive: bool,
    dry_run: bool,
    output: OutputFunc,
) -> dict[str, str] | None:
    updates: dict[str, str] = {}
    openrouter_required = any(
        (current_values.get(key) or default).lower().startswith("openrouter/")
        for key, default in SPECIALIST_MODEL_DEFAULTS.items()
    ) or (current_values.get("LITELLM_MODEL") or "").lower().startswith("openrouter/")

    if openrouter_required and not current_values.get("OPENROUTER_API_KEY"):
        openrouter_key = _resolve_secret(
            key="OPENROUTER_API_KEY",
            current_value=current_values.get("OPENROUTER_API_KEY", ""),
            prompt=secret_prompt,
            non_interactive=non_interactive,
            dry_run=dry_run,
            output=output,
        )
        if openrouter_key is None:
            return None
        updates["OPENROUTER_API_KEY"] = openrouter_key

    if openrouter_required and not current_values.get("OPENROUTER_BASE_URL"):
        openrouter_base_url = _resolve_value(
            key="OPENROUTER_BASE_URL",
            current_value=current_values.get("OPENROUTER_BASE_URL", ""),
            default_value=current_values.get("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1",
            prompt=prompt,
            non_interactive=non_interactive,
            dry_run=dry_run,
            output=output,
        )
        if openrouter_base_url is None:
            return None
        updates["OPENROUTER_BASE_URL"] = openrouter_base_url

    for key, default_value in SPECIALIST_MODEL_DEFAULTS.items():
        resolved = _resolve_value(
            key=key,
            current_value=current_values.get(key, ""),
            default_value=default_value,
            prompt=prompt,
            non_interactive=non_interactive,
            dry_run=dry_run,
            output=output,
        )
        if resolved is None:
            return None
        updates[key] = resolved

    for key, default_value in SPECIALIST_NUMERIC_DEFAULTS.items():
        resolved = _resolve_value(
            key=key,
            current_value=current_values.get(key, ""),
            default_value=default_value,
            prompt=prompt,
            non_interactive=non_interactive,
            dry_run=dry_run,
            output=output,
        )
        if resolved is None:
            return None
        updates[key] = resolved

    for key, default_value in SPECIALIST_BOOL_DEFAULTS.items():
        resolved = _resolve_bool_value(
            key=key,
            current_value=current_values.get(key, ""),
            default_value=default_value,
            prompt=prompt,
            non_interactive=non_interactive,
            dry_run=dry_run,
            output=output,
        )
        if resolved is None:
            return None
        updates[key] = resolved

    return updates


def _resolve_value(
    *,
    key: str,
    current_value: str,
    default_value: str,
    prompt: PromptFunc,
    non_interactive: bool,
    dry_run: bool,
    output: OutputFunc,
) -> str | None:
    if dry_run:
        return _normalize_resolved_value(key, current_value or default_value)

    env_override = _normalize_resolved_value(key, os.environ.get(key, ""))
    normalized_current_value = _normalize_resolved_value(key, current_value)
    normalized_default_value = _normalize_resolved_value(key, default_value)
    if non_interactive:
        resolved = env_override or normalized_current_value or normalized_default_value
        if not resolved or "CHANGE_ME" in resolved:
            return None
        return resolved

    prompt_default = normalized_current_value or normalized_default_value
    while True:
        value = _normalize_resolved_value(key, prompt(f"{key} [{prompt_default}]: ").strip() or prompt_default)
        if value and "CHANGE_ME" not in value:
            return value
        output(f"{key} must be set to a real value and cannot keep the placeholder.")


def _normalize_resolved_value(key: str, raw_value: str) -> str:
    normalized = raw_value.strip()
    if not normalized:
        return ""
    if key.endswith("_URL"):
        return _strip_wrapping_delimiters(normalized)
    return normalized


def _strip_wrapping_delimiters(raw_value: str) -> str:
    normalized = raw_value.strip()
    while len(normalized) >= 2:
        if normalized[0] == normalized[-1] and normalized[0] in {"'", '"'}:
            normalized = normalized[1:-1].strip()
            continue
        if normalized[0] == "[" and normalized[-1] == "]" and "://" in normalized:
            normalized = normalized[1:-1].strip()
            continue
        break
    return normalized


def _resolve_secret(
    *,
    key: str,
    current_value: str,
    prompt: PromptFunc,
    non_interactive: bool,
    dry_run: bool,
    output: OutputFunc,
) -> str | None:
    if dry_run:
        output(f"Dry run: would save {key}")
        return current_value or os.environ.get(key, "") or "<required>"

    env_override = os.environ.get(key, "").strip()
    if non_interactive:
        resolved = env_override or current_value
        if not resolved:
            return None
        output(f"{key}: saved")
        return resolved

    prompt_suffix = " [press Enter to keep existing]" if current_value else ""
    secret_value = prompt(f"{key}{prompt_suffix}: ").strip()
    if secret_value:
        output(f"{key}: saved")
        return secret_value
    if current_value:
        output(f"{key}: saved")
        return current_value
    return None


def _resolve_bool_value(
    *,
    key: str,
    current_value: str,
    default_value: str,
    prompt: PromptFunc,
    non_interactive: bool,
    dry_run: bool,
    output: OutputFunc,
) -> str | None:
    default_bool = _parse_bool(current_value or default_value)
    if dry_run:
        output(f"Dry run: would confirm {key} ({'true' if default_bool else 'false'})")
        return current_value or default_value
    if non_interactive:
        env_override = os.environ.get(key, "").strip()
        if env_override:
            parsed = _parse_bool(env_override)
            if parsed is None:
                return None
            return "true" if parsed else "false"
        return current_value or default_value
    suffix = "[Y/n]" if default_bool else "[y/N]"
    while True:
        raw_value = prompt(f"{key} {suffix} ").strip().lower()
        if not raw_value:
            return "true" if default_bool else "false"
        if raw_value in {"y", "yes", "true", "1", "on"}:
            return "true"
        if raw_value in {"n", "no", "false", "0", "off"}:
            return "false"
        output("Answer yes or no.")


def _parse_bool(raw_value: str) -> bool | None:
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def _resolve_server_name(
    *,
    env_values: dict[str, str],
    non_interactive: bool,
    prompt: PromptFunc,
    dry_run: bool,
) -> str | None:
    default_server_name = env_values.get("AGENT_SAM_SERVER_NAME", "agent-sam.example.com")
    if dry_run:
        return default_server_name
    env_override = os.environ.get("AGENT_SAM_SERVER_NAME", "").strip()
    if non_interactive:
        return env_override or default_server_name
    return prompt(f"nginx server_name [{default_server_name}]: ").strip() or default_server_name


def _prepare_dependency_services(
    env_values: dict[str, str],
    *,
    target: Path,
    options: InstallOptions,
    prompt: PromptFunc,
    output: OutputFunc,
    command_runner: CommandRunner,
    is_root: bool,
) -> dict[str, str] | None:
    if options.dry_run:
        output("Dry run: would verify local dependency services and start bundled docker compose services if they are missing.")
        return env_values

    if not _uses_loopback_dependency_endpoints(env_values):
        output("Dependency endpoints are not loopback-only; skipping bundled local dependency bootstrap.")
        return env_values

    dependency_statuses = _dependency_service_statuses(env_values)
    missing_dependency_keys = [key for key, reachable in dependency_statuses.items() if not reachable]
    if not missing_dependency_keys:
        output("Local dependency services are reachable.")
        return env_values

    missing_dependency_services = [DEPENDENCY_SERVICE_CONFIG[key][0] for key in missing_dependency_keys]
    output(
        "Local dependency services are not reachable on the configured loopback URLs: "
        + ", ".join(missing_dependency_services)
        + "."
    )
    if not _ensure_docker_available(
        prompt=prompt,
        output=output,
        command_runner=command_runner,
        is_root=is_root,
        non_interactive=options.non_interactive,
    ):
        output(
            "Install Docker and run "
            f"docker compose -f {target.as_posix()}/docker-compose.yml up -d {' '.join(missing_dependency_services)}, "
            "or point DATABASE_URL, REDIS_URL, and QDRANT_URL at reachable services."
        )
        return None

    use_bundled_stack = True
    if not options.non_interactive:
        use_bundled_stack = _prompt_yes_no(
            prompt,
            "Start bundled Docker services for the missing dependencies and use their local default URLs where needed?",
            default=True,
        )
    if not use_bundled_stack:
        output(
            f"Run docker compose -f {target.as_posix()}/docker-compose.yml up -d {' '.join(missing_dependency_services)}, "
            "or point DATABASE_URL, REDIS_URL, and QDRANT_URL at reachable services before retrying."
        )
        return None

    bundled_env_values = dict(env_values)
    for key in missing_dependency_keys:
        bundled_env_values[key] = DEPENDENCY_SERVICE_CONFIG[key][1]
    output(
        "Using bundled local dependency stack defaults for " + ", ".join(missing_dependency_keys) + "."
    )

    compose_base_command = _resolve_docker_compose_base_command(command_runner=command_runner, is_root=is_root)
    if compose_base_command is None:
        output("Docker Compose is unavailable after Docker setup. Install the compose plugin and retry.")
        return None

    output("Starting bundled dependency services: " + ", ".join(missing_dependency_services) + ".")
    compose_command = _privileged_command(
        [
            *compose_base_command,
            "-f",
            f"{target.as_posix()}/docker-compose.yml",
            "up",
            "-d",
            *missing_dependency_services,
        ],
        is_root=is_root,
    )
    if _run_with_output(compose_command, command_runner=command_runner, output=output, dry_run=False).returncode != 0:
        return None

    if not _wait_for_local_dependency_services(bundled_env_values, output=output):
        output("Bundled local dependency services did not become reachable in time.")
        return None

    output("Bundled local dependency services are reachable.")
    return bundled_env_values


def _uses_loopback_dependency_endpoints(env_values: dict[str, str]) -> bool:
    endpoints = (
        _extract_host_port(env_values.get("DATABASE_URL", ""), default_port=5432),
        _extract_host_port(env_values.get("REDIS_URL", ""), default_port=6379),
        _extract_host_port(env_values.get("QDRANT_URL", ""), default_port=6333),
    )
    return all(endpoint is not None and _is_loopback_host(endpoint[0]) for endpoint in endpoints)


def _local_dependency_services_reachable(env_values: dict[str, str]) -> bool:
    return all(_dependency_service_statuses(env_values).values())


def _dependency_service_statuses(env_values: dict[str, str]) -> dict[str, bool]:
    statuses: dict[str, bool] = {}
    for key, (_, _, default_port) in DEPENDENCY_SERVICE_CONFIG.items():
        endpoint = _extract_host_port(env_values.get(key, ""), default_port=default_port)
        statuses[key] = endpoint is not None and _tcp_endpoint_reachable(*endpoint)
    return statuses


def _wait_for_local_dependency_services(
    env_values: dict[str, str],
    *,
    output: OutputFunc,
    timeout_seconds: float = 60.0,
) -> bool:
    output("Waiting for bundled local dependency services to become reachable...")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _local_dependency_services_reachable(env_values):
            return True
        time.sleep(2.0)
    return False


def _docker_available(*, command_runner: CommandRunner, is_root: bool) -> bool:
    try:
        result = command_runner(_privileged_command(["docker", "version"], is_root=is_root), capture_output=True)
    except FileNotFoundError:
        return False
    return result.returncode == 0


def _ensure_docker_available(
    *,
    prompt: PromptFunc,
    output: OutputFunc,
    command_runner: CommandRunner,
    is_root: bool,
    non_interactive: bool,
) -> bool:
    if _docker_available(command_runner=command_runner, is_root=is_root):
        return True

    docker_installed = shutil.which("docker") is not None
    if detect_os_name() != "Linux":
        output("Automatic Docker setup is only supported on Linux hosts.")
        return False

    should_configure = True
    if not non_interactive:
        question = "Start Docker now?" if docker_installed else "Install Docker now?"
        should_configure = _prompt_yes_no(prompt, question, default=True)
    if not should_configure:
        return False

    if not docker_installed:
        if not _install_docker_packages(output=output, command_runner=command_runner, is_root=is_root):
            return False

    if not _start_docker_service(output=output, command_runner=command_runner, is_root=is_root):
        return False

    if not _docker_available(command_runner=command_runner, is_root=is_root):
        output("Docker is still unavailable after the automatic setup attempt.")
        return False
    output("Docker is available.")
    return True


def _install_docker_packages(*, output: OutputFunc, command_runner: CommandRunner, is_root: bool) -> bool:
    package_manager = _detect_linux_package_manager()
    if package_manager is None:
        output("Automatic Docker installation is not supported on this Linux distribution.")
        return False

    output(f"Installing Docker packages with {package_manager}.")

    if package_manager == "apt-get":
        if _run_with_output(
            _privileged_command(["apt-get", "update"], is_root=is_root),
            command_runner=command_runner,
            output=output,
            dry_run=False,
        ).returncode != 0:
            return False
        install_variants = (
            ["docker.io", "docker-compose-plugin"],
            ["docker.io", "docker-compose-v2"],
            ["docker.io", "docker-compose"],
        )
    elif package_manager == "dnf":
        install_variants = (
            ["docker", "docker-compose-plugin"],
            ["moby-engine", "docker-compose-plugin"],
            ["docker", "docker-compose"],
        )
    else:
        install_variants = (
            ["docker", "docker-compose-plugin"],
            ["docker", "docker-compose"],
        )

    package_manager_command = [package_manager, "install", "-y"]
    for packages in install_variants:
        if _run_with_output(
            _privileged_command([*package_manager_command, *packages], is_root=is_root),
            command_runner=command_runner,
            output=output,
            dry_run=False,
        ).returncode == 0:
            return True
    return False


def _start_docker_service(*, output: OutputFunc, command_runner: CommandRunner, is_root: bool) -> bool:
    if shutil.which("systemctl") is not None:
        return (
            _run_with_output(
                _privileged_command(["systemctl", "enable", "--now", "docker"], is_root=is_root),
                command_runner=command_runner,
                output=output,
                dry_run=False,
            ).returncode
            == 0
        )
    if shutil.which("service") is not None:
        return (
            _run_with_output(
                _privileged_command(["service", "docker", "start"], is_root=is_root),
                command_runner=command_runner,
                output=output,
                dry_run=False,
            ).returncode
            == 0
        )
    return True


def _detect_linux_package_manager() -> str | None:
    for candidate in ("apt-get", "dnf", "yum"):
        if shutil.which(candidate) is not None:
            return candidate
    return None


def _resolve_docker_compose_base_command(*, command_runner: CommandRunner, is_root: bool) -> list[str] | None:
    for candidate in (["docker", "compose"], ["docker-compose"]):
        try:
            result = command_runner(_privileged_command([*candidate, "version"], is_root=is_root), capture_output=True)
        except FileNotFoundError:
            continue
        if result.returncode == 0:
            return candidate
    return None


def _extract_host_port(raw_value: str, *, default_port: int) -> tuple[str, int] | None:
    if not raw_value:
        return None
    parsed = urlparse(_strip_wrapping_delimiters(raw_value))
    host = parsed.hostname
    if not host:
        return None
    return host, parsed.port or default_port


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    return normalized in {"127.0.0.1", "localhost", "::1"}


def _tcp_endpoint_reachable(host: str, port: int, *, timeout_seconds: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True
    except OSError:
        return False


def _run_with_output(
    command: Sequence[str],
    *,
    command_runner: CommandRunner,
    output: OutputFunc,
    dry_run: bool,
) -> subprocess.CompletedProcess[str]:
    output(f"Running: {format_command(command)}")
    if dry_run:
        output("Dry run: command not executed")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    result = command_runner(command, capture_output=True)
    if result.stdout:
        output(result.stdout.strip())
    if result.returncode != 0:
        if result.stderr:
            output(result.stderr.strip())
        output(f"Command failed: {format_command(command)}")
    return result


def _privileged_prefix(is_root: bool) -> tuple[str, ...]:
    return () if is_root else ("sudo",)


def _privileged_command(command: Sequence[str], *, is_root: bool) -> list[str]:
    return [*_privileged_prefix(is_root), *command]


def _command_as_user(user: str, shell_command: str, *, is_root: bool) -> list[str]:
    if is_root and shutil.which("runuser") is not None:
        return ["runuser", "-u", user, "--", "bash", "-lc", shell_command]
    if is_root and shutil.which("su") is not None:
        return ["su", "-", user, "-c", f"bash -lc {shlex.quote(shell_command)}"]
    return ["sudo", "-u", user, "-H", "bash", "-lc", shell_command]


def _is_root_user() -> bool:
    if hasattr(os, "geteuid"):
        return os.geteuid() == 0
    return False


def _prompt_yes_no(prompt: PromptFunc, question: str, *, default: bool) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        raw_value = prompt(f"{question} {suffix} ").strip().lower()
        if not raw_value:
            return default
        if raw_value in {"y", "yes"}:
            return True
        if raw_value in {"n", "no"}:
            return False


def _check_api_health(*, output: OutputFunc) -> bool:
    try:
        with urlopen("http://127.0.0.1:8000/health", timeout=5) as response:
            if response.status >= 400:
                raise URLError(f"HTTP {response.status}")
    except Exception as exc:
        output(f"API health check failed: {exc}")
        return False
    output("API health check passed: http://127.0.0.1:8000/health")
    return True


if __name__ == "__main__":
    run()