# Doctor Guide

Run diagnostics with:

```powershell
python -m scripts.doctor
```

Production checks and safe automatic fixes:

```bash
python -m scripts.doctor --production
python -m scripts.doctor --production --fix
```

The doctor checks:

- Python version against `requires-python`
- editable install or importability
- `.env` presence
- duplicate `.env` keys
- required environment values
- LLM model and provider credential completeness
- OpenRouter credential presence when OpenRouter models are configured
- per-agent model syntax validation
- budget and toggle validation for specialist routing
- Telegram token presence when Telegram is enabled
- Docker availability
- Postgres, Redis, and Qdrant reachability
- Alembic current revision versus head
- async SQLAlchemy session health
- users/workspaces queries
- a rollback-only dry-run task insert
- gateway registry construction
- worker, LangGraph, and LiteLLM imports
- repository skills loading
- specialist agent profile loading
- generated specialist-agent directories
- memory service query path
- shell risk rules loading
- specialist schema tables

When `HARNESS_ENABLED=true`, doctor also checks:

- harness module imports
- workspace directory existence and writability
- source cache directory existence and writability
- `opensrc` availability when source cache is enabled
- `rg` availability when source cache is enabled
- Playwright imports when browser automation is enabled
- GitHub token presence when automatic PR creation is enabled
- runtime policy validity
- non-root execution in production

Production mode also checks:

- application directory presence and ownership
- systemd availability
- systemd service files
- service enabled and running state
- nginx config presence when nginx is installed
- API health at `http://127.0.0.1:8000/health`

Output uses these markers:

- `[OK]` working as expected
- `[FAIL]` something is broken or missing
- `[FIX]` the next command to run
- `[FIXED]` a safe automatic fix was applied

Secrets are never printed.