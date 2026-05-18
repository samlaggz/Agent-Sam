# Tool System

The harness tool system is built from three layers:

1. `harness/actions.py` defines typed actions the model can request.
2. `harness/tool_registry.py` registers tool metadata and handlers.
3. `harness/tool_executor.py` validates, checks policy, requests approval, executes, and logs.

## Built-in tools

- safe shell
- browser wrapper
- file read
- file write
- file patch
- workspace search
- install library
- install repo
- git
- GitHub
- source cache
- approval resolution

## Logging

Every execution path logs:

- a `tool_call` event
- a `tool_calls` DB row
- a `tool_result` event or an `error` event

Shell commands also continue to use the existing safe shell tool and approval model.