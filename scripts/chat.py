"""
agent-sam chat — fullscreen Hermes-style interactive chat CLI.

Supports:
  - Full conversation with Agent Sam
  - Task creation, approval, queue management
  - Inline answers and task results
  - Clean fullscreen terminal UI
"""
from __future__ import annotations

import asyncio
import os
import sys
import textwrap
from pathlib import Path
from typing import Any
from uuid import UUID

# Ensure the app directory is on sys.path and .env is loaded
_APP_DIR = Path(os.environ.get("APP_DIR", "/opt/agent-sam"))
if _APP_DIR.exists() and str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

_ENV_FILE = _APP_DIR / ".env"
if _ENV_FILE.exists():
    from dotenv import load_dotenv
    load_dotenv(_ENV_FILE, override=False)

_BANNER = r"""
  ___                     _     ____
 / _ \   __ _   ___  _ __| |_  / ___|  __ _  _ __ ___
| | | | / _` | / _ \| '_ \ __| \___ \ / _` || '_ ` _ \
| |_| || (_| ||  __/| | | | |_   ___) | (_| || | | | | |
 \___/  \__, | \___||_| |_|\__| |____/ \__,_||_| |_| |_|
        |___/
"""

_DIVIDER = "─" * 72
_PENDING_APPROVALS: dict[str, str] = {}  # approval_id -> task_title


def _clear() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def _print_header() -> None:
    print(_BANNER)
    print("  Agent Sam — AI Agent OS  |  type /help for commands  |  /exit to quit")
    print(_DIVIDER)
    print()


def _wrap(text: str, indent: int = 0) -> str:
    prefix = " " * indent
    return "\n".join(
        textwrap.fill(line, width=72, initial_indent=prefix, subsequent_indent=prefix)
        if len(line) > 72 - indent else prefix + line
        for line in text.splitlines()
    )


def _print_agent(text: str) -> None:
    print()
    print("  \033[36mAgent Sam\033[0m")
    for line in text.strip().splitlines():
        print(f"    {line}")
    print()


def _print_user(text: str) -> None:
    print(f"  \033[33mYou\033[0m: {text}")


def _print_task(task_id: str, title: str, agent: str, reason: str) -> None:
    print()
    print(f"  \033[32m✓ Task queued\033[0m")
    print(f"    Title : {title[:64]}")
    print(f"    Agent : {agent}")
    print(f"    Reason: {reason[:80]}")
    print(f"    ID    : {task_id}")
    print()


def _print_approval(approval_id: str, command: str, task_title: str) -> None:
    print()
    print(f"  \033[33m⚠ Approval required\033[0m")
    print(f"    Task   : {task_title[:64]}")
    print(f"    Command: {command[:80]}")
    print(f"    ID     : {approval_id}")
    print()
    print("  Type \033[36mapprove\033[0m or \033[36m/approve\033[0m to continue")
    print()
    _PENDING_APPROVALS[approval_id] = task_title


def _print_result(text: str, status: str = "completed") -> None:
    if status == "completed":
        icon = "\033[32m✓\033[0m"
    elif status in {"paused", "pending_approval"}:
        icon = "\033[33m⏸\033[0m"
    else:
        icon = "\033[31m✗\033[0m"
    print()
    print(f"  {icon} Result")
    for line in text.strip().splitlines():
        print(f"    {line}")
    print()


async def _handle_approval_text(text: str, settings: Any, session_factory: Any) -> bool:
    """Return True if we handled an approval, False otherwise."""
    from gateways.base import GatewayChat, GatewayUser, IncomingGatewayMessage
    from gateways.service import AgentGatewayService

    normalized = text.strip().lower()

    # If there's exactly one pending approval and user just says 'approve' or 'yes'
    if normalized in {"approve", "yes", "y", "/approve"} and len(_PENDING_APPROVALS) == 1:
        approval_id = next(iter(_PENDING_APPROVALS))
        task_title = _PENDING_APPROVALS[approval_id]
        return await _run_approve(approval_id, task_title, settings, session_factory)

    # If user gives /approve <id>
    if normalized.startswith("/approve ") or normalized.startswith("approve "):
        parts = text.strip().split(None, 1)
        if len(parts) == 2:
            approval_id = parts[1].strip()
            task_title = _PENDING_APPROVALS.get(approval_id, "unknown task")
            return await _run_approve(approval_id, task_title, settings, session_factory)

    return False


