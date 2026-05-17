from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib
import importlib.metadata
import importlib.util
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import redis
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import func, inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agents.registry import GENERATED_CONFIG_ROOT, GENERATED_PROMPTS_ROOT, list_agents, validate_agent_profiles
from app.asyncio_compat import configure_windows_event_loop_policy
from app.config import Settings
from db.memory_service import MemorySearchRequest, search_memories
from db.models import Task, User, Workspace
from db.skill_service import load_skill_definitions
from gateways.registry import build_enabled_gateway_registry
from gateways.runtime_logging import redact_sensitive_text
from scripts.common import ENV_PATH, LOCAL_DEV_FIX_COMMAND, PROJECT_ROOT, detect_os_name, python_version_text, run_subprocess
from scripts.env_writer import ensure_env_file, find_duplicate_keys
from scripts.install import SPECIALIST_BOOL_DEFAULTS, SPECIALIST_MODEL_DEFAULTS, SPECIALIST_NUMERIC_DEFAULTS
from services.model_router import infer_provider
from tools.shell_risk_rules import BLOCKED_RULES, DANGEROUS_RULES, HIGH_RISK_RULES, MEDIUM_RISK_RULES, SAFE_RULES


PRODUCTION_APP_DIR = Path("/opt/agent-sam")
PRODUCTION_SYSTEMD_DIR = Path("/etc/systemd/system")
PRODUCTION_SERVICE_UNIT_NAMES = (
    "agent-api.service",
    "agent-worker.service",
    "agent-telegram.service",
)
PRODUCTION_NGINX_CONFIG_PATHS = (
    Path("/etc/nginx/sites-available/agent-api.conf"),
    Path("/etc/nginx/conf.d/agent-api.conf"),
)
PRODUCTION_APP_USER = "agentos"
PRODUCTION_API_HEALTH_URL = "http://127.0.0.1:8000/health"


@dataclass(frozen=True)
class DoctorCheck:
    status: str
    message: str


def run() -> None:
    parser = argparse.ArgumentParser(description="Run Agent_Sam diagnostics.")
    parser.add_argument("--env-path", type=Path, default=ENV_PATH)
    parser.add_argument("--production", action="store_true", help="Run Linux production deployment checks.")
    parser.add_argument("--fix", action="store_true", help="Apply safe automatic fixes before re-running checks.")
    parser.add_argument("--non-interactive", action="store_true", help="Reserved for automation compatibility.")
    parser.add_argument("--app-dir", type=Path, default=PRODUCTION_APP_DIR)
    parser.add_argument("--systemd-dir", type=Path, default=PRODUCTION_SYSTEMD_DIR)
    arguments = parser.parse_args()
    configure_windows_event_loop_policy()
    exit_code = asyncio.run(
        run_doctor(
            arguments.env_path,
            production=arguments.production,
            fix=arguments.fix,
            non_interactive=arguments.non_interactive,
            app_dir=arguments.app_dir,
            systemd_dir=arguments.systemd_dir,
        )
    )
    raise SystemExit(exit_code)


async def run_doctor(
    env_path: Path = ENV_PATH,
    *,
    production: bool = False,
    fix: bool = False,
    non_interactive: bool = False,
    app_dir: Path = PRODUCTION_APP_DIR,
    systemd_dir: Path = PRODUCTION_SYSTEMD_DIR,
    output=print,
) -> int:
    checks: list[DoctorCheck] = []
    del non_interactive
    if fix:
        checks.extend(_apply_safe_fixes(env_path, production=production, app_dir=app_dir))
    raw_env = _load_raw_env(env_path)
    checks.extend(_check_python_version())
    checks.extend(_check_importable_install())
    checks.extend(_check_env_file(env_path, production=production))
    checks.extend(_check_env_duplicates(env_path))
    checks.extend(_check_required_env_values(raw_env, production=production))
    checks.extend(_check_llm_config(raw_env, production=production))
    checks.extend(_check_specialist_assets())

    if production:
        checks.extend(_check_production_runtime_account())
        checks.extend(_check_app_directory(app_dir))

    enabled_gateways = _parse_enabled_gateways(raw_env.get("ENABLED_GATEWAYS", ""))
    if "telegram" in enabled_gateways:
        if raw_env.get("TELEGRAM_BOT_TOKEN", "").strip():
            checks.append(DoctorCheck("OK", "TELEGRAM_BOT_TOKEN is set"))
        else:
            checks.append(DoctorCheck("FAIL", "TELEGRAM_BOT_TOKEN is missing"))
            checks.append(DoctorCheck("FIX", _fix_command_for_env_key("TELEGRAM_BOT_TOKEN", production=production)))

    if production:
        checks.extend(_check_systemctl_available())
        checks.extend(_check_systemd_units(systemd_dir))
        checks.extend(_check_systemd_service_states())
        checks.extend(_check_nginx_config())
        checks.extend(_check_api_health())
    else:
        checks.extend(_check_docker_available())
    checks.extend(_check_runtime_imports())
    checks.extend(_check_skills_and_rules())

    settings: Settings | None = None
    try:
        settings = Settings(_env_file=env_path)
    except Exception as exc:
        checks.append(DoctorCheck("FAIL", f"Settings parsing failed: {redact_sensitive_text(str(exc))}"))

    if settings is not None:
        checks.extend(await _check_database_stack(settings, production=production))

    for check in checks:
        output(f"[{check.status}] {check.message}")

    return 1 if any(check.status == "FAIL" for check in checks) else 0


