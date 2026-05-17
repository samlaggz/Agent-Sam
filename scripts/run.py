from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from typing import Callable
from uuid import UUID

from scripts.common import PROJECT_ROOT, format_command


PromptFunc = Callable[[str], str]
OutputFunc = Callable[[str], None]

MENU_OPTIONS = {
    "1": "Setup / Reconfigure",
    "2": "Doctor",
    "3": "Start API",
    "4": "Start Worker",
    "5": "Start Gateways",
    "6": "Start CLI Gateway",
    "7": "Show Agents",
    "8": "Show Models",
    "9": "Show Budgets",
    "10": "Show Pending Skill Proposals",
    "11": "Show Pending Sub-Agent Proposals",
    "12": "Approve Skill Proposal",
    "13": "Approve Sub-Agent Proposal",
    "14": "Exit",
}
INLINE_COMMANDS = {
    "1": [sys.executable, "-m", "scripts.setup"],
    "2": [sys.executable, "-m", "scripts.doctor"],
}
RUNTIME_COMMANDS = {
    "3": [sys.executable, "-m", "app.main"],
    "4": [sys.executable, "-m", "workers.main"],
    "5": [sys.executable, "-m", "gateways.runner"],
    "6": [sys.executable, "-m", "gateways.cli.main"],
}


def run() -> None:
    parser = argparse.ArgumentParser(description="Launch Agent_Sam runtime and operator commands from the control center.")
    parser.parse_args()
    raise SystemExit(run_menu())


def run_menu(*, prompt: PromptFunc = input, output: OutputFunc = print) -> int:
    output("Agent_Sam Control Center")
    while True:
        for choice, label in MENU_OPTIONS.items():
            output(f"{choice}. {label}")

        selected = prompt("Select an option: ").strip()
        if selected not in MENU_OPTIONS:
            output("Choose a valid menu option.")
            continue

        if selected == "14":
            output("Exiting control center.")
            return 0

        label = MENU_OPTIONS[selected]
        try:
            if selected in RUNTIME_COMMANDS:
                _launch_runtime_command(RUNTIME_COMMANDS[selected], label=label, output=output)
                continue
            if selected in INLINE_COMMANDS:
                _run_inline_command(INLINE_COMMANDS[selected], label=label, output=output)
                continue
            if selected == "7":
                _show_agents(output=output)
                continue
            if selected == "8":
                _show_models(output=output)
                continue
            if selected == "9":
                asyncio.run(_show_budgets(output=output))
                continue
            if selected == "10":
                asyncio.run(_show_pending_skill_proposals(output=output))
                continue
            if selected == "11":
                asyncio.run(_show_pending_sub_agent_proposals(output=output))
                continue
            if selected == "12":
                proposal_id = prompt("Skill proposal ID: ").strip()
                asyncio.run(_approve_skill_proposal_by_id(proposal_id, prompt=prompt, output=output))
                continue
            if selected == "13":
                proposal_id = prompt("Sub-agent proposal ID: ").strip()
                asyncio.run(_approve_sub_agent_proposal_by_id(proposal_id, prompt=prompt, output=output))
                continue
        except Exception as exc:
            output(f"{label} failed: {exc}")


def _run_inline_command(command: list[str], *, label: str, output: OutputFunc) -> None:
    completed = subprocess.run(command, cwd=str(PROJECT_ROOT), text=True, check=False)
    if completed.returncode != 0:
        output(f"{label} exited with code {completed.returncode}")


def _show_agents(*, output: OutputFunc) -> None:
    from agents.registry import list_agents

    output("Available agents:")
    for profile in list_agents():
        output(f"- {profile.slug}: {profile.description}")


def _show_models(*, output: OutputFunc) -> None:
    from agents.registry import list_agents
    from app.config import get_settings
    from db.session import AsyncSessionLocal
    from services.model_router import ModelRouter

    router = ModelRouter(get_settings(), AsyncSessionLocal)
    output("Configured models:")
    for model_name in router.list_configured_models(list_agents()):
        output(f"- {model_name}")


async def _show_budgets(*, output: OutputFunc) -> None:
    from sqlalchemy import select

    from agents.registry import list_agents
    from app.config import get_settings
    from db.models import ModelBudget
    from db.session import AsyncSessionLocal

    settings = get_settings()
    output(f"Global max cost per task: ${settings.max_cost_per_task_usd:.2f}")
    output(f"Daily model budget: ${settings.daily_model_budget_usd:.2f}")
    output("Per-agent budgets:")
    for profile in list_agents():
        output(f"- {profile.slug}: ${profile.max_cost_per_task_usd:.2f} default={profile.default_model}")

    async with AsyncSessionLocal() as session:
        rows = list(
            (
                await session.execute(
                    select(ModelBudget).order_by(ModelBudget.created_at.desc()).limit(10)
                )
            ).scalars()
        )
    if not rows:
        output("No budget records yet.")
        return

    output("Recent budget records:")
    for row in rows:
        output(f"- {row.scope}:{row.scope_key} spent=${row.spent_usd:.2f} / ${row.budget_usd:.2f}")


