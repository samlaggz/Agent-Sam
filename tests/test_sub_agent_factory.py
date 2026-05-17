from __future__ import annotations

import pytest

from db.models import SubAgentProposal
from services.sub_agent_factory import create_agent_from_proposal


@pytest.mark.asyncio
async def test_sub_agent_factory_refuses_dangerous_tool_without_admin_approval(session, user) -> None:
    proposal = SubAgentProposal(
        parent_agent_slug="coding_agent",
        proposed_slug="dangerous_agent",
        proposed_name="Dangerous Agent",
        proposed_profile_yaml=(
            "name: Dangerous Agent\n"
            "slug: dangerous_agent\n"
            "description: Should fail.\n"
            "task_types:\n  - deployment\n"
            "default_model: openrouter/openai/gpt-4.1-mini\n"
            "fallback_models: []\n"
            "escalation_model: openrouter/anthropic/claude-3.7-sonnet\n"
            "max_cost_per_task_usd: 0.05\n"
            "max_tokens_per_run: 8000\n"
            "temperature: 0.0\n"
            "tools_allowed:\n  - safe_shell\n"
            "memory_types_allowed:\n  - project_fact\n"
            "skill_tags:\n  - deployment\n"
            "risk_level: high\n"
            "system_prompt_path: prompts/generated/dangerous_agent.md\n"
            "evaluation_checklist:\n  - Checked risk\n"
            "can_create_subagents: false\n"
            "can_propose_skill_updates: false\n"
        ),
        reason="Bad idea",
        expected_savings="",
        expected_quality_gain="",
        required_tools_json=["safe_shell"],
        prompt_markdown="# Dangerous Agent\n",
        requested_by_user_id=user.id,
        metadata_json={},
    )
    session.add(proposal)
    await session.flush()

    with pytest.raises(ValueError):
        await create_agent_from_proposal(session, proposal, reviewed_by_user_id=user.id)