def _load_raw_env(env_path: Path) -> dict[str, str]:
    if not env_path.exists():
        return {}

    values: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", maxsplit=1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _apply_safe_fixes(env_path: Path, *, production: bool, app_dir: Path) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    if not env_path.exists():
        example_path = env_path.parent / ".env.example"
        resolved_example = example_path if example_path.exists() else PROJECT_ROOT / ".env.example"
        ensure_env_file(env_path, example_path=resolved_example)
        checks.append(DoctorCheck("FIXED", f"Created .env at {env_path}"))

    if production and not app_dir.exists():
        app_dir.mkdir(parents=True, exist_ok=True)
        checks.append(DoctorCheck("FIXED", f"Created app directory {_display_path(app_dir)}"))
    return checks


def _check_env_duplicates(env_path: Path) -> list[DoctorCheck]:
    if not env_path.exists():
        return []

    duplicates = find_duplicate_keys(env_path)
    if not duplicates:
        return [DoctorCheck("OK", "No duplicate .env keys found")]
    duplicate_text = ", ".join(duplicates)
    return [
        DoctorCheck("FAIL", f"Duplicate .env keys found: {duplicate_text}"),
        DoctorCheck("FIX", "Remove duplicate keys from .env so each setting appears only once"),
    ]


def _display_path(path: Path) -> str:
    if path.drive:
        return str(path)
    return path.as_posix()


def _check_python_version() -> list[DoctorCheck]:
    spec = _read_requires_python()
    required_version = _minimum_python_version(spec)
    current = tuple(int(part) for part in python_version_text().split(".")[:3])
    checks = [DoctorCheck("OK", f"OS {detect_os_name()}"), DoctorCheck("OK", f"Python {python_version_text()}")]
    if current >= required_version:
        checks.append(DoctorCheck("OK", f"Python satisfies requires-python {spec}"))
    else:
        checks.append(DoctorCheck("FAIL", f"Python {python_version_text()} does not satisfy {spec}"))
        checks.append(DoctorCheck("FIX", f"Use Python {spec} before running Agent_Sam"))
    return checks


def _read_requires_python() -> str:
    pyproject_text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for line in pyproject_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("requires-python"):
            return stripped.split("=", maxsplit=1)[1].strip().strip('"')
    return ">=3.10"


def _minimum_python_version(spec: str) -> tuple[int, int, int]:
    normalized = spec.strip()
    if normalized.startswith(">="):
        parts = normalized[2:].split(".")
        numbers = [int(part) for part in parts]
        while len(numbers) < 3:
            numbers.append(0)
        return tuple(numbers[:3])
    return (3, 10, 0)


def _check_importable_install() -> list[DoctorCheck]:
    try:
        version = importlib.metadata.version("agent-sam")
        return [DoctorCheck("OK", f"Editable install/package import works (agent-sam {version})")]
    except importlib.metadata.PackageNotFoundError:
        if importlib.util.find_spec("gateways") is not None:
            return [DoctorCheck("OK", "Repository modules are importable from the workspace")]
        return [
            DoctorCheck("FAIL", "Editable install is missing and repository modules are not importable"),
            DoctorCheck("FIX", "Run: python -m pip install -e .[dev]"),
        ]


