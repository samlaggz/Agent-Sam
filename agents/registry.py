from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from agents.base import AgentProfile
from scripts.env_writer import load_env_values


AGENTS_ROOT = Path(__file__).resolve().parent
CONFIG_ROOT = AGENTS_ROOT / "configs"
PROMPTS_ROOT = AGENTS_ROOT / "prompts"
GENERATED_CONFIG_ROOT = CONFIG_ROOT / "generated"
GENERATED_PROMPTS_ROOT = PROMPTS_ROOT / "generated"
ENV_PATTERN = re.compile(r"\$\{(?P<name>[A-Z0-9_]+)(?::-(?P<default>.*))?\}")
KNOWN_TOOLS = {
    "brand_context_search",
    "citation_collector",
    "file_read",
    "file_write",
    "grep",
    "image_prompt_builder",
    "memory_save",
    "memory_search",
    "pytest",
    "safe_shell",
    "service_control",
    "skill_search",
    "sql_readonly",
    "test_result_reader",
    "web_open",
    "web_search",
}
KNOWN_MEMORY_TYPES = {
    "decision",
    "project_fact",
    "skill_note",
    "task_summary",
    "warning",
}
RISK_LEVELS = {"low", "medium", "high"}
DANGEROUS_TOOLS = {"safe_shell", "service_control", "sql_write", "sql_delete"}


def load_agent_profiles(config_root: Path = CONFIG_ROOT) -> dict[str, AgentProfile]:
    profiles: dict[str, AgentProfile] = {}
    config_paths = sorted(config_root.glob("*.yaml"))
    generated_paths = sorted((config_root / "generated").glob("*.yaml")) if (config_root / "generated").exists() else []
    for config_path in [*config_paths, *generated_paths]:
        profile = _load_single_profile(config_path)
        profiles[profile.slug] = profile
    validate_agent_profiles(profiles)
    return profiles


@lru_cache(maxsize=1)
def _cached_profiles() -> dict[str, AgentProfile]:
    return load_agent_profiles()


def get_agent_profile(slug: str) -> AgentProfile:
    profiles = _cached_profiles()
    if slug not in profiles:
        raise KeyError(f"Unknown agent profile: {slug}")
    return profiles[slug]


def list_agents() -> tuple[AgentProfile, ...]:
    return tuple(sorted(_cached_profiles().values(), key=lambda profile: profile.slug))


def validate_agent_profiles(profiles: Mapping[str, AgentProfile] | Sequence[AgentProfile]) -> tuple[AgentProfile, ...]:
    normalized = profiles.values() if isinstance(profiles, Mapping) else profiles
    validated: list[AgentProfile] = []
    seen_slugs: set[str] = set()
    for profile in normalized:
        if profile.slug in seen_slugs:
            raise ValueError(f"Duplicate agent slug: {profile.slug}")
        if not profile.default_model or not profile.escalation_model:
            raise ValueError(f"Agent {profile.slug} is missing model configuration")
        if profile.max_cost_per_task_usd <= 0:
            raise ValueError(f"Agent {profile.slug} must have a positive max_cost_per_task_usd")
        if profile.max_tokens_per_run <= 0:
            raise ValueError(f"Agent {profile.slug} must have a positive max_tokens_per_run")
        if not profile.system_prompt_path.exists():
            raise ValueError(f"Agent {profile.slug} prompt file is missing: {profile.system_prompt_path}")
        unknown_tools = sorted(set(profile.tools_allowed) - KNOWN_TOOLS)
        if unknown_tools:
            raise ValueError(f"Agent {profile.slug} has unknown tools: {', '.join(unknown_tools)}")
        if profile.risk_level not in RISK_LEVELS:
            raise ValueError(f"Agent {profile.slug} has invalid risk level: {profile.risk_level}")
        if any(tool_name in DANGEROUS_TOOLS for tool_name in profile.tools_allowed) and profile.risk_level == "low":
            raise ValueError(f"Agent {profile.slug} cannot use dangerous tools with low risk_level")
        unknown_memory_types = sorted(set(profile.memory_types_allowed) - KNOWN_MEMORY_TYPES)
        if unknown_memory_types:
            raise ValueError(
                f"Agent {profile.slug} has unknown memory types: {', '.join(unknown_memory_types)}"
            )
        seen_slugs.add(profile.slug)
        validated.append(profile)
    return tuple(validated)


