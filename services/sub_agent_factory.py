from __future__ import annotations

from pathlib import Path
from uuid import UUID

import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from agents.registry import GENERATED_CONFIG_ROOT, GENERATED_PROMPTS_ROOT, KNOWN_TOOLS, refresh_agent_profiles
from db.models import AgentProfileRecord, SubAgentProposal


ADMIN_ONLY_TOOLS = {"safe_shell", "service_control", "sql_write", "sql_delete"}


async def create_agent_from_proposal(
    session: AsyncSession,
    proposal: SubAgentProposal,
    *,
    reviewed_by_user_id: UUID,
) -> AgentProfileRecord:
    payload = yaml.safe_load(proposal.proposed_profile_yaml) or {}
    required_tools = tuple(str(tool_name) for tool_name in payload.get("tools_allowed", []))
    unknown_tools = sorted(set(required_tools) - KNOWN_TOOLS)
    if unknown_tools:
        raise ValueError(f"Sub-agent proposal contains unknown tools: {', '.join(unknown_tools)}")

    dangerous_tools = sorted(set(required_tools) & ADMIN_ONLY_TOOLS)
    admin_approved = bool((proposal.metadata_json or {}).get("dangerous_tools_approved", False))
    if dangerous_tools and not admin_approved:
        raise ValueError("Dangerous tools require admin approval before sub-agent creation.")

    GENERATED_CONFIG_ROOT.mkdir(parents=True, exist_ok=True)
    GENERATED_PROMPTS_ROOT.mkdir(parents=True, exist_ok=True)

    config_path = GENERATED_CONFIG_ROOT / f"{proposal.proposed_slug}.yaml"
    prompt_path = GENERATED_PROMPTS_ROOT / f"{proposal.proposed_slug}.md"
    config_path.write_text(proposal.proposed_profile_yaml.rstrip() + "\n", encoding="utf-8")
    prompt_path.write_text((proposal.prompt_markdown or f"# {proposal.proposed_name}\n").rstrip() + "\n", encoding="utf-8")

    profile_record = AgentProfileRecord(
        slug=proposal.proposed_slug,
        name=proposal.proposed_name,
        description=str(payload.get("description", proposal.reason)),
        profile_yaml=proposal.proposed_profile_yaml.rstrip() + "\n",
        version=int(payload.get("version", 1)),
        is_active=True,
    )
    session.add(profile_record)
    await session.flush()
    refresh_agent_profiles()
    return profile_record