def _check_env_file(env_path: Path, *, production: bool = False) -> list[DoctorCheck]:
    if env_path.exists():
        return [DoctorCheck("OK", f".env found at {env_path}")]
    fix_message = (
        "Create .env from .env.example and configure production secrets"
        if production
        else "Run: python -m scripts.setup"
    )
    return [
        DoctorCheck("FAIL", f".env is missing at {env_path}"),
        DoctorCheck("FIX", fix_message),
    ]


def _check_required_env_values(raw_env: dict[str, str], *, production: bool = False) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    required_keys = (
        "DATABASE_URL",
        "REDIS_URL",
        "QDRANT_URL",
        "DEFAULT_WORKSPACE_ID",
        "DEFAULT_USER_ID",
        "ENABLED_GATEWAYS",
    )
    for key in required_keys:
        if _has_effective_value(raw_env.get(key, "")):
            checks.append(DoctorCheck("OK", f"{key} is set"))
        else:
            checks.append(DoctorCheck("FAIL", f"{key} is missing"))
            checks.append(DoctorCheck("FIX", _fix_command_for_env_key(key, production=production)))
    return checks


def _check_llm_config(raw_env: dict[str, str], *, production: bool = False) -> list[DoctorCheck]:
    model = raw_env.get("LITELLM_MODEL", "").strip()
    if not _has_effective_value(model):
        return [
            DoctorCheck("FAIL", "LITELLM_MODEL is missing"),
            DoctorCheck("FIX", _llm_fix_message("LITELLM_MODEL", production=production)),
        ]

    provider = _infer_llm_provider(model)
    checks = [DoctorCheck("OK", f"LITELLM_MODEL is set ({model})")]

    model_keys = {"LITELLM_MODEL": model, **{key: raw_env.get(key, default) for key, default in SPECIALIST_MODEL_DEFAULTS.items()}}
    for key, value in model_keys.items():
        if not value:
            continue
        validation_error = _validate_model_name(value)
        if validation_error is None:
            checks.append(DoctorCheck("OK", f"{key} syntax looks valid ({value})"))
        else:
            checks.append(DoctorCheck("FAIL", f"{key} invalid: {validation_error}"))
            checks.append(DoctorCheck("FIX", f"Set {key} to a valid provider/model string"))

    for key, default_value in SPECIALIST_NUMERIC_DEFAULTS.items():
        raw_value = raw_env.get(key, default_value)
        try:
            parsed = float(raw_value)
        except ValueError:
            checks.append(DoctorCheck("FAIL", f"{key} must be numeric (got {raw_value})"))
            checks.append(DoctorCheck("FIX", _llm_fix_message(key, production=production)))
            continue
        if parsed <= 0:
            checks.append(DoctorCheck("FAIL", f"{key} must be greater than zero"))
            checks.append(DoctorCheck("FIX", _llm_fix_message(key, production=production)))
        else:
            checks.append(DoctorCheck("OK", f"{key} is set to {parsed}"))

    for key, default_value in SPECIALIST_BOOL_DEFAULTS.items():
        raw_value = (raw_env.get(key) or default_value).strip().lower()
        if raw_value in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
            checks.append(DoctorCheck("OK", f"{key} is set ({raw_value})"))
        else:
            checks.append(DoctorCheck("FAIL", f"{key} must be a boolean value"))
            checks.append(DoctorCheck("FIX", f"Set {key} to true or false in .env"))

    if provider == "openrouter" or any(infer_provider(value) == "openrouter" for value in model_keys.values() if value):
        if _any_effective_values(raw_env, "OPENROUTER_API_KEY", "LITELLM_API_KEY"):
            checks.append(DoctorCheck("OK", "OpenRouter credentials are configured"))
        else:
            checks.append(DoctorCheck("FAIL", "OpenRouter credentials are missing"))
            checks.append(DoctorCheck("FIX", _llm_fix_message("OPENROUTER_API_KEY", production=production)))

    if provider == "openai":
        if _any_effective_values(raw_env, "OPENAI_API_KEY", "LITELLM_API_KEY"):
            checks.append(DoctorCheck("OK", "OpenAI-compatible API credentials are configured"))
        else:
            checks.append(DoctorCheck("FAIL", "OpenAI-compatible API credentials are missing"))
            checks.append(DoctorCheck("FIX", _llm_fix_message("OPENAI_API_KEY", production=production)))
    elif provider == "anthropic":
        if _any_effective_values(raw_env, "ANTHROPIC_API_KEY", "LITELLM_API_KEY"):
            checks.append(DoctorCheck("OK", "Anthropic API credentials are configured"))
        else:
            checks.append(DoctorCheck("FAIL", "Anthropic API credentials are missing"))
            checks.append(DoctorCheck("FIX", _llm_fix_message("ANTHROPIC_API_KEY", production=production)))
    elif provider == "gemini":
        if _any_effective_values(raw_env, "GEMINI_API_KEY", "LITELLM_API_KEY"):
            checks.append(DoctorCheck("OK", "Gemini API credentials are configured"))
        else:
            checks.append(DoctorCheck("FAIL", "Gemini API credentials are missing"))
            checks.append(DoctorCheck("FIX", _llm_fix_message("GEMINI_API_KEY", production=production)))
    elif provider == "ollama":
        if _has_effective_value(raw_env.get("OLLAMA_BASE_URL", "")):
            checks.append(DoctorCheck("OK", "OLLAMA_BASE_URL is set"))
        else:
            checks.append(DoctorCheck("FAIL", "OLLAMA_BASE_URL is missing"))
            checks.append(DoctorCheck("FIX", _llm_fix_message("OLLAMA_BASE_URL", production=production)))
    else:
        if _any_effective_values(raw_env, "LITELLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY") or _has_effective_value(raw_env.get("OLLAMA_BASE_URL", "")):
            checks.append(DoctorCheck("OK", "A provider credential or base URL is configured for the custom LiteLLM model"))
        else:
            checks.append(DoctorCheck("FAIL", "No provider credential or base URL is configured for the custom LiteLLM model"))
            checks.append(DoctorCheck("FIX", "Set LITELLM_API_KEY, a provider-specific API key, or OLLAMA_BASE_URL in .env"))
    return checks


