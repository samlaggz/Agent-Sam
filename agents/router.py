from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from agents.base import AgentDecision, AgentProfile, AgentRunContext, SpecialistAgent
from agents.registry import find_agents_for_task, get_agent_profile, load_agent_profiles


@dataclass(frozen=True)
class RouteRequest:
    title: str
    description: str = ""
    metadata: Mapping[str, Any] | None = None
    requested_agent_slug: str | None = None
    premium_requested: bool = False
    safety_critical: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "metadata": dict(self.metadata or {}),
            "requested_agent_slug": self.requested_agent_slug,
            "premium_requested": self.premium_requested,
            "safety_critical": self.safety_critical,
        }


class RouterAgent(SpecialistAgent):
    def __init__(self) -> None:
        profiles = load_agent_profiles()
        super().__init__(profiles.get("router_agent") or get_agent_profile("router_agent"))
        self._profiles = profiles

    def route(self, request: RouteRequest) -> AgentDecision:
        payload = request.to_payload()
        requested_slug = request.requested_agent_slug
        if requested_slug and requested_slug in self._profiles and requested_slug != self.profile.slug:
            chosen_profile = self._profiles[requested_slug]
            confidence = 0.98
            reason = f"User explicitly requested {requested_slug}."
        else:
            matches = tuple(profile for profile in find_agents_for_task(payload, self._profiles) if profile.slug != self.profile.slug)
            if matches:
                chosen_profile = matches[0]
            else:
                chosen_profile = _infer_best_fallback(payload, self._profiles)
            confidence = _estimate_confidence(payload, chosen_profile)
            reason = _build_reason(payload, chosen_profile)

        model = _choose_model(chosen_profile, payload)
        requires_human_approval = request.safety_critical or chosen_profile.risk_level == "high"
        fallback_agents = tuple(
            profile.slug
            for profile in find_agents_for_task(payload, self._profiles)
            if profile.slug not in {self.profile.slug, chosen_profile.slug}
        )[:3]
        return AgentDecision(
            agent_slug=chosen_profile.slug,
            confidence=confidence,
            reason=reason,
            model=model,
            estimated_cost_level=_cost_level_for_model(model),
            requires_human_approval=requires_human_approval,
            fallback_agent_slugs=fallback_agents,
            metadata={"risk_level": chosen_profile.risk_level},
        )

    async def run(self, context: AgentRunContext):
        decision = self.route(
            RouteRequest(
                title=context.title,
                description=context.description,
                metadata=context.metadata,
                requested_agent_slug=context.requested_agent_slug,
                premium_requested=context.premium_requested,
            )
        )
        from agents.base import AgentResult

        return AgentResult(
            agent_slug=self.profile.slug,
            status="completed",
            summary=decision.reason,
            structured_output=decision.to_json(),
            model_used=decision.model,
        )


def _choose_model(profile: AgentProfile, payload: Mapping[str, Any]) -> str:
    title = str(payload.get("title", "")).lower()
    description = str(payload.get("description", "")).lower()
    premium_requested = bool(payload.get("premium_requested", False))
    if premium_requested or any(keyword in f"{title} {description}" for keyword in ("critical", "production", "safety", "severe")):
        return profile.escalation_model
    return profile.default_model


def _estimate_confidence(payload: Mapping[str, Any], profile: AgentProfile) -> float:
    haystack = f"{payload.get('title', '')} {payload.get('description', '')}".lower()
    matches = sum(1 for task_type in profile.task_types if task_type.replace("_", " ") in haystack)
    if matches >= 2:
        return 0.92
    if matches == 1:
        return 0.84
    return 0.72


def _build_reason(payload: Mapping[str, Any], profile: AgentProfile) -> str:
    haystack = f"{payload.get('title', '')} {payload.get('description', '')}".lower()
    keyword = next((task_type for task_type in profile.task_types if task_type.replace("_", " ") in haystack), None)
    if keyword is not None:
        return f"Matched task intent '{keyword}' to {profile.slug}."
    return f"Selected {profile.slug} as the cheapest suitable specialist for the task content."


def _infer_best_fallback(payload: Mapping[str, Any], profiles: Mapping[str, Any]) -> Any:
    haystack = f"{payload.get('title', '')} {payload.get('description', '')}".lower()
    server_ops_keywords = (
        "folder", "file", "directory", "path", "find", "ls", "pwd",
        "server", "linux", "/var", "/etc", "/opt", "process", "service",
        "nginx", "apache", "systemd", "shiva", "drive", "site", "/www",
    )
    if any(kw in haystack for kw in server_ops_keywords):
        return profiles.get("server_ops_agent") or profiles["coding_agent"]
    return profiles.get("coding_agent") or next(iter(profiles.values()))


def _cost_level_for_model(model: str) -> str:
    lowered = model.lower()
    if any(token in lowered for token in ("mini", "haiku", "flash", "small")):
        return "low"
    if any(token in lowered for token in ("sonnet", "medium", "large", "turbo")):
        return "medium"
    return "high"