def find_agents_for_task(task: Mapping[str, Any], profiles: Mapping[str, AgentProfile] | None = None) -> tuple[AgentProfile, ...]:
    available_profiles = profiles or _cached_profiles()
    haystack = _task_text(task)
    scored: list[tuple[int, AgentProfile]] = []
    for profile in available_profiles.values():
        score = 0
        for task_type in profile.task_types:
            if task_type.replace("_", " ") in haystack:
                score += 3
        for keyword in _keywords_for_profile(profile.slug):
            if keyword in haystack:
                score += 2
        if profile.slug in haystack:
            score += 5
        if score > 0:
            scored.append((score, profile))
    scored.sort(key=lambda item: (-item[0], item[1].slug))
    return tuple(profile for _, profile in scored)


def refresh_agent_profiles() -> None:
    _cached_profiles.cache_clear()


def _load_single_profile(config_path: Path) -> AgentProfile:
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    resolved = _resolve_env_values(payload)
    prompt_path = AGENTS_ROOT / Path(str(resolved["system_prompt_path"]))
    return AgentProfile(
        name=str(resolved["name"]),
        slug=str(resolved["slug"]),
        description=str(resolved["description"]),
        task_types=_normalize_tuple(resolved.get("task_types", [])),
        default_model=str(resolved["default_model"]),
        fallback_models=_normalize_tuple(resolved.get("fallback_models", [])),
        escalation_model=str(resolved["escalation_model"]),
        max_cost_per_task_usd=float(resolved.get("max_cost_per_task_usd", 0)),
        max_tokens_per_run=int(resolved.get("max_tokens_per_run", 0)),
        temperature=float(resolved.get("temperature", 0.2)),
        tools_allowed=_normalize_tuple(resolved.get("tools_allowed", [])),
        memory_types_allowed=_normalize_tuple(resolved.get("memory_types_allowed", [])),
        skill_tags=_normalize_tuple(resolved.get("skill_tags", [])),
        risk_level=str(resolved.get("risk_level", "medium")),
        system_prompt_path=prompt_path,
        evaluation_checklist=_normalize_tuple(resolved.get("evaluation_checklist", [])),
        can_create_subagents=bool(resolved.get("can_create_subagents", False)),
        can_propose_skill_updates=bool(resolved.get("can_propose_skill_updates", False)),
    )


def _resolve_env_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _resolve_env_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_env_values(item) for item in value]
    if isinstance(value, str):
        return _resolve_env_string(value)
    return value


def _resolve_env_string(raw_value: str) -> str:
    env_values = load_env_values(Path.cwd() / ".env")

    def replace(match: re.Match[str]) -> str:
        env_name = match.group("name")
        default_value = match.group("default") or ""
        return os.environ.get(env_name, env_values.get(env_name, default_value))

    return ENV_PATTERN.sub(replace, raw_value)


def _normalize_tuple(values: Sequence[Any]) -> tuple[str, ...]:
    return tuple(str(value).strip() for value in values if str(value).strip())


def _task_text(task: Mapping[str, Any]) -> str:
    metadata = task.get("metadata", {}) if isinstance(task.get("metadata", {}), Mapping) else {}
    parts = [
        str(task.get("title", "")),
        str(task.get("description", "")),
        str(task.get("task_type", "")),
        " ".join(f"{key}:{value}" for key, value in metadata.items()),
    ]
    return " ".join(parts).lower()


def _keywords_for_profile(slug: str) -> tuple[str, ...]:
    return {
        "coding_agent": ("debug", "refactor", "implementation", "code", "bugfix"),
        "testing_agent": ("pytest", "test", "ci", "regression", "failing test"),
        "research_agent": (
            "latest",
            "docs",
            "documentation",
            "research",
            "compare",
            "internet",
            "online",
            "web",
            "website",
            "browse",
            "look up",
            "lookup",
            "news",
        ),
        "planning_agent": ("roadmap", "architecture", "breakdown", "plan"),
        "server_ops_agent": (
            "systemd",
            "nginx",
            "linux",
            "deploy",
            "postgres",
            "redis",
            "qdrant",
            "server",
            "filesystem",
            "file system",
            "folder",
            "file",
            "path",
            "directory",
            "service",
            "site",
            "apache",
            "process",
            "daemon",
            "find on server",
            "running process",
        ),
        "graphics_agent": ("visual", "brand", "image", "video", "design", "creative"),
        "data_agent": ("sql", "analytics", "report", "spreadsheet", "query"),
        "qa_reviewer_agent": ("review", "final check", "safety", "qa"),
        "router_agent": ("route", "choose agent", "routing"),
    }.get(slug, ())