def _fix_command_for_env_key(key: str, *, production: bool = False) -> str:
    if production:
        if key in {"DEFAULT_WORKSPACE_ID", "DEFAULT_USER_ID"}:
            return "Run bash deployment/linux/bootstrap-app.sh to seed defaults, or set DEFAULT_USER_ID and DEFAULT_WORKSPACE_ID manually"
        if key == "ENABLED_GATEWAYS":
            return "Run: python -m scripts.setup --production"
        if key == "TELEGRAM_BOT_TOKEN":
            return "Set TELEGRAM_BOT_TOKEN in .env before starting the Telegram service"
        return f"Set {key} in .env"
    if key in {"DEFAULT_WORKSPACE_ID", "DEFAULT_USER_ID"}:
        return "Run: python scripts/seed_dev.py"
    if key == "ENABLED_GATEWAYS":
        return "Run: python -m gateways.setup"
    if key == "TELEGRAM_BOT_TOKEN":
        return "Run: python -m gateways.setup"
    return "Run: python -m scripts.setup"


def _parse_enabled_gateways(raw_value: str) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for token in raw_value.split(","):
        name = token.strip().lower()
        if not name or name in seen:
            continue
        normalized.append(name)
        seen.add(name)
    return tuple(normalized)


def _has_effective_value(raw_value: str) -> bool:
    value = raw_value.strip()
    return bool(value and "CHANGE_ME" not in value)


def _any_effective_values(raw_env: dict[str, str], *keys: str) -> bool:
    return any(_has_effective_value(raw_env.get(key, "")) for key in keys)


def _infer_llm_provider(model: str) -> str:
    normalized = model.strip().lower()
    if normalized.startswith("openrouter/"):
        return "openrouter"
    if normalized.startswith(("openai/", "gpt-", "o1", "o3")):
        return "openai"
    if normalized.startswith("anthropic/"):
        return "anthropic"
    if normalized.startswith(("gemini/", "google/")):
        return "gemini"
    if normalized.startswith("ollama/"):
        return "ollama"
    return "custom"


def _llm_fix_message(key: str, *, production: bool) -> str:
    if key == "LITELLM_MODEL":
        return "Set LITELLM_MODEL in .env to the LiteLLM provider/model you want to use"
    if key == "OPENROUTER_API_KEY":
        return "Set OPENROUTER_API_KEY or LITELLM_API_KEY in .env"
    if key == "OPENAI_API_KEY":
        return "Set OPENAI_API_KEY or LITELLM_API_KEY in .env"
    if key == "ANTHROPIC_API_KEY":
        return "Set ANTHROPIC_API_KEY or LITELLM_API_KEY in .env"
    if key == "GEMINI_API_KEY":
        return "Set GEMINI_API_KEY or LITELLM_API_KEY in .env"
    if key == "OLLAMA_BASE_URL":
        return "Set OLLAMA_BASE_URL in .env to the Ollama host URL"
    if key in SPECIALIST_NUMERIC_DEFAULTS:
        return f"Set {key} in .env to a positive numeric value"
    return f"Set {key} in .env"


