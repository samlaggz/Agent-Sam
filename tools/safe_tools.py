from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone


ToolResult = dict[str, str]


@dataclass(frozen=True)
class SafeTool:
    name: str
    description: str
    handler: Callable[[], ToolResult]


def utc_now_tool() -> ToolResult:
    return {"utc_now": datetime.now(timezone.utc).isoformat()}


def health_snapshot_tool() -> ToolResult:
    # TODO: expose richer diagnostics without leaking unsafe internals.
    return {"status": "skeleton"}


SAFE_TOOLS = [
    SafeTool(
        name="utc_now",
        description="Return the current UTC timestamp.",
        handler=utc_now_tool,
    ),
    SafeTool(
        name="health_snapshot",
        description="Return a minimal runtime health snapshot.",
        handler=health_snapshot_tool,
    ),
]
