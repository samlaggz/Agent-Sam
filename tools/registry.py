from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tools.safe_tools import SAFE_TOOLS, SafeTool
from tools.shell_command import ShellCommandTool


def get_tool_registry() -> dict[str, SafeTool]:
    return {tool.name: tool for tool in SAFE_TOOLS}


def build_runtime_tool_registry(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    allowed_roots: Sequence[str | Path] | None = None,
) -> dict[str, Any]:
    registry: dict[str, Any] = get_tool_registry()
    registry["shell_command"] = ShellCommandTool(session_factory, allowed_roots=allowed_roots)
    return registry
