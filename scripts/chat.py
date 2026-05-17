"""
agent-sam chat — fullscreen Hermes-style interactive chat CLI.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

# Ensure app dir and .env are loaded before anything else
_APP_DIR = Path(os.environ.get("APP_DIR", "/opt/agent-sam"))
if _APP_DIR.exists() and str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

_ENV_FILE = _APP_DIR / ".env"
if not _ENV_FILE.exists():
    _ENV_FILE = Path.cwd() / ".env"
try:
    from dotenv import load_dotenv
    if _ENV_FILE.exists():
        load_dotenv(_ENV_FILE, override=False)
except ImportError:
    pass

# Suppress noisy LiteLLM warnings in the chat terminal
import logging as _logging
_logging.getLogger("LiteLLM").setLevel(_logging.ERROR)
_logging.getLogger("litellm").setLevel(_logging.ERROR)
_logging.getLogger("httpx").setLevel(_logging.WARNING)
_logging.getLogger("httpcore").setLevel(_logging.WARNING)

_BANNER = r"""
  ___                     _     ____
 / _ \   __ _   ___  _ __| |_  / ___|  __ _  _ __ ___
| | | | / _` | / _ \| '_ \ __| \___ \ / _` || '_ ` _ \
| |_| || (_| ||  __/| | | | |_   ___) | (_| || | | | | |
 \___/  \__, | \___||_| |_|\__| |____/ \__,_||_| |_| |_|
        |___/
