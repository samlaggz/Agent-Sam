from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Approval, Skill, SkillProposal, User, Workspace
from db.skill_service import (
    SkillProposalCreateRequest,
    create_skill_proposal,
    get_skill_version_history,
    load_skill_definitions,
    review_skill_proposal,
    search_relevant_skills,
    sync_skills_from_directory,
)

def build_skill_yaml(
    name: str,
    *,
    version: int,
    description: str,
    triggers: list[str],
    tools_allowed: list[str],
    procedure: list[str] | None = None,
) -> str:
        payload = {
                "name": name,
                "version": version,
                "description": description,
                "triggers": triggers,
                "inputs": [
                        {
                                "name": "task_text",
                                "type": "string",
                                "required": True,
                                "description": "The task request.",
                        }
                ],
                "procedure": procedure or ["Review the request.", "Produce a safe action plan."],
                "tools_allowed": tools_allowed,
                "risk_notes": ["Escalate risky actions for approval."],
                "failure_modes": ["The task lacks enough context for a safe next step."],
                "evaluation_checklist": [
                        "The selected tool matches the task.",
                        "The final output records any remaining gaps.",
                ],
        }
        return yaml.safe_dump(payload, sort_keys=False)


@pytest.mark.asyncio
async def test_sync_skills_from_directory_persists_metadata_and_searches_relevant_skills(
    session: AsyncSession,
    workspace: Workspace,
    tmp_path: Path,
) -> None:
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    (skills_root / "server_ops.yaml").write_text(
        build_skill_yaml(
            "server_ops",
            version=1,
            description="Investigate and operate long-running services safely.",
            triggers=["restart service", "inspect service health"],
            tools_allowed=["health_snapshot", "shell_command"],
            procedure=["Inspect service health.", "Only then consider a restart."],
        ),
        encoding="utf-8",
    )
    (skills_root / "code_review.yaml").write_text(
        build_skill_yaml(
            "code_review",
            version=1,
            description="Review code changes for regressions and missing tests.",
            triggers=["review patch", "inspect code diff"],
            tools_allowed=["health_snapshot"],
            procedure=["Find correctness risks first.", "Call out missing tests with evidence."],
        ),
        encoding="utf-8",
    )

    result = await sync_skills_from_directory(session, workspace_id=workspace.id, skills_root=skills_root)

    assert result.created == ("code_review@v1", "server_ops@v1")
    assert not result.proposed

    stored_skills = list(
        (
            await session.execute(
                select(Skill)
                .where(Skill.workspace_id == workspace.id)
                .order_by(Skill.name.asc())
            )
        ).scalars()
    )

    assert [skill.name for skill in stored_skills] == ["code_review", "server_ops"]
    assert stored_skills[0].triggers_json == ["review patch", "inspect code diff"]
    assert stored_skills[1].tools_allowed_json == ["health_snapshot", "shell_command"]

    review_matches = await search_relevant_skills(
        session,
        workspace_id=workspace.id,
        query_text="Review this code diff and check for regressions or missing tests.",
        limit=2,
    )
    ops_matches = await search_relevant_skills(
        session,
        workspace_id=workspace.id,
        query_text="Investigate service health before restarting the api process.",
        limit=2,
    )

    assert review_matches[0].name == "code_review"
    assert ops_matches[0].name == "server_ops"


@pytest.mark.asyncio
async def test_create_skill_proposal_creates_pending_approval(
    session: AsyncSession,
    workspace: Workspace,
    user: User,
) -> None:
    proposal = await create_skill_proposal(
        session,
        SkillProposalCreateRequest(
            workspace_id=workspace.id,
            requested_by_user_id=user.id,
            reason="Need a reusable scraping workflow.",
            yaml_definition=build_skill_yaml(
                "scraping_pipeline",
                version=1,
                description="Run a bounded scraping workflow for public pages.",
                triggers=["scrape public pages", "extract web data"],
                tools_allowed=["shell_command", "health_snapshot"],
            ),
        ),
    )

    approval = await session.scalar(
        select(Approval).where(Approval.skill_proposal_id == proposal.id).limit(1)
    )

    assert proposal.change_type == "create"
    assert proposal.status == "pending"
    assert approval is not None
    assert approval.status == "pending"
    assert approval.requested_by_user_id == user.id


@pytest.mark.asyncio
async def test_review_skill_proposal_publishes_new_version_and_keeps_history(
    session: AsyncSession,
    workspace: Workspace,
    user: User,
    tmp_path: Path,
) -> None:
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    (skills_root / "server_ops.yaml").write_text(
        build_skill_yaml(
            "server_ops",
            version=1,
            description="Investigate and operate services conservatively.",
            triggers=["restart service", "inspect runtime incident"],
            tools_allowed=["health_snapshot", "shell_command"],
        ),
        encoding="utf-8",
    )
    await sync_skills_from_directory(session, workspace_id=workspace.id, skills_root=skills_root)

    proposal = await create_skill_proposal(
        session,
        SkillProposalCreateRequest(
            workspace_id=workspace.id,
            requested_by_user_id=user.id,
            reason="Version two adds stronger post-checks.",
            yaml_definition=build_skill_yaml(
                "server_ops",
                version=2,
                description="Investigate and operate services with explicit post-change verification.",
                triggers=["restart service", "inspect runtime incident", "verify service health"],
                tools_allowed=["health_snapshot", "shell_command"],
                procedure=[
                    "Gather safe diagnostics first.",
                    "Pause high-risk changes for approval.",
                    "Verify service health after any change.",
                ],
            ),
        ),
    )

    history_before = await get_skill_version_history(session, workspace_id=workspace.id, name="server_ops")

    assert [skill.version for skill in history_before] == [1]

    published_skill = await review_skill_proposal(
        session,
        proposal_id=proposal.id,
        reviewed_by_user_id=user.id,
        approve=True,
    )

    history_after = await get_skill_version_history(session, workspace_id=workspace.id, name="server_ops")
    approval = await session.scalar(
        select(Approval).where(Approval.skill_proposal_id == proposal.id).limit(1)
    )
    refreshed_proposal = await session.get(SkillProposal, proposal.id)

    assert published_skill is not None
    assert published_skill.version == 2
    assert [skill.version for skill in history_after] == [1, 2]
    assert history_after[0].status == "superseded"
    assert history_after[1].status == "active"
    assert approval is not None
    assert approval.status == "approved"
    assert refreshed_proposal is not None
    assert refreshed_proposal.status == "applied"
    assert refreshed_proposal.applied_skill_id == published_skill.id


def test_repository_example_skills_are_valid() -> None:
    skills_root = Path(__file__).resolve().parents[1] / "skills"
    definitions = load_skill_definitions(skills_root)
    definition_names = {definition.name for definition in definitions}

    assert {"scraping_pipeline", "server_ops", "code_review", "linux_deployment"}.issubset(
        definition_names
    )