"""
OpenAI-format tool definitions for native tool calling.

Instead of asking the model to produce a JSON plan, we give it real tool
definitions and let it decide what to call on each turn — Hermes-style.
"""
from __future__ import annotations

from typing import Any


def shell_command_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "shell_command",
            "description": (
                "Execute a shell command on the server. Use for: file operations, "
                "service management, package installs, config changes, diagnostics. "
                "Always use absolute paths. For creating files, use tee with heredoc:\n"
                "tee /path/to/file > /dev/null << 'EOF'\ncontent\nEOF\n"
                "Before symlinking nginx configs, remove broken symlinks first:\n"
                "find /etc/nginx/sites-enabled/ -xtype l -delete"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute. Must be a valid, complete command.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Brief explanation of why this command is needed.",
                    },
                    "working_directory": {
                        "type": "string",
                        "description": "Working directory. Defaults to /opt/agent-sam if omitted.",
                    },
                },
                "required": ["command", "reason"],
            },
        },
    }


def web_search_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web using DuckDuckGo. Returns titles, URLs, and snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query.",
                    },
                },
                "required": ["query"],
            },
        },
    }


def web_open_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "web_open",
            "description": "Open a URL and read its content. Returns the text content of the page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to open (must start with http:// or https://).",
                    },
                },
                "required": ["url"],
            },
        },
    }


def save_memory_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": (
                "Save an important fact, decision, or finding to long-term memory. "
                "Use for: server configs discovered, paths found, decisions made, "
                "problems solved, warnings about what NOT to do."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The fact or finding to remember.",
                    },
                    "memory_type": {
                        "type": "string",
                        "enum": [
                            "server_fact",
                            "project_fact",
                            "decision",
                            "warning",
                            "task_summary",
                        ],
                        "description": "Category of the memory.",
                    },
                },
                "required": ["content", "memory_type"],
            },
        },
    }


def task_complete_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "task_complete",
            "description": (
                "Mark the task as complete with a final summary. "
                "Call this ONLY when the task objective is fully achieved and verified."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Human-readable summary of what was done and the result.",
                    },
                },
                "required": ["summary"],
            },
        },
    }


def task_failed_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "task_failed",
            "description": (
                "Mark the task as failed with an explanation. "
                "Only call this if the task truly cannot be completed after trying."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why the task failed and what was tried.",
                    },
                },
                "required": ["reason"],
            },
        },
    }


def build_tool_definitions(
    *,
    shell_enabled: bool = True,
    web_enabled: bool = False,
    memory_enabled: bool = True,
) -> list[dict[str, Any]]:
    """Build the tool list based on agent capabilities."""
    tools: list[dict[str, Any]] = []

    if shell_enabled:
        tools.append(shell_command_tool())
    if web_enabled:
        tools.append(web_search_tool())
        tools.append(web_open_tool())
    if memory_enabled:
        tools.append(save_memory_tool())

    # Always include terminal tools
    tools.append(task_complete_tool())
    tools.append(task_failed_tool())

    return tools
