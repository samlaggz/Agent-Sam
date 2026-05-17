"""
agent-sam — Hermes-style single-command CLI for Agent_Sam.

Usage on the server:
    agent-sam                          interactive control center
    agent-sam chat                     open chat CLI (like Hermes chat)
    agent-sam setup                    configure gateways and secrets
    agent-sam start                    start all services (api + worker + gateways)
    agent-sam start api                start API only
    agent-sam start worker             start worker only
    agent-sam start gateways           start all enabled gateways
    agent-sam start telegram           start telegram gateway
    agent-sam stop                     stop all systemd services
    agent-sam status                   show systemd service status
    agent-sam doctor                   run diagnostics
    agent-sam queue                    show task queue
    agent-sam queue clear              cancel all queued tasks
    agent-sam agents                   list specialist agents
    agent-sam models                   list models
    agent-sam logs [service]           tail logs for a service
"""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Callable, NoReturn

from scripts.common import PROJECT_ROOT, format_command


SYSTEMD_SERVICES = ("agent-api", "agent-worker", "agent-telegram")
OutputFunc = Callable[[str], None]

BANNER = """
╔══════════════════════════════════════╗
║        Agent Sam — AI Agent OS       ║
╚══════════════════════════════════════╝
"""

HELP_TEXT = """
Commands:
  chat            Open interactive chat shell
  setup           Configure gateways and tokens
  start           Start services  (api | worker | gateways | telegram | all)
  stop            Stop all systemd services
  status          Show service health
  doctor          Run system diagnostics
  queue           Show task queue
  queue clear     Cancel all queued/paused tasks
  agents          List specialist agents and their models
  models          List all configured models
  logs [svc]      Tail logs (api | worker | telegram)
  help            Show this help

Examples:
  agent-sam                     # interactive control center menu
  agent-sam chat                # Hermes-style chat shell
  agent-sam setup               # configure telegram token, gateways
  agent-sam start               # start everything
  agent-sam queue               # see what is queued
  agent-sam queue clear         # cancel all tasks
  agent-sam logs worker         # tail worker logs
"""


def run(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="agent-sam",
        description="Agent Sam control center",
        add_help=False,
    )
    parser.add_argument("command", nargs="?", default=None)
    parser.add_argument("subcommand", nargs="?", default=None)
    parser.add_argument("--help", "-h", action="store_true")
    args = parser.parse_args(argv)

    if args.help or args.command == "help":
        print(BANNER + HELP_TEXT)
        return

    if args.command is None:
        _interactive_menu()
        return

    cmd = args.command.lower()
    sub = (args.subcommand or "").lower()

    if cmd == "chat":
        _start_chat()
    elif cmd == "setup":
        _run_setup()
    elif cmd == "start":
        _start_services(sub)
    elif cmd == "stop":
        _stop_services()
    elif cmd == "status":
        _show_status()
    elif cmd == "doctor":
        _run_doctor()
    elif cmd == "queue":
        if sub == "clear":
            asyncio.run(_queue_clear())
        else:
            asyncio.run(_queue_show())
    elif cmd == "agents":
        _show_agents()
    elif cmd == "models":
        _show_models()
    elif cmd == "logs":
        _tail_logs(sub or "worker")
    else:
        print(f"Unknown command: {cmd}\nRun 'agent-sam help' for usage.")
        raise SystemExit(1)


# ─────────────────────────────────────────────
# Interactive menu
# ─────────────────────────────────────────────

def _interactive_menu() -> None:
    print(BANNER)
    MENU = {
        "1": ("Chat (interactive CLI)", _start_chat),
        "2": ("Setup / Configure gateways", _run_setup),
        "3": ("Start all services", lambda: _start_services("all")),
        "4": ("Start API", lambda: _start_services("api")),
        "5": ("Start Worker", lambda: _start_services("worker")),
        "6": ("Start Gateways", lambda: _start_services("gateways")),
        "7": ("Show service status", _show_status),
        "8": ("Show task queue", lambda: asyncio.run(_queue_show())),
        "9": ("Clear task queue", lambda: asyncio.run(_queue_clear())),
        "10": ("List agents", _show_agents),
        "11": ("List models", _show_models),
        "12": ("Doctor / Diagnostics", _run_doctor),
        "13": ("Tail worker logs", lambda: _tail_logs("worker")),
        "0": ("Exit", None),
    }
    while True:
        print()
        for key, (label, _) in MENU.items():
            print(f"  {key:>2}. {label}")
        print()
        choice = input("Select: ").strip()
        if choice not in MENU:
            print("  Invalid choice.")
            continue
        label, fn = MENU[choice]
        if fn is None:
            print("Bye.")
            break
        try:
            fn()
        except KeyboardInterrupt:
            print()
        except Exception as exc:
            print(f"  Error: {exc}")


# ─────────────────────────────────────────────
# Chat
# ─────────────────────────────────────────────

def _start_chat() -> None:
    print("\nStarting Agent Sam chat shell. Type /exit to leave.\n")
    cmd = [sys.executable, "-m", "gateways.cli.main"]
    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    if result.returncode not in (0, 130):
        print(f"Chat exited with code {result.returncode}")


# ─────────────────────────────────────────────
# Setup
# ─────────────────────────────────────────────

def _run_setup() -> None:
    print("\nRunning gateway and secrets setup...\n")
    result = subprocess.run(
        [sys.executable, "-m", "scripts.setup"],
        cwd=str(PROJECT_ROOT),
    )
    if result.returncode != 0:
        print(f"Setup exited with code {result.returncode}")


