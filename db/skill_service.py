from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Approval, Skill, SkillProposal


REQUIRED_SKILL_FIELDS = {
    "name",
    "version",
    "description",
    "triggers",
    "inputs",
    "procedure",
    "tools_allowed",
    "risk_notes",
    "failure_modes",
    "evaluation_checklist",
}
SKILL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,127}$")
TOKEN_PATTERN = re.compile(r"[a-z0-9_:-]{3,}")


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    version: int
    description: str
    triggers: tuple[str, ...]
    inputs: tuple[dict[str, Any], ...]
    procedure: tuple[Any, ...]
    tools_allowed: tuple[str, ...]
    risk_notes: tuple[str, ...]
    failure_modes: tuple[str, ...]
    evaluation_checklist: tuple[str, ...]
    yaml_definition: str
    source_path: str | None = None

    @property
    def content_hash(self) -> str:
        canonical_payload = json.dumps(self.to_payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "triggers": list(self.triggers),
            "inputs": [dict(item) for item in self.inputs],
            "procedure": [item for item in self.procedure],
            "tools_allowed": list(self.tools_allowed),
            "risk_notes": list(self.risk_notes),
            "failure_modes": list(self.failure_modes),
            "evaluation_checklist": list(self.evaluation_checklist),
        }


@dataclass(frozen=True)
class SkillProposalCreateRequest:
    workspace_id: UUID
    yaml_definition: str
    requested_by_user_id: UUID | None = None
    reason: str | None = None
    source_path: str | None = None


@dataclass(frozen=True)
class SkillSearchResult:
    skill_id: UUID
    name: str
    version: int
    description: str
    excerpt: str
    source: str | None
    score: float
    triggers: tuple[str, ...]
    tools_allowed: tuple[str, ...]


@dataclass(frozen=True)
class SkillSyncResult:
    created: tuple[str, ...]
    proposed: tuple[str, ...]
    unchanged: tuple[str, ...]


def load_skill_definition(skill_path: Path) -> SkillDefinition:
    raw_yaml = skill_path.read_text(encoding="utf-8")
    return parse_skill_yaml(raw_yaml, source_path=skill_path.name)


def load_skill_definitions(skills_root: Path) -> list[SkillDefinition]:
    if not skills_root.exists():
        return []

    loaded: list[SkillDefinition] = []
    seen_keys: set[tuple[str, int]] = set()
    for skill_path in sorted(skills_root.glob("*.y*ml")):
        definition = load_skill_definition(skill_path)
        key = (definition.name, definition.version)
        if key in seen_keys:
            raise ValueError(f"Duplicate skill definition detected for {definition.name} v{definition.version}.")
        seen_keys.add(key)
        loaded.append(definition)
    return loaded


def parse_skill_yaml(yaml_definition: str, *, source_path: str | None = None) -> SkillDefinition:
    payload = yaml.safe_load(yaml_definition)
    if not isinstance(payload, dict):
        raise ValueError("Skill YAML must decode to an object.")

    missing = REQUIRED_SKILL_FIELDS.difference(payload.keys())
    unexpected = set(payload.keys()).difference(REQUIRED_SKILL_FIELDS)
    if missing:
        missing_list = ", ".join(sorted(missing))
        raise ValueError(f"Skill YAML is missing required fields: {missing_list}.")
    if unexpected:
        unexpected_list = ", ".join(sorted(unexpected))
        raise ValueError(f"Skill YAML contains unexpected fields: {unexpected_list}.")

    name = str(payload["name"]).strip()
    if not SKILL_NAME_PATTERN.match(name):
        raise ValueError("Skill name must use lower snake_case and start with a letter.")

    version = payload["version"]
    if not isinstance(version, int) or version <= 0:
        raise ValueError("Skill version must be a positive integer.")

    description = _normalize_required_string(payload["description"], field_name="description")
    triggers = _normalize_string_list(payload["triggers"], field_name="triggers")
    tools_allowed = _normalize_string_list(payload["tools_allowed"], field_name="tools_allowed")
    risk_notes = _normalize_string_list(payload["risk_notes"], field_name="risk_notes")
    failure_modes = _normalize_string_list(payload["failure_modes"], field_name="failure_modes")
    evaluation_checklist = _normalize_string_list(
        payload["evaluation_checklist"],
        field_name="evaluation_checklist",
    )
    inputs = _normalize_inputs(payload["inputs"])
    procedure = _normalize_procedure(payload["procedure"])

    return SkillDefinition(
        name=name,
        version=version,
        description=description,
        triggers=triggers,
        inputs=inputs,
        procedure=procedure,
        tools_allowed=tools_allowed,
        risk_notes=risk_notes,
        failure_modes=failure_modes,
        evaluation_checklist=evaluation_checklist,
        yaml_definition=yaml_definition.strip() + "\n",
        source_path=source_path,
    )


