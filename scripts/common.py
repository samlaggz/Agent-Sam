from __future__ import annotations

import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"
ENV_EXAMPLE_PATH = PROJECT_ROOT / ".env.example"
LOCAL_DEV_FIX_COMMAND = (
    "docker compose up -d postgres redis qdrant && alembic upgrade head && python scripts/seed_dev.py"
)

DEFAULT_NEXT_COMMANDS = (
    "python -m scripts.doctor",
    "python -m scripts.run",
    "python -m gateways.runner",
    "python -m gateways.cli.main",
    "python -m gateways.telegram.main",
)


def detect_os_name() -> str:
    mapping = {
        "Windows": "Windows",
        "Linux": "Linux",
        "Darwin": "macOS",
    }
    return mapping.get(platform.system(), platform.system() or "Unknown")


def python_version_text() -> str:
    return sys.version.split()[0]


def format_command(command: Sequence[str]) -> str:
    if sys.platform.startswith("win"):
        return subprocess.list2cmdline(list(command))
    return " ".join(command)


def run_subprocess(
    command: Sequence[str],
    *,
    cwd: Path = PROJECT_ROOT,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=str(cwd),
        text=True,
        capture_output=capture_output,
        check=False,
    )


def parse_seed_output(output: str) -> tuple[str | None, str | None]:
    user_match = re.search(r"DEFAULT_USER_ID=([0-9a-fA-F-]{36})", output)
    workspace_match = re.search(r"DEFAULT_WORKSPACE_ID=([0-9a-fA-F-]{36})", output)
    user_id = user_match.group(1) if user_match else None
    workspace_id = workspace_match.group(1) if workspace_match else None
    return user_id, workspace_id


def print_next_commands(output, commands: Sequence[str] = DEFAULT_NEXT_COMMANDS) -> None:
    output("Next commands:")
    for command in commands:
        output(command)