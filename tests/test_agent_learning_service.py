from __future__ import annotations

from uuid import uuid4

import pytest

from db.models import SkillProposal, SubAgentProposal
from db.task_queue import create_task
from services.agent_learning_service import activate_skill_proposal, approve_skill_proposal, approve_sub_agent_proposal, create_agent_from_approved_proposal, extract_learning_from_completed_task, propose_skill_update, propose_sub_agent


SKILL_YAML = """
name: deploy_checklist
version: 1
description: Validate deployment safety.
triggers:
  - deploy
inputs:
  - name: target
    type: string
procedure:
  - Review deployment plan
tools_allowed:
  - file_read
risk_notes:
  - Review before production
failure_modes:
  - Missed rollback steps
evaluation_checklist:
  - Deployment reviewed
""".strip()


@pytest.mark.asyncio
async def test_learning_event_saved_after_completed_task(session, workspace, user) -> None:
    task = await create_task(
        session,
        workspace_id=workspace.id,
        created_by_user_id=user.id,
        title="Document rollout",
        description="Summarize what worked",
    )

    events = await extract_learning_from_completed_task(session, task.id, "coding_agent")

    assert len(events) == 1
    assert events[0].agent_slug == "coding_agent"


@pytest.mark.asyncio
async def test_skill_proposal_requires_approval_before_activation(session, workspace, user) -> None:
    proposal = await propose_skill_update(
        session,
        workspace_id=workspace.id,
        agent_slug="coding_agent",
        yaml_definition=SKILL_YAML,
        reason="Useful deployment checklist",
        evidence="Task 123 succeeded",
        requested_by_user_id=user.id,
    )

    with pytest.raises(ValueError):
        await activate_skill_proposal(session, proposal_id=proposal.id, reviewed_by_user_id=user.id)

    approved = await approve_skill_proposal(session, proposal_id=proposal.id, reviewed_by_user_id=user.id)
    assert approved.status == "approved"
    activated = await activate_skill_proposal(session, proposal_id=proposal.id, reviewed_by_user_id=user.id)

    assert activated.name == "deploy_checklist"


@pytest.mark.asyncio
async def test_sub_agent_proposal_requires_approval_before_creation(session, user) -> None:
    proposal = await propose_sub_agent(
        session,
        parent_agent_slug="coding_agent",
        proposed_slug="log_analysis_agent",
        proposed_name="Log Analysis Agent",
        proposed_profile_yaml=(
            "name: Log Analysis Agent\n"
            "slug: log_analysis_agent\n"
            "description: Analyze logs.\n"
            "task_types:\n  - diagnostics\n"
            "default_model: openrouter/openai/gpt-4.1-mini\n"
            "fallback_models: []\n"
            "escalation_model: openrouter/anthropic/claude-3.7-sonnet\n"
            "max_cost_per_task_usd: 0.05\n"
            "max_tokens_per_run: 8000\n"
            "temperature: 0.0\n"
            "tools_allowed:\n  - file_read\n"
            "memory_types_allowed:\n  - project_fact\n"
            "skill_tags:\n  - logs\n"
            "risk_level: low\n"
            "system_prompt_path: prompts/generated/log_analysis_agent.md\n"
            "evaluation_checklist:\n  - Checked logs\n"
            "can_create_subagents: false\n"
            "can_propose_skill_updates: false\n"
        ),
        reason="Common logs-only diagnostic tasks",
        expected_savings="Lower coding agent spend",
        expected_quality_gain="More focused triage",
        required_tools=["file_read"],
        prompt_markdown="# Log Analysis Agent\n",
        requested_by_user_id=user.id,
    )

    with pytest.raises(ValueError):
        await create_agent_from_approved_proposal(session, proposal_id=proposal.id, reviewed_by_user_id=user.id)

    approved = await approve_sub_agent_proposal(session, proposal_id=proposal.id, reviewed_by_user_id=user.id)
    assert approved.status == "approved"
    created = await create_agent_from_approved_proposal(session, proposal_id=proposal.id, reviewed_by_user_id=user.id)

    assert created.slug == "log_analysis_agent"