def _check_docker_available() -> list[DoctorCheck]:
    try:
        result = run_subprocess(["docker", "version"], capture_output=True)
    except FileNotFoundError:
        result = None

    if result is not None and result.returncode == 0:
        return [DoctorCheck("OK", "Docker is available")]
    return [
        DoctorCheck("FAIL", "Docker is unavailable or the daemon is not running"),
        DoctorCheck("FIX", "Start Docker Desktop or the docker daemon for local development"),
    ]


def _check_runtime_imports() -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    for module_name, label in (
        ("workers.main", "Worker runtime imports"),
        ("workers.tasks", "Worker task handler imports"),
        ("langgraph", "LangGraph import works"),
        ("litellm", "LiteLLM import works"),
    ):
        try:
            importlib.import_module(module_name)
        except Exception as exc:
            checks.append(DoctorCheck("FAIL", f"{label}: {redact_sensitive_text(str(exc))}"))
        else:
            checks.append(DoctorCheck("OK", label))
    return checks


def _check_skills_and_rules() -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    try:
        definitions = load_skill_definitions(PROJECT_ROOT / "skills")
    except Exception as exc:
        checks.append(DoctorCheck("FAIL", f"Skills failed to load: {redact_sensitive_text(str(exc))}"))
    else:
        checks.append(DoctorCheck("OK", f"Skills loaded from /skills ({len(definitions)} definitions)"))

    if all((BLOCKED_RULES, DANGEROUS_RULES, HIGH_RISK_RULES, MEDIUM_RISK_RULES, SAFE_RULES)):
        checks.append(DoctorCheck("OK", "Shell tool risk rules loaded"))
    else:
        checks.append(DoctorCheck("FAIL", "Shell tool risk rules are missing"))
    return checks


def _check_specialist_assets() -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    try:
        validate_agent_profiles()
        profiles = list_agents()
    except Exception as exc:
        checks.append(DoctorCheck("FAIL", f"Agent profiles failed to load: {redact_sensitive_text(str(exc))}"))
    else:
        checks.append(DoctorCheck("OK", f"Agent profiles loaded ({len(profiles)} profiles)"))

    if GENERATED_CONFIG_ROOT.exists() and GENERATED_PROMPTS_ROOT.exists():
        checks.append(DoctorCheck("OK", "Generated agent directories exist"))
    else:
        checks.append(DoctorCheck("FAIL", "Generated agent directories are missing"))
        checks.append(DoctorCheck("FIX", "Create agents/configs/generated and agents/prompts/generated"))
    return checks


async def _check_database_stack(settings: Settings) -> list[DoctorCheck]:
    return await _check_database_stack(settings, production=False)


