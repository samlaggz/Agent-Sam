from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import AgentProfileRecord, AgentRun, Approval, LearningEvent, Skill, SkillProposal, SubAgentProposal, Task
from db.skill_service import _create_skill_proposal_record, _get_latest_skill, _publish_skill_definition, parse_skill_yaml
from services.sub_agent_factory import create_agent_from_proposal


SAFE_MEMORY_TYPES = {"project_fact", "skill_note", "task_summary", "warning", "decision"}
AUTO_MEMORY_CONFIDENCE_THRESHOLD = 0.85


@dataclass(frozen=True)
class ExtractedLearning:
    event_type: str
    content: str
    confidence: float
    evidence: str


async def extract_learning_from_completed_task(session: AsyncSession, task_id: UUID, agent_slug: str) -> tuple[LearningEvent, ...]:
    task = await session.get(Task, task_id)
    if task is None:
        raise ValueError(f"Task {task_id} does not exist.")

    latest_agent_run = await session.scalar(
        select(AgentRun).where(AgentRun.task_id == task_id).order_by(AgentRun.created_at.desc()).limit(1)
    )
    summary = latest_agent_run.output_summary if latest_agent_run is not None else (task.description or task.title)
    evidence = summary or task.title
    content = f"Task {task.title}: {summary or 'completed without recorded summary.'}".strip()
    learning_event = await save_learning_event(
        session,
        agent_slug=agent_slug,
        task_id=task_id,
        event_type="completed_task_summary",
        content=content,
        confidence=0.8,
        approved=False,
        metadata_json={"evidence": evidence},
    )
    await session.commit()
    return (learning_event,)


async def save_learning_event(
    session: AsyncSession,
    *,
    agent_slug: str,
    task_id: UUID | None,
    event_type: str,
    content: str,
    confidence: float,
    approved: bool = False,
    metadata_json: dict[str, Any] | None = None,
) -> LearningEvent:
    learning_event = LearningEvent(
        agent_slug=agent_slug,
        task_id=task_id,
        event_type=event_type,
        content=content,
        confidence=confidence,
        approved=approved,
        metadata_json=metadata_json or {},
    )
    session.add(learning_event)
    await session.flush()
    return learning_event


async def propose_skill_update(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    agent_slug: str,
    yaml_definition: str,
    reason: str,
    evidence: str,
    requested_by_user_id: UUID | None = None,
) -> SkillProposal:
    definition = parse_skill_yaml(yaml_definition)
    proposal = await _create_skill_proposal_record(
        session,
        workspace_id=workspace_id,
        definition=definition,
        requested_by_user_id=requested_by_user_id,
        reason=reason,
    )
    proposal.agent_slug = agent_slug
    proposal.evidence_text = evidence
    proposal.status = "pending"
    await session.commit()
    await session.refresh(proposal)
    return proposal


async def propose_sub_agent(
    session: AsyncSession,
    *,
    parent_agent_slug: str,
    proposed_slug: str,
    proposed_name: str,
    proposed_profile_yaml: str,
    reason: str,
    expected_savings: str,
    expected_quality_gain: str,
    required_tools: list[str],
    prompt_markdown: str,
    requested_by_user_id: UUID | None = None,
) -> SubAgentProposal:
    proposal = SubAgentProposal(
        parent_agent_slug=parent_agent_slug,
        proposed_slug=proposed_slug,
        proposed_name=proposed_name,
        proposed_profile_yaml=proposed_profile_yaml,
        reason=reason,
        expected_savings=expected_savings,
        expected_quality_gain=expected_quality_gain,
        required_tools_json=list(required_tools),
        prompt_markdown=prompt_markdown,
        status="pending",
        requested_by_user_id=requested_by_user_id,
        metadata_json={"required_tools": list(required_tools)},
    )
    session.add(proposal)
    await session.commit()
    await session.refresh(proposal)
    return proposal


async def approve_skill_proposal(
    session: AsyncSession,
    *,
    proposal_id: UUID,
    reviewed_by_user_id: UUID,
) -> SkillProposal:
    proposal = await session.get(SkillProposal, proposal_id)
    if proposal is None:
        raise ValueError(f"Skill proposal {proposal_id} does not exist.")
    if proposal.status != "pending":
        raise ValueError(f"Skill proposal {proposal_id} is already {proposal.status}.")

    approval = await session.scalar(
        select(Approval).where(Approval.skill_proposal_id == proposal_id).order_by(Approval.created_at.desc()).limit(1)
    )
    if approval is not None:
        approval.status = "approved"
        approval.reviewed_by_user_id = reviewed_by_user_id
        approval.reviewed_at = datetime.now(timezone.utc)
    proposal.status = "approved"
    proposal.reviewed_by_user_id = reviewed_by_user_id
    proposal.reviewed_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(proposal)
    return proposal


async def activate_skill_proposal(
    session: AsyncSession,
    *,
    proposal_id: UUID,
    reviewed_by_user_id: UUID,
) -> Skill:
    proposal = await session.get(SkillProposal, proposal_id)
    if proposal is None:
        raise ValueError(f"Skill proposal {proposal_id} does not exist.")
    if proposal.status not in {"approved", "applied"}:
        raise ValueError(f"Skill proposal {proposal_id} must be approved before activation.")

    definition = parse_skill_yaml(proposal.yaml_definition, source_path=proposal.source_path)
    published_skill = await _publish_skill_definition(
        session,
        workspace_id=proposal.workspace_id,
        definition=definition,
        approved_by_user_id=reviewed_by_user_id,
    )
    proposal.status = "activated"
    proposal.applied_skill_id = published_skill.id
    proposal.reviewed_by_user_id = reviewed_by_user_id
    proposal.reviewed_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(published_skill)
    return published_skill


async def approve_sub_agent_proposal(
    session: AsyncSession,
    *,
    proposal_id: UUID,
    reviewed_by_user_id: UUID,
) -> SubAgentProposal:
    proposal = await session.get(SubAgentProposal, proposal_id)
    if proposal is None:
        raise ValueError(f"Sub-agent proposal {proposal_id} does not exist.")
    if proposal.status != "pending":
        raise ValueError(f"Sub-agent proposal {proposal_id} is already {proposal.status}.")
    proposal.status = "approved"
    proposal.reviewed_by_user_id = reviewed_by_user_id
    proposal.reviewed_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(proposal)
    return proposal


async def create_agent_from_approved_proposal(
    session: AsyncSession,
    *,
    proposal_id: UUID,
    reviewed_by_user_id: UUID,
) -> AgentProfileRecord:
    proposal = await session.get(SubAgentProposal, proposal_id)
    if proposal is None:
        raise ValueError(f"Sub-agent proposal {proposal_id} does not exist.")
    if proposal.status != "approved":
        raise ValueError("Sub-agent proposal must be approved before creation.")
    profile_record = await create_agent_from_proposal(session, proposal, reviewed_by_user_id=reviewed_by_user_id)
    proposal.status = "created"
    proposal.reviewed_by_user_id = reviewed_by_user_id
    proposal.reviewed_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(profile_record)
    return profile_record