from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import UUID


@dataclass(frozen=True)
class AgentToolPolicy:
    approval_required_tools: tuple[str, ...] = ()
    web_access_allowed: bool = False
    shell_policy: str = "blocked"
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentCapability:
    agent_slug: str
    task_types: tuple[str, ...]
    tools_allowed: tuple[str, ...]
    memory_types_allowed: tuple[str, ...]
    skill_tags: tuple[str, ...]
    risk_level: str
    tool_policy: AgentToolPolicy = field(default_factory=AgentToolPolicy)


@dataclass(frozen=True)
class AgentProfile:
    name: str
    slug: str
    description: str
    task_types: tuple[str, ...]
    default_model: str
    fallback_models: tuple[str, ...]
    escalation_model: str
    max_cost_per_task_usd: float
    max_tokens_per_run: int
    temperature: float
    tools_allowed: tuple[str, ...]
    memory_types_allowed: tuple[str, ...]
    skill_tags: tuple[str, ...]
    risk_level: str
    system_prompt_path: Path
    evaluation_checklist: tuple[str, ...]
    can_create_subagents: bool
    can_propose_skill_updates: bool


@dataclass(frozen=True)
class AgentRunContext:
    task_id: UUID | None
    task_run_id: UUID | None
    workspace_id: UUID | None
    user_id: UUID | None
    title: str
    description: str
    metadata: Mapping[str, Any]
    requested_agent_slug: str | None = None
    requested_model: str | None = None
    premium_requested: bool = False
    approval_granted: bool = False
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass(frozen=True)
class AgentDecision:
    agent_slug: str
    confidence: float
    reason: str
    model: str
    estimated_cost_level: str
    requires_human_approval: bool
    fallback_agent_slugs: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "agent_slug": self.agent_slug,
            "confidence": round(self.confidence, 4),
            "reason": self.reason,
            "model": self.model,
            "estimated_cost_level": self.estimated_cost_level,
            "requires_human_approval": self.requires_human_approval,
            "fallback_agent_slugs": list(self.fallback_agent_slugs),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class AgentResult:
    agent_slug: str
    status: str
    summary: str
    structured_output: Mapping[str, Any]
    model_used: str
    actual_cost_usd: float | None = None
    estimated_cost_usd: float | None = None
    evaluation_notes: tuple[str, ...] = ()


class SpecialistAgent:
    def __init__(self, profile: AgentProfile, capability: AgentCapability | None = None) -> None:
        self.profile = profile
        self.capability = capability or AgentCapability(
            agent_slug=profile.slug,
            task_types=profile.task_types,
            tools_allowed=profile.tools_allowed,
            memory_types_allowed=profile.memory_types_allowed,
            skill_tags=profile.skill_tags,
            risk_level=profile.risk_level,
            tool_policy=_default_tool_policy(profile),
        )

    async def run(self, context: AgentRunContext) -> AgentResult:
        raise NotImplementedError

    def evaluate(self, result: AgentResult) -> tuple[str, ...]:
        notes: list[str] = []
        for item in self.profile.evaluation_checklist:
            if item.strip():
                notes.append(item)
        return tuple(notes)


def _default_tool_policy(profile: AgentProfile) -> AgentToolPolicy:
    approval_required_tools = tuple(
        tool_name
        for tool_name in profile.tools_allowed
        if tool_name in {"safe_shell", "sql_write", "sql_delete", "service_control"}
    )
    shell_policy = "approval_required" if "safe_shell" in profile.tools_allowed else "blocked"
    return AgentToolPolicy(
        approval_required_tools=approval_required_tools,
        web_access_allowed=any(tool_name.startswith("web_") for tool_name in profile.tools_allowed),
        shell_policy=shell_policy,
    )


def build_agent_capability(profile: AgentProfile) -> AgentCapability:
    return AgentCapability(
        agent_slug=profile.slug,
        task_types=profile.task_types,
        tools_allowed=profile.tools_allowed,
        memory_types_allowed=profile.memory_types_allowed,
        skill_tags=profile.skill_tags,
        risk_level=profile.risk_level,
        tool_policy=_default_tool_policy(profile),
    )