async def _show_pending_skill_proposals(*, output: OutputFunc) -> None:
    from sqlalchemy import select

    from db.models import SkillProposal
    from db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        proposals = list(
            (
                await session.execute(
                    select(SkillProposal)
                    .where(SkillProposal.status == "pending")
                    .order_by(SkillProposal.created_at.asc())
                )
            ).scalars()
        )
    if not proposals:
        output("No pending skill proposals.")
        return

    output("Pending skill proposals:")
    for proposal in proposals:
        output(
            f"- {proposal.id}: {proposal.name} v{proposal.version}"
            f" agent={proposal.agent_slug or 'unknown'} reason={proposal.reason or 'n/a'}"
        )


async def _show_pending_sub_agent_proposals(*, output: OutputFunc) -> None:
    from sqlalchemy import select

    from db.models import SubAgentProposal
    from db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        proposals = list(
            (
                await session.execute(
                    select(SubAgentProposal)
                    .where(SubAgentProposal.status == "pending")
                    .order_by(SubAgentProposal.created_at.asc())
                )
            ).scalars()
        )
    if not proposals:
        output("No pending sub-agent proposals.")
        return

    output("Pending sub-agent proposals:")
    for proposal in proposals:
        output(
            f"- {proposal.id}: {proposal.proposed_slug}"
            f" parent={proposal.parent_agent_slug} reason={proposal.reason}"
        )


async def _approve_skill_proposal_by_id(
    proposal_id_text: str,
    *,
    prompt: PromptFunc,
    output: OutputFunc,
) -> None:
    from db.session import AsyncSessionLocal
    from services.agent_learning_service import approve_skill_proposal

    proposal_id = _parse_uuid(proposal_id_text)
    if proposal_id is None:
        output("Skill proposal IDs must be valid UUID values.")
        return

    reviewer_user_id = _resolve_reviewer_user_id(prompt=prompt, output=output)
    if reviewer_user_id is None:
        return

    async with AsyncSessionLocal() as session:
        proposal = await approve_skill_proposal(session, proposal_id=proposal_id, reviewed_by_user_id=reviewer_user_id)
    output(f"Skill proposal {proposal.id} approved.")


async def _approve_sub_agent_proposal_by_id(
    proposal_id_text: str,
    *,
    prompt: PromptFunc,
    output: OutputFunc,
) -> None:
    from db.session import AsyncSessionLocal
    from services.agent_learning_service import approve_sub_agent_proposal

    proposal_id = _parse_uuid(proposal_id_text)
    if proposal_id is None:
        output("Sub-agent proposal IDs must be valid UUID values.")
        return

    reviewer_user_id = _resolve_reviewer_user_id(prompt=prompt, output=output)
    if reviewer_user_id is None:
        return

    async with AsyncSessionLocal() as session:
        proposal = await approve_sub_agent_proposal(session, proposal_id=proposal_id, reviewed_by_user_id=reviewer_user_id)
    output(f"Sub-agent proposal {proposal.id} approved.")


def _resolve_reviewer_user_id(*, prompt: PromptFunc, output: OutputFunc) -> UUID | None:
    from app.config import get_settings

    settings = get_settings()
    if settings.default_user_id is not None:
        return settings.default_user_id

    raw_value = prompt("Reviewer user ID: ").strip()
    reviewer_user_id = _parse_uuid(raw_value)
    if reviewer_user_id is None:
        output("Reviewer user IDs must be valid UUID values.")
        return None
    return reviewer_user_id


def _parse_uuid(raw_value: str) -> UUID | None:
    try:
        return UUID(raw_value)
    except ValueError:
        return None


def _launch_runtime_command(command: list[str], *, label: str, output: OutputFunc) -> None:
    if sys.platform.startswith("win"):
        power_shell_command = f"Set-Location '{PROJECT_ROOT}'; {format_command(command)}"
        creation_flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        subprocess.Popen(
            ["powershell", "-NoExit", "-Command", power_shell_command],
            cwd=str(PROJECT_ROOT),
            creationflags=creation_flags,
        )
        output(f"Launched {label} in a new PowerShell window.")
        return

    output(f"Run this command in a separate terminal: {format_command(command)}")


if __name__ == "__main__":
    run()