async def sync_skills_from_directory(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    skills_root: Path,
    requested_by_user_id: UUID | None = None,
) -> SkillSyncResult:
    created: list[str] = []
    proposed: list[str] = []
    unchanged: list[str] = []

    for definition in load_skill_definitions(skills_root):
        label = _skill_label(definition.name, definition.version)
        exact_skill = await _get_skill_by_name_version(
            session,
            workspace_id=workspace_id,
            name=definition.name,
            version=definition.version,
        )
        if exact_skill is not None:
            if _hash_existing_skill(exact_skill) != definition.content_hash:
                raise ValueError(
                    f"Skill {label} already exists with different content. Create a new version instead."
                )
            unchanged.append(label)
            continue

        latest_skill = await _get_latest_skill(session, workspace_id=workspace_id, name=definition.name)
        if latest_skill is None:
            await _publish_skill_definition(
                session,
                workspace_id=workspace_id,
                definition=definition,
                approved_by_user_id=requested_by_user_id,
            )
            created.append(label)
            continue

        if definition.version <= latest_skill.version:
            raise ValueError(
                f"Skill {definition.name} version {definition.version} is not newer than the latest stored version {latest_skill.version}."
            )

        pending_proposal = await _get_pending_proposal(
            session,
            workspace_id=workspace_id,
            name=definition.name,
            version=definition.version,
            content_hash=definition.content_hash,
        )
        if pending_proposal is not None:
            unchanged.append(label)
            continue

        await _create_skill_proposal_record(
            session,
            workspace_id=workspace_id,
            definition=definition,
            requested_by_user_id=requested_by_user_id,
            reason=f"Skill file {definition.source_path or definition.name} proposes a new version.",
        )
        proposed.append(label)

    await session.commit()
    return SkillSyncResult(created=tuple(created), proposed=tuple(proposed), unchanged=tuple(unchanged))


async def search_relevant_skills(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    query_text: str,
    limit: int = 4,
) -> list[SkillSearchResult]:
    if limit <= 0:
        return []

    result = await session.execute(
        select(Skill)
        .where(Skill.workspace_id == workspace_id, Skill.status == "active")
        .order_by(Skill.version.desc(), Skill.name.asc())
    )
    active_skills = list(result.scalars().all())
    if not active_skills:
        return []

    query_tokens = _tokenize_text(query_text)
    ranked = sorted(
        (
            SkillSearchResult(
                skill_id=skill.id,
                name=skill.name,
                version=skill.version,
                description=skill.description or "",
                excerpt=_build_excerpt(skill.description or "", skill.procedure_json or []),
                source=skill.source_path,
                score=_score_skill(skill, query_tokens=query_tokens, raw_query=query_text),
                triggers=tuple(skill.triggers_json or []),
                tools_allowed=tuple(skill.tools_allowed_json or []),
            )
            for skill in active_skills
        ),
        key=lambda item: (item.score, item.version, item.name),
        reverse=True,
    )

    matches = [item for item in ranked if item.score > 0]
    if not matches:
        matches = ranked[:limit]
    return matches[:limit]


