# Events

Harness runs emit append-only rows into `agent_events`.

## Event types

- `user_message`
- `agent_thought`
- `tool_call`
- `tool_result`
- `file_edit`
- `shell_command`
- `browser_action`
- `approval_requested`
- `approval_resolved`
- `error`
- `run_summary`

## Guarantees

- events are sequence-ordered per task or run
- event export is JSONL-friendly
- content and metadata are redacted before persistence when they contain obvious secret material
- event history can be replayed after process restart

Use `/events <task_id>` in the CLI or Telegram gateway to inspect the most recent recorded events.