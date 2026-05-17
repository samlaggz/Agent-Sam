from pathlib import Path

import pytest

from agents.registry import find_agents_for_task, load_agent_profiles, validate_agent_profiles


def test_load_agent_profiles() -> None:
    profiles = load_agent_profiles()

    assert "coding_agent" in profiles
    assert "testing_agent" in profiles
    assert profiles["coding_agent"].default_model
    assert profiles["server_ops_agent"].risk_level == "high"


def test_invalid_profile_rejected(tmp_path: Path) -> None:
    prompts_dir = tmp_path.parent / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = prompts_dir / "broken.md"
    prompt_path.write_text("# Broken\n", encoding="utf-8")

    from agents.base import AgentProfile

    broken = AgentProfile(
        name="Broken",
        slug="broken",
        description="broken",
        task_types=("debugging",),
        default_model="",
        fallback_models=(),
        escalation_model="",
        max_cost_per_task_usd=0,
        max_tokens_per_run=0,
        temperature=0.1,
        tools_allowed=("file_read", "unknown_tool"),
        memory_types_allowed=("project_fact",),
        skill_tags=(),
        risk_level="low",
        system_prompt_path=prompt_path,
        evaluation_checklist=("item",),
        can_create_subagents=False,
        can_propose_skill_updates=False,
    )

    with pytest.raises(ValueError):
        validate_agent_profiles((broken,))


def test_find_agents_for_task_matches_server_ops_and_research() -> None:
    profiles = load_agent_profiles()

    server_matches = find_agents_for_task(
        {"title": "Fix nginx and systemd deployment", "description": "Linux production host issue"},
        profiles,
    )
    research_matches = find_agents_for_task(
        {"title": "Find latest FastAPI docs", "description": "Need official documentation links"},
        profiles,
    )

    assert server_matches[0].slug == "server_ops_agent"
    assert research_matches[0].slug == "research_agent"