async def _check_database_stack(settings: Settings, *, production: bool) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    engine = create_async_engine(settings.database_url, future=True, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    postgres_ready = False

    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        checks.append(DoctorCheck("FAIL", f"Postgres reachable: {redact_sensitive_text(str(exc))}"))
        checks.append(DoctorCheck("FIX", _database_fix_message("postgres", production=production)))
    else:
        postgres_ready = True
        checks.append(DoctorCheck("OK", "Postgres reachable"))

    checks.extend(_check_redis(settings, production=production))
    checks.extend(_check_qdrant(settings, production=production))

    if postgres_ready:
        checks.extend(await _check_alembic_revision(engine))
        checks.extend(await _check_async_session(session_factory))
        checks.extend(await _check_users_and_workspaces(session_factory, production=production))
        checks.extend(await _check_dry_run_task(session_factory, settings, production=production))
        checks.extend(await _check_memory_query(session_factory, settings, production=production))
        checks.extend(await _check_specialist_tables(engine))
        checks.extend(_check_gateway_registry(settings, session_factory))

    await engine.dispose()
    return checks


def _database_fix_message(service_name: str, *, production: bool) -> str:
    if not production:
        if service_name == "postgres":
            return f"Run: {LOCAL_DEV_FIX_COMMAND}"
        if service_name == "redis":
            return "Start redis with docker compose up -d redis"
        return "Start qdrant with docker compose up -d qdrant"

    if service_name == "postgres":
        return "Verify DATABASE_URL points to the production PostgreSQL instance and that network access is allowed"
    if service_name == "redis":
        return "Verify REDIS_URL points to the production Redis instance and that network access is allowed"
    return "Verify QDRANT_URL points to the production Qdrant instance and that network access is allowed"


def _check_redis(settings: Settings, *, production: bool) -> list[DoctorCheck]:
    try:
        client = redis.Redis.from_url(settings.redis_url)
        client.ping()
    except Exception as exc:
        return [
            DoctorCheck("FAIL", f"Redis reachable: {redact_sensitive_text(str(exc))}"),
            DoctorCheck("FIX", _database_fix_message("redis", production=production)),
        ]
    return [DoctorCheck("OK", "Redis reachable")]


def _check_qdrant(settings: Settings, *, production: bool) -> list[DoctorCheck]:
    qdrant_url = settings.qdrant_url.rstrip("/") + "/collections"
    try:
        with urlopen(qdrant_url, timeout=5) as response:
            if response.status >= 400:
                raise URLError(f"HTTP {response.status}")
    except Exception as exc:
        return [
            DoctorCheck("FAIL", f"Qdrant reachable: {redact_sensitive_text(str(exc))}"),
            DoctorCheck("FIX", _database_fix_message("qdrant", production=production)),
        ]
    return [DoctorCheck("OK", "Qdrant reachable")]


async def _check_alembic_revision(engine) -> list[DoctorCheck]:
    try:
        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        script = ScriptDirectory.from_config(config)
        head = script.get_current_head()
        async with engine.connect() as connection:
            result = await connection.execute(text("SELECT version_num FROM alembic_version"))
            current = result.scalar_one_or_none()
    except Exception as exc:
        return [DoctorCheck("FAIL", f"Alembic revision check failed: {redact_sensitive_text(str(exc))}")]
    if current == head:
        return [DoctorCheck("OK", f"Alembic current revision matches head ({head})")]
    return [
        DoctorCheck("FAIL", f"Alembic revision mismatch: current={current}, head={head}"),
        DoctorCheck("FIX", "Run: python -m alembic upgrade head"),
    ]


async def _check_async_session(session_factory: async_sessionmaker) -> list[DoctorCheck]:
    try:
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:
        return [DoctorCheck("FAIL", f"Async SQLAlchemy session failed: {redact_sensitive_text(str(exc))}")]
    return [DoctorCheck("OK", "Async SQLAlchemy session works")]


async def _check_users_and_workspaces(session_factory: async_sessionmaker, *, production: bool) -> list[DoctorCheck]:
    async with session_factory() as session:
        user_count = int(await session.scalar(select(func.count(User.id))) or 0)
        workspace_count = int(await session.scalar(select(func.count(Workspace.id))) or 0)
    if user_count > 0 and workspace_count > 0:
        return [DoctorCheck("OK", f"Users/workspaces query works ({user_count} users, {workspace_count} workspaces)")]
    fix_message = (
        "Create the required production user and workspace rows, then set DEFAULT_USER_ID and DEFAULT_WORKSPACE_ID in .env"
        if production
        else "Run: python scripts/seed_dev.py"
    )
    return [
        DoctorCheck("FAIL", f"Users/workspaces query returned {user_count} users and {workspace_count} workspaces"),
        DoctorCheck("FIX", fix_message),
    ]


async def _check_dry_run_task(
    session_factory: async_sessionmaker,
    settings: Settings,
    *,
    production: bool,
) -> list[DoctorCheck]:
    async with session_factory() as session:
        workspace = await _resolve_workspace(session, settings)
        user = await _resolve_user(session, settings)
        fix_message = (
            "Set DEFAULT_WORKSPACE_ID and DEFAULT_USER_ID in .env to existing production rows"
            if production
            else "Run: python scripts/seed_dev.py"
        )
        if workspace is None or user is None:
            return [
                DoctorCheck("FAIL", "Dry-run task check skipped because DEFAULT_WORKSPACE_ID or DEFAULT_USER_ID does not resolve"),
                DoctorCheck("FIX", fix_message),
            ]

        dry_run_task = Task(
            workspace_id=workspace.id,
            created_by_user_id=user.id,
            title="Doctor dry-run task",
            description="Rollback-only setup validation task.",
            metadata_json={"source": "doctor", "dry_run": True},
        )
        session.add(dry_run_task)
        await session.flush()
        await session.rollback()
    return [DoctorCheck("OK", "Dry-run task insert/rollback works")]


async def _check_memory_query(
    session_factory: async_sessionmaker,
    settings: Settings,
    *,
    production: bool,
) -> list[DoctorCheck]:
    async with session_factory() as session:
        workspace = await _resolve_workspace(session, settings)
        if workspace is None:
            fix_message = (
                "Set DEFAULT_WORKSPACE_ID in .env to an existing production workspace"
                if production
                else "Run: python scripts/seed_dev.py"
            )
            return [
                DoctorCheck("FAIL", "Memory service query skipped because DEFAULT_WORKSPACE_ID does not resolve"),
                DoctorCheck("FIX", fix_message),
            ]
        await search_memories(session, MemorySearchRequest(workspace_id=workspace.id, limit=1))
    return [DoctorCheck("OK", "Memory service query works")]


async def _check_specialist_tables(engine) -> list[DoctorCheck]:
    required_tables = {
        "agent_profiles",
        "agent_runs",
        "agent_run_steps",
        "agent_evaluations",
        "sub_agent_proposals",
        "learning_events",
        "model_budgets",
    }
    try:
        async with engine.connect() as connection:
            table_names = await connection.run_sync(lambda sync_connection: set(inspect(sync_connection).get_table_names()))
    except Exception as exc:
        return [DoctorCheck("FAIL", f"Specialist tables check failed: {redact_sensitive_text(str(exc))}" )]

    missing = sorted(required_tables - table_names)
    if not missing:
        return [DoctorCheck("OK", "Specialist agent tables exist")]
    return [
        DoctorCheck("FAIL", f"Specialist tables missing: {', '.join(missing)}"),
        DoctorCheck("FIX", "Run: python -m alembic upgrade head"),
    ]


def _validate_model_name(model_name: str) -> str | None:
    normalized = model_name.strip()
    if not normalized:
        return "model is empty"
    provider = infer_provider(normalized)
    if provider == "openrouter" and normalized.count("/") < 2:
        return "OpenRouter models must use openrouter/provider/model-name syntax"
    if provider != "openrouter" and "/" not in normalized and normalized.count("-") < 1:
        return "model is too ambiguous"
    return None


def _check_production_runtime_account(*, expected_user: str = PRODUCTION_APP_USER) -> list[DoctorCheck]:
    current_user = getpass.getuser()
    if current_user == expected_user:
        return [DoctorCheck("OK", f"Running as {expected_user}")]
    return [DoctorCheck("WARN", f"Production doctor is running as {current_user}; expected {expected_user}")]


def _check_app_directory(app_dir: Path, *, expected_user: str = PRODUCTION_APP_USER) -> list[DoctorCheck]:
    rendered_app_dir = _display_path(app_dir)
    if not app_dir.exists():
        return [
            DoctorCheck("FAIL", f"Application directory is missing: {rendered_app_dir}"),
            DoctorCheck("FIX", f"Run bash install.sh or create {rendered_app_dir} and set ownership to {expected_user}"),
        ]

    checks = [DoctorCheck("OK", f"Application directory exists at {rendered_app_dir}")]
    if os.name != "posix":
        checks.append(DoctorCheck("WARN", "Application directory ownership check skipped on non-POSIX OS"))
        return checks

    try:
        import grp
        import pwd

        stat_result = app_dir.stat()
        owner_name = pwd.getpwuid(stat_result.st_uid).pw_name
        group_name = grp.getgrgid(stat_result.st_gid).gr_name
    except Exception as exc:
        checks.append(DoctorCheck("WARN", f"Application directory ownership check failed: {redact_sensitive_text(str(exc))}"))
        return checks

    if owner_name == expected_user and group_name == expected_user:
        checks.append(DoctorCheck("OK", f"Application directory is owned by {expected_user}"))
    else:
        checks.append(DoctorCheck("FAIL", f"Application directory owner/group is {owner_name}:{group_name}, expected {expected_user}:{expected_user}"))
        checks.append(DoctorCheck("FIX", f"Run sudo chown -R {expected_user}:{expected_user} {rendered_app_dir}"))
    return checks


def _check_systemctl_available() -> list[DoctorCheck]:
    if shutil.which("systemctl") is None:
        return [
            DoctorCheck("FAIL", "systemctl is unavailable on this host"),
            DoctorCheck("FIX", "Run production doctor on the Linux host that manages the Agent_Sam services"),
        ]
    return [DoctorCheck("OK", "systemctl is available")]


def _check_systemd_units(systemd_dir: Path) -> list[DoctorCheck]:
    missing_units = [unit_name for unit_name in PRODUCTION_SERVICE_UNIT_NAMES if not (systemd_dir / unit_name).exists()]
    if not missing_units:
        return [DoctorCheck("OK", f"Systemd service files exist in {_display_path(systemd_dir)}")]
    return [
        DoctorCheck("FAIL", f"Missing systemd service files in {_display_path(systemd_dir)}: {', '.join(missing_units)}"),
        DoctorCheck("FIX", "Run bash install.sh or deployment/linux/install-host-assets.sh from a sudo-capable operator account"),
    ]


def _check_systemd_service_states() -> list[DoctorCheck]:
    if shutil.which("systemctl") is None:
        return []

    checks: list[DoctorCheck] = []
    for service_name in ("agent-api", "agent-worker", "agent-telegram"):
        enabled_result = run_subprocess(["systemctl", "is-enabled", service_name], capture_output=True)
        if enabled_result.returncode == 0:
            checks.append(DoctorCheck("OK", f"{service_name} is enabled"))
        else:
            checks.append(DoctorCheck("FAIL", f"{service_name} is not enabled"))
            checks.append(DoctorCheck("FIX", f"Run sudo systemctl enable {service_name}"))

        active_result = run_subprocess(["systemctl", "is-active", service_name], capture_output=True)
        if active_result.returncode == 0 and active_result.stdout.strip() == "active":
            checks.append(DoctorCheck("OK", f"{service_name} is running"))
        else:
            checks.append(DoctorCheck("FAIL", f"{service_name} is not running"))
            checks.append(DoctorCheck("FIX", f"Run sudo systemctl start {service_name}"))
    return checks


def _check_nginx_config() -> list[DoctorCheck]:
    if shutil.which("nginx") is None:
        return [DoctorCheck("OK", "nginx not installed; reverse proxy check skipped")]

    for config_path in PRODUCTION_NGINX_CONFIG_PATHS:
        if config_path.exists():
            return [DoctorCheck("OK", f"nginx config exists at {config_path}")]

    expected_paths = ", ".join(_display_path(path) for path in PRODUCTION_NGINX_CONFIG_PATHS)
    return [
        DoctorCheck("FAIL", f"nginx is installed but Agent_Sam config is missing ({expected_paths})"),
        DoctorCheck("FIX", "Install deployment/nginx/agent-api.conf and reload nginx"),
    ]


def _check_api_health() -> list[DoctorCheck]:
    try:
        with urlopen(PRODUCTION_API_HEALTH_URL, timeout=5) as response:
            if response.status >= 400:
                raise URLError(f"HTTP {response.status}")
    except Exception as exc:
        return [
            DoctorCheck("FAIL", f"API health check failed at {PRODUCTION_API_HEALTH_URL}: {redact_sensitive_text(str(exc))}"),
            DoctorCheck("FIX", "Run sudo systemctl status agent-api and inspect sudo journalctl -u agent-api -f"),
        ]
    return [DoctorCheck("OK", f"API health check passed at {PRODUCTION_API_HEALTH_URL}")]


def _check_gateway_registry(settings: Settings, session_factory: async_sessionmaker) -> list[DoctorCheck]:
    try:
        build_enabled_gateway_registry(settings, session_factory=session_factory)
    except Exception as exc:
        return [DoctorCheck("FAIL", f"Gateway registry construction failed: {redact_sensitive_text(str(exc))}")]
    return [DoctorCheck("OK", "Gateways can be constructed from the registry")]


async def _resolve_workspace(session, settings: Settings) -> Workspace | None:
    if settings.default_workspace_id is not None:
        workspace = await session.get(Workspace, settings.default_workspace_id)
        if workspace is not None:
            return workspace
    result = await session.execute(select(Workspace).order_by(Workspace.created_at.asc()).limit(1))
    return result.scalar_one_or_none()


async def _resolve_user(session, settings: Settings) -> User | None:
    if settings.default_user_id is not None:
        user = await session.get(User, settings.default_user_id)
        if user is not None:
            return user
    result = await session.execute(select(User).order_by(User.created_at.asc()).limit(1))
    return result.scalar_one_or_none()


if __name__ == "__main__":
    run()