async def create_skill_proposal(
    session: AsyncSession,
    request: SkillProposalCreateRequest,
) -> SkillProposal:
    definition = parse_skill_yaml(request.yaml_definition, source_path=request.source_path)
    exact_skill = await _get_skill_by_name_version(
        session,
        workspace_id=request.workspace_id,
        name=definition.name,
        version=definition.version,
    )
    if exact_skill is not None:
        if _hash_existing_skill(exact_skill) == definition.content_hash:
            raise ValueError(f"Skill {_skill_label(definition.name, definition.version)} is already published.")
        raise ValueError(
            f"Skill {_skill_label(definition.name, definition.version)} already exists with different content."
        )

    pending_proposal = await _get_pending_proposal(
        session,
        workspace_id=request.workspace_id,
        name=definition.name,
        version=definition.version,
        content_hash=definition.content_hash,
    )
    if pending_proposal is not None:
        return pending_proposal

    latest_skill = await _get_latest_skill(session, workspace_id=request.workspace_id, name=definition.name)
    if latest_skill is not None and definition.version <= latest_skill.version:
        raise ValueError(
            f"Skill {_skill_label(definition.name, definition.version)} must use a version greater than {latest_skill.version}."
        )

    proposal = await _create_skill_proposal_record(
        session,
        workspace_id=request.workspace_id,
        definition=definition,
        requested_by_user_id=request.requested_by_user_id,
        reason=request.reason,
    )
    await session.commit()
    await session.refresh(proposal)
    return proposal


async def review_skill_proposal(
    session: AsyncSession,
    *,
    proposal_id: UUID,
    reviewed_by_user_id: UUID,
    approve: bool,
) -> Skill | None:
    proposal = await session.get(SkillProposal, proposal_id)
    if proposal is None:
        raise ValueError(f"Skill proposal {proposal_id} does not exist.")
    if proposal.status != "pending":
        raise ValueError(f"Skill proposal {proposal_id} is already {proposal.status}.")

    approval = await _get_latest_approval_for_proposal(session, proposal.id)
    if approval is None:
        raise ValueError(f"Skill proposal {proposal_id} is missing an approval record.")
    if approval.status != "pending":
        raise ValueError(f"Approval {approval.id} is already {approval.status}.")

    now = datetime.now(timezone.utc)
    proposal.reviewed_by_user_id = reviewed_by_user_id
    proposal.reviewed_at = now
    approval.reviewed_by_user_id = reviewed_by_user_id
    approval.reviewed_at = now

    if not approve:
        proposal.status = "rejected"
        approval.status = "rejected"
        await session.commit()
        return None

    definition = parse_skill_yaml(proposal.yaml_definition, source_path=proposal.source_path)
    published_skill = await _publish_skill_definition(
        session,
        workspace_id=proposal.workspace_id,
        definition=definition,
        approved_by_user_id=reviewed_by_user_id,
    )
    proposal.status = "applied"
    proposal.applied_skill_id = published_skill.id
    approval.status = "approved"
    await session.commit()
    await session.refresh(published_skill)
    return published_skill


async def get_skill_version_history(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    name: str,
) -> list[Skill]:
    result = await session.execute(
        select(Skill)
        .where(Skill.workspace_id == workspace_id, Skill.name == name)
        .order_by(Skill.version.asc())
    )
    return list(result.scalars().all())


def _normalize_required_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Skill field '{field_name}' must be a non-empty string.")
    return value.strip()


def _normalize_string_list(value: Any, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"Skill field '{field_name}' must be a list of strings.")

    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        cleaned = _normalize_required_string(item, field_name=field_name)
        if cleaned in seen:
            continue
        normalized.append(cleaned)
        seen.add(cleaned)
    return tuple(normalized)