async def _run_approve(approval_id: str, task_title: str, settings: Any, session_factory: Any) -> bool:
    from gateways.base import GatewayChat, GatewayUser, IncomingGatewayMessage
    from gateways.service import AgentGatewayService

    service = AgentGatewayService(session_factory, settings=settings)
    incoming = IncomingGatewayMessage(
        workspace_id=settings.default_workspace_id,
        user_id=settings.default_user_id,
        gateway_name="cli",
        gateway_user=GatewayUser(gateway_user_id="cli-user"),
        gateway_chat=GatewayChat(gateway_chat_id="cli-chat"),
        text=f"/approve {approval_id}",
    )
    try:
        response = await asyncio.wait_for(
            service.handle_incoming_message(incoming),
            timeout=15.0,
        )
        _PENDING_APPROVALS.pop(approval_id, None)
        _print_agent(f"Approved. Continuing task: {task_title}")

        # Immediately resume the task by finding and re-queueing it
        try:
            from db.task_queue import list_task_queue, resume_task
            import contextlib
            async with session_factory() as session:
                paused = await list_task_queue(session, statuses=("paused",), limit=5)
                for task in paused:
                    meta = task.metadata_json or {}
                    if str(task.created_by_user_id) == str(settings.default_user_id):
                        await resume_task(session, task_id=task.id)
                        _print_agent(f"Task resumed and queued for execution.")
                        break
        except Exception:
            pass

        return True
    except Exception as exc:
        _print_agent(f"Approval failed: {exc}")
        return False


async def _send_message(text: str, settings: Any, session_factory: Any) -> None:
    from gateways.base import GatewayChat, GatewayUser, IncomingGatewayMessage
    from gateways.service import AgentGatewayService

    service = AgentGatewayService(session_factory, settings=settings)
    incoming = IncomingGatewayMessage(
        workspace_id=settings.default_workspace_id,
        user_id=settings.default_user_id,
        gateway_name="cli",
        gateway_user=GatewayUser(
            gateway_user_id="cli-user",
            display_name="You",
        ),
        gateway_chat=GatewayChat(
            gateway_chat_id="cli-chat",
            title="CLI Chat",
            chat_type="terminal",
        ),
        text=text,
    )
    try:
        response = await asyncio.wait_for(
            service.handle_incoming_message(incoming),
            timeout=30.0,
        )
        if response.task_id is not None:
            _print_task(
                str(response.task_id),
                text[:64],
                "",
                "",
            )
        if response.should_reply and response.text:
            txt = response.text

            # Extract task creation info
            if "Agent:" in txt and "Why:" in txt:
                lines = txt.splitlines()
                title_line = next((l for l in lines if "Task ID:" in l), None)
                agent_line = next((l for l in lines if l.strip().startswith("Agent:")), None)
                why_line = next((l for l in lines if l.strip().startswith("Why:")), None)
                task_id_line = next((l for l in lines if "Task ID:" in l), None)
                task_id = task_id_line.split(":", 1)[1].strip() if task_id_line else (str(response.task_id) if response.task_id else "")
                agent = agent_line.split(":", 1)[1].strip() if agent_line else ""
                reason = why_line.split(":", 1)[1].strip() if why_line else ""
                _print_task(task_id, text[:64], agent, reason)
            elif "Approval ID:" in txt:
                # Extract approval info
                lines = txt.splitlines()
                approval_id = ""
                command = ""
                for line in lines:
                    if "Approval ID:" in line:
                        approval_id = line.split("Approval ID:", 1)[1].strip().rstrip(".")
                    if "`" in line:
                        parts = line.split("`")
                        if len(parts) >= 2:
                            command = parts[1]
                if approval_id:
                    _print_approval(approval_id, command, text[:64])
                else:
                    _print_agent(txt)
            else:
                _print_agent(txt)

            # Check if result contains previous task output
            if any(kw in txt.lower() for kw in ("task completed:", "i found", "i checked", "result:")):
                pass  # already printed nicely above

    except asyncio.TimeoutError:
        _print_agent("The request timed out. The worker may still be processing it. Type 'update' to check status.")
    except Exception as exc:
        _print_agent(f"Error: {exc}")


