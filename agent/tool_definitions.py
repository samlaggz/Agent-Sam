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
                "Execute a shell command on the server. Use for: service management, "
                "package installs, process diagnostics, network checks, and other OS operations. "
                "If file_read, file_write, or grep are in your tool list, prefer them over shell "
                "for filesystem inspection and editing. "
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


def file_read_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "file_read",
            "description": (
                "Read a text file from the allowed workspace or server roots. "
                "Prefer this over shell cat for inspecting files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path or allowed-root-relative path to the file.",
                    },
                },
                "required": ["path"],
            },
        },
    }


def file_write_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "file_write",
            "description": (
                "Write text content to a file within the allowed workspace or server roots. "
                "Creates parent directories if needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path or allowed-root-relative path to the file.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The full text content to write.",
                    },
                    "append": {
                        "type": "boolean",
                        "description": "If true, append instead of replacing the file. Default false.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    }


def grep_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "grep",
            "description": (
                "Search for text in a file or recursively under a directory within the allowed roots. "
                "Returns matching paths, line numbers, and line text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Text or regex pattern to search for.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Optional file or directory path. Defaults to the task working directory.",
                    },
                    "is_regex": {
                        "type": "boolean",
                        "description": "Treat query as a regular expression. Default false.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of matches to return. Default 50.",
                    },
                },
                "required": ["query"],
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


def browser_navigate_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "browser_navigate",
            "description": (
                "Navigate to a URL in the browser. Opens the page and returns a snapshot "
                "of the accessibility tree with interactive element refs like @e1, @e5. "
                "Use this for pages that need interaction (login forms, clicking buttons, "
                "dynamic content). For simple page reading, prefer web_search or shell curl."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to navigate to (e.g., 'https://example.com').",
                    },
                },
                "required": ["url"],
            },
        },
    }


def browser_snapshot_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "browser_snapshot",
            "description": (
                "Get a text snapshot of the current page's accessibility tree. "
                "Shows interactive elements with ref IDs (like @e1, @e2) for clicking/typing. "
                "Call after browser_navigate or after any interaction that changes the page."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "full": {
                        "type": "boolean",
                        "description": "If true, returns complete page content. Default: compact interactive view.",
                    },
                },
                "required": [],
            },
        },
    }


def browser_click_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": (
                "Click on an element by its ref ID from the snapshot (e.g., '@e5'). "
                "The ref IDs are shown in square brackets in the snapshot output."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "string",
                        "description": "The element reference (e.g., '@e5', '@e12').",
                    },
                },
                "required": ["ref"],
            },
        },
    }


def browser_type_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "browser_type",
            "description": (
                "Type text into an input field by its ref ID. Clears the field first. "
                "Use browser_snapshot to find the right ref ID for the input field."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "string",
                        "description": "The input element reference (e.g., '@e3').",
                    },
                    "text": {
                        "type": "string",
                        "description": "The text to type into the field.",
                    },
                },
                "required": ["ref", "text"],
            },
        },
    }


def browser_press_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "browser_press",
            "description": (
                "Press a keyboard key. Useful for submitting forms (Enter), "
                "navigating (Tab), or dismissing dialogs (Escape)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Key to press: 'Enter', 'Tab', 'Escape', 'ArrowDown', 'ArrowUp'.",
                    },
                },
                "required": ["key"],
            },
        },
    }


def browser_scroll_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "browser_scroll",
            "description": "Scroll the page up or down to reveal more content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down"],
                        "description": "Direction to scroll.",
                    },
                },
                "required": ["direction"],
            },
        },
    }


def build_tool_definitions(
    *,
    shell_enabled: bool = True,
    web_enabled: bool = False,
    browser_enabled: bool = False,
    file_enabled: bool = False,
    grep_enabled: bool = False,
    memory_enabled: bool = True,
) -> list[dict[str, Any]]:
    """Build the tool list based on agent capabilities."""
    tools: list[dict[str, Any]] = []

    if shell_enabled:
        tools.append(shell_command_tool())
    if file_enabled:
        tools.append(file_read_tool())
        tools.append(file_write_tool())
    if grep_enabled:
        tools.append(grep_tool())
    if web_enabled:
        tools.append(web_search_tool())
        tools.append(web_open_tool())
    if browser_enabled:
        tools.append(browser_navigate_tool())
        tools.append(browser_snapshot_tool())
        tools.append(browser_click_tool())
        tools.append(browser_type_tool())
        tools.append(browser_press_tool())
        tools.append(browser_scroll_tool())
    if memory_enabled:
        tools.append(save_memory_tool())

    # Always include terminal tools
    tools.append(task_complete_tool())
    tools.append(task_failed_tool())

    return tools