def _normalize_inputs(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        raise ValueError("Skill field 'inputs' must be a list.")

    normalized: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Each skill input must be an object.")
        serialized = _coerce_json_value(item, field_name="inputs")
        name = serialized.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Each skill input must include a non-empty 'name'.")
        normalized.append(serialized)
    return tuple(normalized)


def _normalize_procedure(value: Any) -> tuple[Any, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("Skill field 'procedure' must be a non-empty list.")

    normalized: list[Any] = []
    for item in value:
        if isinstance(item, str):
            normalized.append(_normalize_required_string(item, field_name="procedure"))
            continue
        if not isinstance(item, dict):
            raise ValueError("Each procedure step must be a string or an object.")
        normalized.append(_coerce_json_value(item, field_name="procedure"))
    return tuple(normalized)


def _coerce_json_value(value: Any, *, field_name: str) -> Any:
    try:
        return json.loads(json.dumps(value))
    except TypeError as exc:
        raise ValueError(f"Skill field '{field_name}' must be JSON serializable.") from exc


def _build_excerpt(description: str, procedure: list[Any] | tuple[Any, ...]) -> str:
    fragments = [description.strip()]
    for item in procedure[:2]:
        if isinstance(item, str):
            fragments.append(item.strip())
        elif isinstance(item, dict):
            fragments.append(" ".join(str(value).strip() for value in item.values() if str(value).strip()))
    excerpt = " ".join(fragment for fragment in fragments if fragment)
    return excerpt[:800].strip()


def _tokenize_text(text: str) -> set[str]:
    return set(TOKEN_PATTERN.findall(text.lower()))


def _score_skill(skill: Skill, *, query_tokens: set[str], raw_query: str) -> float:
    if not query_tokens:
        return 0.0

    name_tokens = _tokenize_text(skill.name.replace("_", " "))
    description_tokens = _tokenize_text(skill.description or "")
    trigger_tokens = _tokenize_text(" ".join(skill.triggers_json or []))
    tool_tokens = _tokenize_text(" ".join(skill.tools_allowed_json or []))
    failure_tokens = _tokenize_text(" ".join(skill.failure_modes_json or []))
    procedure_tokens = _tokenize_text(_flatten_json_text(skill.procedure_json or []))
    input_tokens = _tokenize_text(_flatten_json_text(skill.inputs_json or []))
    checklist_tokens = _tokenize_text(" ".join(skill.evaluation_checklist_json or []))

    raw_query_lower = raw_query.lower()
    score = 0.0
    score += len(query_tokens & name_tokens) * 4.0
    score += len(query_tokens & description_tokens) * 3.0
    score += len(query_tokens & procedure_tokens) * 2.5
    score += len(query_tokens & failure_tokens) * 2.0
    score += len(query_tokens & checklist_tokens) * 1.8
    score += len(query_tokens & trigger_tokens) * 1.5
    score += len(query_tokens & tool_tokens) * 1.2
    score += len(query_tokens & input_tokens) * 1.0
    if any(trigger.lower() in raw_query_lower for trigger in (skill.triggers_json or [])):
        score += 2.0
    if skill.name.replace("_", " ") in raw_query_lower:
        score += 2.0
    return score


def _flatten_json_text(value: list[Any]) -> str:
    fragments: list[str] = []
    for item in value:
        if isinstance(item, str):
            fragments.append(item)
            continue
        if isinstance(item, dict):
            fragments.extend(str(part) for part in item.values())
    return " ".join(fragments)


async def _get_latest_skill(session: AsyncSession, *, workspace_id: UUID, name: str) -> Skill | None:
    return await session.scalar(
        select(Skill)
        .where(Skill.workspace_id == workspace_id, Skill.name == name)
        .order_by(Skill.version.desc())
        .limit(1)
    )


async def _get_skill_by_name_version(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    name: str,
    version: int,
) -> Skill | None:
    return await session.scalar(
        select(Skill)
        .where(Skill.workspace_id == workspace_id, Skill.name == name, Skill.version == version)
        .limit(1)
    )


async def _get_pending_proposal(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    name: str,
    version: int,
    content_hash: str,
) -> SkillProposal | None:
    return await session.scalar(
        select(SkillProposal)
        .where(
            SkillProposal.workspace_id == workspace_id,
            SkillProposal.name == name,
            SkillProposal.version == version,
            SkillProposal.content_hash == content_hash,
            SkillProposal.status == "pending",
        )
        .limit(1)
    )


async def _get_latest_approval_for_proposal(
    session: AsyncSession,
    proposal_id: UUID,
) -> Approval | None:
    return await session.scalar(
        select(Approval)
        .where(Approval.skill_proposal_id == proposal_id)
        .order_by(Approval.created_at.desc())
        .limit(1)
    )


async def _publish_skill_definition(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    definition: SkillDefinition,
    approved_by_user_id: UUID | None,
) -> Skill:
    exact_skill = await _get_skill_by_name_version(
        session,
        workspace_id=workspace_id,
        name=definition.name,
        version=definition.version,
    )
    if exact_skill is not None:
        if _hash_existing_skill(exact_skill) != definition.content_hash:
            raise ValueError(
                f"Skill {_skill_label(definition.name, definition.version)} already exists with different content."
            )
        return exact_skill

    latest_skill = await _get_latest_skill(session, workspace_id=workspace_id, name=definition.name)
    if latest_skill is not None and definition.version <= latest_skill.version:
        raise ValueError(
            f"Skill {_skill_label(definition.name, definition.version)} must be newer than version {latest_skill.version}."
        )

    active_versions = await session.execute(
        select(Skill).where(
            Skill.workspace_id == workspace_id,
            Skill.name == definition.name,
            Skill.status == "active",
        )
    )
    for active_skill in active_versions.scalars().all():
        active_skill.status = "superseded"

    now = datetime.now(timezone.utc)
    skill = Skill(
        workspace_id=workspace_id,
        name=definition.name,
        version=definition.version,
        description=definition.description,
        yaml_definition=definition.yaml_definition,
        source_path=definition.source_path,
        content_hash=definition.content_hash,
        triggers_json=list(definition.triggers),
        inputs_json=[dict(item) for item in definition.inputs],
        procedure_json=[item for item in definition.procedure],
        tools_allowed_json=list(definition.tools_allowed),
        risk_notes_json=list(definition.risk_notes),
        failure_modes_json=list(definition.failure_modes),
        evaluation_checklist_json=list(definition.evaluation_checklist),
        status="active",
        approved_by_user_id=approved_by_user_id,
        approved_at=now,
        metadata_json={"source_path": definition.source_path},
    )
    session.add(skill)
    await session.flush()
    return skill


async def _create_skill_proposal_record(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    definition: SkillDefinition,
    requested_by_user_id: UUID | None,
    reason: str | None,
) -> SkillProposal:
    latest_skill = await _get_latest_skill(session, workspace_id=workspace_id, name=definition.name)
    change_type = "create" if latest_skill is None else "update"
    proposal = SkillProposal(
        workspace_id=workspace_id,
        base_skill_id=latest_skill.id if latest_skill is not None else None,
        requested_by_user_id=requested_by_user_id,
        name=definition.name,
        version=definition.version,
        change_type=change_type,
        description=definition.description,
        yaml_definition=definition.yaml_definition,
        source_path=definition.source_path,
        content_hash=definition.content_hash,
        triggers_json=list(definition.triggers),
        inputs_json=[dict(item) for item in definition.inputs],
        procedure_json=[item for item in definition.procedure],
        tools_allowed_json=list(definition.tools_allowed),
        risk_notes_json=list(definition.risk_notes),
        failure_modes_json=list(definition.failure_modes),
        evaluation_checklist_json=list(definition.evaluation_checklist),
        status="pending",
        reason=reason,
        metadata_json={"source_path": definition.source_path},
    )
    session.add(proposal)
    await session.flush()

    approval = Approval(
        workspace_id=workspace_id,
        skill_proposal_id=proposal.id,
        requested_by_user_id=requested_by_user_id,
        status="pending",
        reason=reason or f"Review skill proposal {_skill_label(definition.name, definition.version)}.",
        metadata_json={
            "type": "skill_proposal",
            "skill_name": definition.name,
            "skill_version": definition.version,
            "change_type": change_type,
        },
    )
    session.add(approval)
    await session.flush()
    return proposal


def _hash_existing_skill(skill: Skill) -> str:
    if skill.content_hash:
        return skill.content_hash
    parsed = parse_skill_yaml(skill.yaml_definition, source_path=skill.source_path)
    return parsed.content_hash


def _skill_label(name: str, version: int) -> str:
    return f"{name}@v{version}"