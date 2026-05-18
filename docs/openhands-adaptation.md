# OpenHands Adaptation Map

## What Was Studied

- Public MIT-licensed OpenHands core repository layout and README-level architecture.
- Public OpenHands concepts around event-driven agent execution, SDK composition, runtime isolation, CLI/local GUI split, and MIT-vs-enterprise boundary.
- Public opensrc README and CLI usage for fetching and searching cached dependency source locally.

## License Notes

- OpenHands core is MIT-licensed.
- The `enterprise/` directory in OpenHands is not adopted and was not used as an implementation source.
- opensrc is Apache-2.0 licensed. Agent_Sam integrates with its CLI rather than vendoring its source.
- This harness adapts concepts, naming patterns, and execution boundaries. It does not copy OpenHands enterprise code.

## Patterns Adopted

- Append-only event history with typed event records.
- Action/observation split for model planning and tool execution.
- A dedicated harness loop under the existing top-level orchestrator instead of replacing it.
- Runtime and workspace isolation abstractions.
- Patch-oriented file editing and replayable trace export.
- Tool registry plus executor indirection so tools can be added without rewriting the loop.
- Model abstraction that stays behind the existing LiteLLM/OpenRouter router.
- External source cache integration through CLI boundaries instead of vendoring dependency source.

## Patterns Not Adopted

- OpenHands GUI and frontend stack.
- Enterprise-only integrations and multi-tenant cloud surfaces.
- Kubernetes or container orchestration as a required runtime path.
- Any CAPTCHA-solving or stealth browser behavior.
- Blind code copying from reference projects.

## Concept Mapping

| OpenHands Concept | Agent_Sam Harness Implementation |
| --- | --- |
| action / event loop | `harness/loop.py` + `harness/events.py` |
| observations | `harness/observations.py` |
| tool actions | `harness/actions.py` + `harness/tool_registry.py` |
| shell command execution | `harness/runtime.py` wrapping `tools/shell_command.py` |
| browser action | `harness/tool_executor.py` wrapping `tools/browser_tool.py` |
| file editing | `harness/file_editor.py` |
| runtime / sandbox | `harness/runtime.py` |
| workspace isolation | `harness/workspace.py` |
| model / LLM abstraction | `harness/model_adapter.py` calling `services/model_router.py` |
| GitHub / PR integration | `harness/github.py` |
| memory / history | `db.models.AgentEvent`, `harness/events.py`, existing memory service |
| configuration | `app/config.py` harness env surface |
| safety / confirmation | `harness/policies.py` + existing approvals table |
| offline source reference | `harness/source_cache.py` + `scripts/source_cache.py` |

## File Mapping

- `harness/events.py`: typed event models and event store.
- `harness/actions.py`: model-plannable actions.
- `harness/observations.py`: tool execution results.
- `harness/runtime.py`: local runtime wrapper with workspace boundaries.
- `harness/workspace.py`: task workspace lifecycle.
- `harness/file_editor.py`: diff-based edits, backups, rollback.
- `harness/tool_registry.py`: tool metadata and registration.
- `harness/tool_executor.py`: policy checks, approvals, execution, logging.
- `harness/model_adapter.py`: bridge from harness loop to `ModelRouter`.
- `harness/loop.py`: iterative execution loop with pause/resume semantics.
- `harness/github.py`: git and GitHub automation wrapper.
- `harness/source_cache.py`: opensrc-backed offline source cache adapter.
- `db.models.AgentEvent`: append-only run/task event persistence.
- `db.models.WorkspaceRuntime`: runtime workspace persistence.

## Implementation Phases

1. Add harness package, event persistence, workspace records, and adaptation docs.
2. Add typed actions, observations, file editor, runtime, policy engine, and tool registry.
3. Add tool executor wrappers for shell, browser, files, git, GitHub, and source cache.
4. Add harness loop and feature-flagged integration under the existing specialist runtime.
5. Extend doctor, setup, CLI/gateway commands, and documentation.
6. Validate with focused harness tests, then full compile/test/doctor runs.