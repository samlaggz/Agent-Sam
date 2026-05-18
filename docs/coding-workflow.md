# Coding Workflow

The harness coding workflow keeps the existing specialist router and adds a workspace-backed execution loop for implementation tasks.

## Typical flow

1. `/code <task>` creates a coding task and requests the harness path.
2. The runtime creates or seeds a task workspace.
3. The model plans one action at a time.
4. File edits go through the patch-based editor.
5. `/test <task_id>` creates a testing follow-up task.
6. `/pr <task_id>` creates a PR-preparation follow-up task.
7. `/events <task_id>` exports the latest execution trace.
8. `/workspace <task_id>` reveals the isolated workspace path.

## Why this stays maintainable

- LangGraph routing remains the top-level coordinator
- harness logic is confined to the `harness/` package
- new tools can be registered without rewriting the loop
- execution traces are persisted independently of chat history