# ─────────────────────────────────────────────
# Service management
# ─────────────────────────────────────────────

def _start_services(target: str) -> None:
    is_linux = sys.platform.startswith("linux")
    if is_linux and _systemctl_available():
        _systemctl_start(target)
    else:
        _start_local(target)


def _systemctl_available() -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "--version"],
            capture_output=True,
            timeout=3,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _systemctl_start(target: str) -> None:
    service_map = {
        "api": ("agent-api",),
        "worker": ("agent-worker",),
        "telegram": ("agent-telegram",),
        "gateways": ("agent-telegram",),
        "all": SYSTEMD_SERVICES,
        "": SYSTEMD_SERVICES,
    }
    services = service_map.get(target, SYSTEMD_SERVICES)
    print(f"Starting {' '.join(services)} via systemd...")
    subprocess.run(["sudo", "systemctl", "start", *services])
    subprocess.run(["sudo", "systemctl", "status", *services, "--no-pager"])


def _start_local(target: str) -> None:
    cmds = {
        "api": [sys.executable, "-m", "app.main"],
        "worker": [sys.executable, "-m", "workers.main"],
        "telegram": [sys.executable, "-m", "gateways.telegram.main"],
        "gateways": [sys.executable, "-m", "gateways.runner"],
    }
    if target in {"all", ""}:
        for name, cmd in cmds.items():
            print(f"  Run in separate terminal: {format_command(cmd)}")
        return
    cmd = cmds.get(target)
    if cmd is None:
        print(f"Unknown service: {target}")
        return
    print(f"Running {format_command(cmd)} (Ctrl+C to stop)...")
    try:
        subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    except KeyboardInterrupt:
        pass


def _stop_services() -> None:
    if sys.platform.startswith("linux") and _systemctl_available():
        print("Stopping all services via systemd...")
        subprocess.run(["sudo", "systemctl", "stop", *SYSTEMD_SERVICES])
    else:
        print("Systemd not available. Kill the processes manually.")


def _show_status() -> None:
    if sys.platform.startswith("linux") and _systemctl_available():
        subprocess.run(["sudo", "systemctl", "status", *SYSTEMD_SERVICES, "--no-pager"])
    else:
        print("Systemd not available on this platform.")


# ─────────────────────────────────────────────
# Doctor
# ─────────────────────────────────────────────

def _run_doctor() -> None:
    print("\nRunning diagnostics...\n")
    subprocess.run([sys.executable, "-m", "scripts.doctor"], cwd=str(PROJECT_ROOT))


# ─────────────────────────────────────────────
# Queue
# ─────────────────────────────────────────────

async def _queue_show() -> None:
    from db.session import AsyncSessionLocal
    from db.task_queue import get_queue_status, list_task_queue

    async with AsyncSessionLocal() as session:
        status = await get_queue_status(session)
        tasks = await list_task_queue(
            session,
            statuses=("pending", "running", "paused"),
            limit=20,
        )

    total = status.get("pending", 0) + status.get("running", 0) + status.get("paused", 0)
    if total == 0:
        print("Queue is empty.")
        return

    print(f"\n{total} active tasks:")
    status_emoji = {"pending": "⏳", "running": "⚙️", "paused": "⏸"}
    for task in tasks:
        emoji = status_emoji.get(task.status, "•")
        print(f"  {emoji} [{task.status}] {task.title[:60]}  ID: {task.id}")
    print()


async def _queue_clear() -> None:
    from db.session import AsyncSessionLocal
    from db.task_queue import cancel_task, list_task_queue

    async with AsyncSessionLocal() as session:
        tasks = await list_task_queue(
            session,
            statuses=("pending", "running", "paused"),
            limit=500,
        )
        count = 0
        for task in tasks:
            await cancel_task(session, task_id=task.id)
            count += 1
    print(f"Cancelled {count} task(s). Queue is now empty.")


# ─────────────────────────────────────────────
# Agents & models
# ─────────────────────────────────────────────

def _show_agents() -> None:
    from agents.registry import list_agents

    print("\nSpecialist Agents:")
    print(f"  {'AGENT':<30} {'MODEL':<50} {'RISK'}")
    print("  " + "-" * 90)
    for p in list_agents():
        if p.slug == "router_agent":
            continue
        print(f"  {p.slug:<30} {p.default_model:<50} {p.risk_level}")
    print()


def _show_models() -> None:
    from agents.registry import list_agents
    from app.config import get_settings
    from db.session import AsyncSessionLocal
    from services.model_router import ModelRouter

    router = ModelRouter(get_settings(), AsyncSessionLocal)
    print("\nConfigured models:")
    for m in router.list_configured_models(list_agents()):
        print(f"  - {m}")
    print()


# ─────────────────────────────────────────────
# Logs
# ─────────────────────────────────────────────

def _tail_logs(service: str) -> None:
    service_map = {
        "api": "agent-api",
        "worker": "agent-worker",
        "telegram": "agent-telegram",
        "gateways": "agent-telegram",
    }
    unit = service_map.get(service, service)
    if sys.platform.startswith("linux") and _systemctl_available():
        print(f"Tailing logs for {unit} (Ctrl+C to stop)...\n")
        try:
            subprocess.run(["sudo", "journalctl", "-u", unit, "-f", "--no-pager"])
        except KeyboardInterrupt:
            pass
    else:
        print(f"Systemd not available. Service: {unit}")


if __name__ == "__main__":
    run()