async def _show_queue(session_factory: Any) -> None:
    from db.task_queue import get_queue_status, list_task_queue

    async with session_factory() as session:
        status = await get_queue_status(session)
        tasks = await list_task_queue(session, statuses=("pending", "running", "paused"), limit=10)

    total = status.get("pending", 0) + status.get("running", 0) + status.get("paused", 0)
    if total == 0:
        _print_agent("Queue is empty — no active tasks.")
        return

    lines = [f"{total} active tasks:"]
    icons = {"pending": "⏳", "running": "⚙️", "paused": "⏸"}
    for task in tasks:
        icon = icons.get(task.status, "•")
        lines.append(f"  {icon} [{task.status:8}] {task.title[:55]}  {str(task.id)[:8]}…")
    _print_agent("\n".join(lines))


async def _main() -> None:
    _clear()
    _print_header()
    _print_agent("Hello! I'm Agent Sam. Ask me anything or give me a task to work on. Type /help for commands.")

    try:
        from app.config import get_settings
        from db.session import AsyncSessionLocal

        settings = get_settings()
        session_factory = AsyncSessionLocal
    except Exception as exc:
        _print_agent(f"Failed to load configuration: {exc}")
        _print_agent("Make sure you are in /opt/agent-sam with the .env file present.")
        return

    if settings.default_workspace_id is None or settings.default_user_id is None:
        _print_agent(
            "DEFAULT_WORKSPACE_ID or DEFAULT_USER_ID is not set in .env.\n"
            "Run: cd /opt/agent-sam && python -m scripts.setup\n"
            "or check your .env file."
        )
        return

    while True:
        try:
            user_input = input("  \033[33mYou\033[0m: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            _print_agent("Goodbye.")
            break

        if not user_input:
            continue

        nl = user_input.lower()

        if nl in {"/exit", "/quit", "exit", "quit"}:
            _print_agent("Goodbye.")
            break

        if nl in {"/help", "help"}:
            _print_agent(
                "Commands:\n"
                "  /queue         — show task queue\n"
                "  /queue clear   — cancel all tasks\n"
                "  /status        — show latest task status\n"
                "  approve        — approve pending action (use when prompted)\n"
                "  /approve <id>  — approve by specific ID\n"
                "  /agents        — list specialist agents\n"
                "  /exit          — exit chat\n"
                "\nOr just type naturally to chat or create tasks."
            )
            continue

        if nl in {"/queue"} or nl.startswith("/queue"):
            sub = nl[6:].strip()
            if sub == "clear":
                await _send_message("Remove all", settings, session_factory)
            else:
                await _show_queue(session_factory)
            continue

        if nl == "/agents":
            await _send_message("/agents", settings, session_factory)
            continue

        if nl == "/status":
            await _send_message("What is the progress", settings, session_factory)
            continue

        # Handle approval shorthand
        handled = await _handle_approval_text(user_input, settings, session_factory)
        if handled:
            continue

        await _send_message(user_input, settings, session_factory)


def run() -> None:
    """Entry point for agent-sam chat command."""
    # If running on Linux outside the app dir, change to app dir
    if sys.platform.startswith("linux"):
        app_dir = Path(os.environ.get("APP_DIR", "/opt/agent-sam"))
        if app_dir.exists():
            os.chdir(str(app_dir))

    asyncio.run(_main())


if __name__ == "__main__":
    run()
