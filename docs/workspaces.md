# Workspaces

Each harness task gets an isolated workspace under `AGENT_WORKSPACES_DIR`.

## Layout

```text
workspaces/
  task_<task_id>/
    repo/
    downloads/
    screenshots/
    source_cache/
    logs/
    patches/
    events.jsonl
```

## Persistence

Workspace records are stored in `workspaces_runtime` with the task id, path, status, timestamps, and metadata.

## Operations

- create workspace
- seed repo contents for coding and testing tasks
- snapshot and restore
- cleanup
- enforce path boundaries

Use `/workspace <task_id>` to inspect the current workspace path for a task.