"""

_DIVIDER = "─" * 72
TASK_POLL_TIMEOUT = 120.0
TASK_POLL_INTERVAL = 2.0
_LAST_TASK_ID: str | None = None
_PENDING_APPROVALS: dict[str, str] = {}  # approval_id -> task_title

_BULK_CANCEL_PHRASES = frozenset({
    "delete them", "cancel them", "remove them",
    "clear all", "cancel all", "delete all", "remove all",
    "clear queue", "cancel all tasks", "delete all tasks", "remove all tasks",
})


def _clear() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def _print_header() -> None:
    print(_BANNER)
    print("  Agent Sam — AI Agent OS  |  /help for commands  |  /exit to quit")
    print(_DIVIDER)
    print()


def _print_agent(text: str) -> None:
    print()
    print("  \033[36mAgent Sam\033[0m")
    for line in text.strip().splitlines():
        print(f"    {line}")
    print()


def _print_task_queued(task_id: str, agent: str, reason: str) -> None:
    short = reason[:76] if reason else "routed"
    print(f"  \033[32m⚙ Working...\033[0m  [{agent or 'routing'}]  {short}")


def _print_result_block(title: str, text: str, status: str) -> None:
    icons = {"completed": "\033[32m✓\033[0m", "failed": "\033[31m✗\033[0m"}
    icon = icons.get(status, "\033[33m⏸\033[0m")
    print()
    print(f"  {icon} {title[:60]}")
    for line in text.strip().splitlines():
        print(f"    {line}")
    print()


def _print_approval_needed(approval_id: str, command: str, task_title: str) -> None:
    global _PENDING_APPROVALS
    print()
    print("  \033[33m⚠ Approval needed\033[0m")
    print(f"    Task   : {task_title[:60]}")
    if command:
        print(f"    Command: {command[:76]}")
    print(f"    ID     : {approval_id}")
    print()
    print("  → Type \033[36mapprove\033[0m to continue")
    print()
    _PENDING_APPROVALS[approval_id] = task_title


def _make_incoming(text: str, settings: Any) -> Any:
    from gateways.base import GatewayChat, GatewayUser, IncomingGatewayMessage
    return IncomingGatewayMessage(
        workspace_id=settings.default_workspace_id,
        user_id=settings.default_user_id,
        gateway_name="cli",
        gateway_user=GatewayUser(gateway_user_id="cli-root", display_name="You"),
        gateway_chat=GatewayChat(gateway_chat_id="cli-chat", title="CLI Chat", chat_type="terminal"),
        text=text,
    )


async def _poll_task_result(task_id: UUID, session_factory: Any, service: Any, settings: Any) -> None:
    from db.models import Approval, Message, Task, ToolCall
    from sqlalchemy import select

    deadline = asyncio.get_event_loop().time() + TASK_POLL_TIMEOUT
    last_seen = 0
    printed_approval = False

    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(TASK_POLL_INTERVAL)
        try:
            async with session_factory() as session:
                task = await session.get(Task, task_id)
                if task is None:
                    break

                result = await session.execute(
                    select(Message)
                    .where(Message.task_id == task_id, Message.role == "assistant")
                    .order_by(Message.created_at.asc())
                )
                messages = list(result.scalars().all())

                if task.status == "paused" and not printed_approval:
                    ap_result = await session.execute(
                        select(Approval)
                        .where(Approval.task_id == task_id, Approval.status == "pending")
                        .order_by(Approval.created_at.desc())
                        .limit(1)
                    )
                    approval = ap_result.scalar_one_or_none()
                    if approval is not None:
                        command = ""
                        if approval.tool_call_id is not None:
                            tc = await session.get(ToolCall, approval.tool_call_id)
                            if tc is not None and isinstance(tc.input_payload, dict):
                                command = tc.input_payload.get("command", "")
                        _print_approval_needed(str(approval.id), command, task.title)
                        printed_approval = True
                        continue

                new_msgs = messages[last_seen:]
                for msg in new_msgs:
                    stage = (msg.metadata_json or {}).get("stage", "")
                    if stage == "report_result":
                        _print_result_block(task.title, msg.content, task.status)
                last_seen = len(messages)

                if task.status in {"completed", "failed", "cancelled"}:
                    if last_seen < len(messages):
                        last_result = next(
                            (m for m in reversed(messages)
                             if (m.metadata_json or {}).get("stage") == "report_result"),
                            messages[-1] if messages else None,
                        )
                        if last_result:
                            _print_result_block(task.title, last_result.content, task.status)
                    break
                if printed_approval and task.status not in {"paused"}:
                    printed_approval = False
        except Exception:
            await asyncio.sleep(TASK_POLL_INTERVAL)
            continue


async def _clear_queue(session_factory: Any) -> None:
    from db.task_queue import cancel_task, list_task_queue
    async with session_factory() as session:
        tasks = await list_task_queue(session, statuses=("pending", "running", "paused"), limit=500)
        count = 0
        for task in tasks:
            await cancel_task(session, task_id=task.id)
            count += 1
    _print_agent(f"Done — cancelled {count} task(s). Queue is empty." if count else "Queue is already empty.")


async def _show_queue(session_factory: Any) -> None:
    from db.task_queue import get_queue_status, list_task_queue
    async with session_factory() as session:
        status = await get_queue_status(session)
        tasks = await list_task_queue(session, statuses=("pending", "running", "paused"), limit=15)
    total = status.get("pending", 0) + status.get("running", 0) + status.get("paused", 0)
    if total == 0:
        _print_agent("Queue is empty — no active tasks.")
        return
    icons = {"pending": "⏳", "running": "⚙️", "paused": "⏸"}
    lines = [f"{total} task(s) active:"]
    for task in tasks:
        icon = icons.get(task.status, "•")
        lines.append(f"  {icon} [{task.status:8}] {task.title[:54]}  {str(task.id)[:8]}…")
    _print_agent("\n".join(lines))


async def _do_approve(approval_id: str, service: Any, settings: Any, session_factory: Any) -> None:
    global _PENDING_APPROVALS
    try:
        incoming = _make_incoming(f"/approve {approval_id}", settings)
        await asyncio.wait_for(service.handle_incoming_message(incoming), timeout=15.0)
        task_title = _PENDING_APPROVALS.pop(approval_id, "task")
        _print_agent(f"Approved ✓  Resuming: {task_title}")
        from db.task_queue import list_task_queue, resume_task
        async with session_factory() as session:
            paused = await list_task_queue(session, statuses=("paused",), limit=10)
            for task in paused:
                if str(task.created_by_user_id) == str(settings.default_user_id):
                    await resume_task(session, task_id=task.id)
    except Exception as exc:
        _print_agent(f"Approval failed: {exc}")


async def _handle_approval_text(text: str, settings: Any, session_factory: Any, service: Any) -> bool:
    """Return True if we handled an approval, False otherwise."""
    global _PENDING_APPROVALS
    nl = text.strip().lower()
    if nl in {"approve", "/approve", "yes approve"} and _PENDING_APPROVALS:
        approval_id = next(iter(_PENDING_APPROVALS))
        await _do_approve(approval_id, service, settings, session_factory)
        return True
    if (nl.startswith("/approve ") or nl.startswith("approve ")) and len(text.split()) >= 2:
        approval_id = text.strip().split(None, 1)[1].strip()
        await _do_approve(approval_id, service, settings, session_factory)
        return True
    return False


async def _handle_line(line: str, service: Any, settings: Any, session_factory: Any) -> None:
    global _LAST_TASK_ID, _PENDING_APPROVALS

    stripped = line.strip()
    if not stripped:
        return
    nl = stripped.lower()

    if nl in {"/exit", "/quit", "exit", "quit"}:
        raise SystemExit(0)

    if nl in {"/help", "help"}:
        _print_agent(
            "Commands:\n"
            "  /queue          — show task queue\n"
            "  /queue clear    — cancel all tasks\n"
            "  approve         — approve pending action\n"
            "  deny            — skip pending approval\n"
            "  /agents         — list specialist agents\n"
            "  /status         — show latest task result\n"
            "  /exit           — exit chat\n"
            "\nOr just type naturally."
        )
        return

    if nl in {"/queue", "queue"}:
        await _show_queue(session_factory)
        return

    if nl in {"queue clear", "/queue clear"} or nl in _BULK_CANCEL_PHRASES:
        await _clear_queue(session_factory)
        return

    if nl in {"deny", "skip", "no"} and _PENDING_APPROVALS:
        aid = next(iter(_PENDING_APPROVALS))
        _PENDING_APPROVALS.pop(aid)
        _print_agent("Approval skipped — task remains paused.")
        return

    if await _handle_approval_text(stripped, settings, session_factory, service):
        return

    if nl in {"/agents", "agents"}:
        try:
            from agents.registry import list_agents
            lines = ["Specialist agents:"]
            for p in list_agents():
                if p.slug != "router_agent":
                    lines.append(f"  {p.slug:<28} {p.default_model}")
            _print_agent("\n".join(lines))
        except Exception as exc:
            _print_agent(f"Could not load agents: {exc}")
        return

    if nl in {"/status", "status"}:
        if _LAST_TASK_ID is not None:
            try:
                from db.models import Message, Task
                from sqlalchemy import select

                async with session_factory() as session:
                    task = await session.get(Task, UUID(_LAST_TASK_ID))
                    if task is not None:
                        result_msg_q = await session.execute(
                            select(Message)
                            .where(Message.task_id == task.id, Message.role == "assistant")
                            .order_by(Message.created_at.desc())
                            .limit(1)
                        )
                        msg = result_msg_q.scalar_one_or_none()
                        text = msg.content if msg else f"Status: {task.status}"
                        _print_result_block(task.title, text, task.status)
                        return
            except Exception:
                pass
        _print_agent("No recent task to check status on.")
        return

    # Send to gateway
    try:
        incoming = _make_incoming(stripped, settings)
        resp = await asyncio.wait_for(service.handle_incoming_message(incoming), timeout=30.0)
    except asyncio.TimeoutError:
        _print_agent("Request timed out. Worker may still be processing — type /status to check.")
        return
    except Exception as exc:
        _print_agent(f"Error: {exc}")
        return

    if resp.task_id is not None:
        _LAST_TASK_ID = str(resp.task_id)

    text = resp.text or ""

    # Task was queued — show brief confirmation then poll for result
    if resp.task_id is not None and ("Got it" in text or "queued" in text.lower() or "Task ID:" in text):
        agent, reason = "", ""
        for part in text.splitlines():
            if part.strip().startswith("Agent:"):
                agent = part.split(":", 1)[1].strip()
            elif part.strip().startswith("Why:"):
                reason = part.split(":", 1)[1].strip()
        _print_task_queued(str(resp.task_id), agent, reason)
        await _poll_task_result(resp.task_id, session_factory, service, settings)
        return

    # Approval embedded in response
    if "Approval ID:" in text:
        approval_id, command = "", ""
        for part in text.splitlines():
            if "Approval ID:" in part:
                approval_id = part.split("Approval ID:", 1)[1].strip().rstrip(".")
            if "`" in part:
                ticks = part.split("`")
                if len(ticks) >= 2:
                    command = ticks[1]
        if approval_id:
            _print_approval_needed(approval_id, command, stripped[:64])
            return

    if resp.should_reply and text:
        _print_agent(text)


async def _main() -> None:
    global _LAST_TASK_ID, _PENDING_APPROVALS
    _clear()
    _print_header()

    try:
        from app.config import get_settings
        from db.session import AsyncSessionLocal
        from gateways.service import AgentGatewayService

        settings = get_settings()
        session_factory = AsyncSessionLocal
        service = AgentGatewayService(session_factory, settings=settings)
    except Exception as exc:
        _print_agent(f"Failed to load configuration: {exc}")
        _print_agent(
            "Run from /opt/agent-sam:\n"
            "  cd /opt/agent-sam && source .venv/bin/activate && agent-sam chat"
        )
        return

    if settings.default_workspace_id is None or settings.default_user_id is None:
        _print_agent(
            "DEFAULT_WORKSPACE_ID or DEFAULT_USER_ID is not set.\n"
            "Run: cd /opt/agent-sam && python -m scripts.setup"
        )
        return

    _print_agent(
        "Hello! I'm Agent Sam — your AI agent OS.\n"
        "Give me tasks, ask questions, or manage the queue.\n"
        "Results appear automatically. Type /help for commands."
    )

    # Clean up stale paused subtasks from previous sessions
    try:
        from db.task_queue import cancel_task, list_task_queue
        async with session_factory() as session:
            old = await list_task_queue(session, statuses=("paused",), limit=50)
            stale = [t for t in old if t.parent_task_id is not None]
            for t in stale:
                await cancel_task(session, task_id=t.id)
            if stale:
                _print_agent(f"Cleaned up {len(stale)} stale paused subtasks.")
    except Exception:
        pass

    _JUNK_LINES = frozenset({
        "try:", "checking the connection", "checking the proxy and the firewall",
        "running windows network diagnostics", "err_connection_timed_out",
        "err_connection_refused", "err_name_not_resolved",
    })

    while True:
        try:
            raw_input = input("  \033[33mYou\033[0m: ")
        except (EOFError, KeyboardInterrupt):
            print()
            _print_agent("Goodbye.")
            break

        # Join multi-line paste into a single message and filter out browser junk lines
        lines = [line.strip() for line in raw_input.splitlines() if line.strip()]
        filtered = [line for line in lines if line.lower() not in _JUNK_LINES]
        user_input = " ".join(filtered)
        if not user_input:
            continue

        try:
            await _handle_line(user_input, service, settings, session_factory)
        except SystemExit:
            _print_agent("Goodbye.")
            break
        except Exception as exc:
            _print_agent(f"Unexpected error: {exc}")


def run() -> None:
    if sys.platform.startswith("linux"):
        app_dir = Path(os.environ.get("APP_DIR", "/opt/agent-sam"))
        if app_dir.exists():
            os.chdir(str(app_dir))
    asyncio.run(_main())


if __name__ == "__main__":
    run()
