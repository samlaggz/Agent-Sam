# Skills

Skill definitions live here as YAML files so the agent runtime can load, validate, and route them without code changes.

Each skill file must include exactly these fields:

- `name`
- `version`
- `description`
- `triggers`
- `inputs`
- `procedure`
- `tools_allowed`
- `risk_notes`
- `failure_modes`
- `evaluation_checklist`

Runtime behavior:

- New skill files are loaded and saved as active database records when the workspace does not already have that skill.
- A newer skill version on disk becomes a pending proposal instead of replacing the active version automatically.
- Proposed skills or updates must be approved before a new version is published.
- Published skill rows are immutable version history; older active versions become `superseded` when a newer approved version is applied.
