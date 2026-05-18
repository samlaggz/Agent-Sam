# Specialist Agents

Agent_Sam routes each task to a specialist profile before execution.

Current built-in specialists:

- `coding_agent`
- `testing_agent`
- `research_agent`
- `planning_agent`
- `server_ops_agent`
- `graphics_agent`
- `data_agent`
- `qa_reviewer_agent`

Each profile in `agents/configs/` defines task types, model defaults, allowed tools, cost limits, risk level, memory access, and evaluation rules.

Generated agents are written to:

- `agents/configs/generated/`
- `agents/prompts/generated/`

Execution records are stored in:

- `agent_runs`
- `agent_run_steps`
- `agent_evaluations`
- `learning_events`

When the harness is enabled, specialist execution can also emit:

- `agent_events`
- `workspaces_runtime`

The harness does not replace the specialist router. Routing still happens first, then selected task types drop into the harness loop for action planning, tool execution, approvals, and trace export.