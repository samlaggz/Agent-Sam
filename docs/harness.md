# Harness

The Agent_Sam harness is an OpenHands-inspired execution layer that sits under the existing specialist router and LangGraph orchestration path.

## What it does

- creates a task-scoped workspace
- loads prior `agent_events`
- asks the model for the next typed action
- validates the action against runtime policy
- executes through the tool executor
- appends observations and summary events
- pauses on approval or failure thresholds

## Feature flag

Enable the harness with:

```env
HARNESS_ENABLED=true
AGENT_RUNTIME=local
AGENT_WORKSPACES_DIR=./workspaces
```

The legacy LangGraph path stays in place. When the harness flag is off, Agent_Sam behaves as it did before.

## Main modules

- `harness/loop.py`
- `harness/events.py`
- `harness/actions.py`
- `harness/observations.py`
- `harness/tool_executor.py`
- `harness/runtime.py`
- `harness/workspace.py`
- `harness/file_editor.py`
- `harness/model_adapter.py`

## Safety model

- shell commands stay inside the task workspace
- risky commands request approval
- browser automation stops on CAPTCHA-like flows
- file edits use diffs and backups
- the runtime refuses to execute as root on Linux