from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from tools.safe_tools import SAFE_TOOLS, SafeTool
from tools.shell_command import ShellCommandTool
from tools.web_research import HttpWebResearchProvider, WebResearchProvider, WebResearchTool


def get_tool_registry() -> dict[str, SafeTool]:
    return {tool.name: tool for tool in SAFE_TOOLS}


def build_runtime_tool_registry(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    allowed_roots: Sequence[str | Path] | None = None,
    web_research_provider: WebResearchProvider | None = None,
) -> dict[str, Any]:
    registry: dict[str, Any] = get_tool_registry()
    registry["shell_command"] = ShellCommandTool(session_factory, allowed_roots=allowed_roots)
    if settings.enable_web_research or web_research_provider is not None:
        web_tool = WebResearchTool(session_factory, provider=web_research_provider or HttpWebResearchProvider())
        registry["web_search"] = web_tool
        registry["web_open"] = web_tool
    return registry
