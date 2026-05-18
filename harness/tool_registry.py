from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from pydantic import BaseModel


ToolHandler = Callable[[Any, Any], Awaitable[Any]]


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: type[BaseModel] | None
    output_schema: type[BaseModel] | None
    handler: ToolHandler | None = None
    risk_level: str = "safe"
    allowed_agents: tuple[str, ...] = ()
    requires_approval: bool = False
    workspace_required: bool = False
    network_required: bool = False
    secrets_allowed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register_tool(self, tool: ToolSpec) -> ToolSpec:
        self._tools[tool.name] = tool
        return tool

    def unregister_tool(self, name: str) -> None:
        self._tools.pop(name, None)

    def get_tool(self, name: str) -> ToolSpec:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"Tool not registered: {name}")
        return tool

    def list_tools(self) -> tuple[ToolSpec, ...]:
        return tuple(self._tools.values())

    def validate_tool_input(self, name: str, payload: dict[str, Any]) -> BaseModel | dict[str, Any]:
        tool = self.get_tool(name)
        if tool.input_schema is None:
            return payload
        return tool.input_schema.model_validate(payload)

    def tools_for_agent(self, agent_slug: str) -> tuple[ToolSpec, ...]:
        return tuple(
            tool
            for tool in self._tools.values()
            if not tool.allowed_agents or agent_slug in tool.allowed_agents
        )

    def tools_for_task(self, task_id) -> tuple[ToolSpec, ...]:
        del task_id
        return self.list_tools()


_default_registry = ToolRegistry()


def register_tool(tool: ToolSpec) -> ToolSpec:
    return _default_registry.register_tool(tool)


def unregister_tool(name: str) -> None:
    _default_registry.unregister_tool(name)


def get_tool(name: str) -> ToolSpec:
    return _default_registry.get_tool(name)


def list_tools() -> tuple[ToolSpec, ...]:
    return _default_registry.list_tools()


def validate_tool_input(name: str, payload: dict[str, Any]) -> BaseModel | dict[str, Any]:
    return _default_registry.validate_tool_input(name, payload)


def tools_for_agent(agent_slug: str) -> tuple[ToolSpec, ...]:
    return _default_registry.tools_for_agent(agent_slug)


def tools_for_task(task_id) -> tuple[ToolSpec, ...]:
    return _default_registry.tools_for_task(task_id)