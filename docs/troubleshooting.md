# Troubleshooting

## Specialist runtime

- `/models` looks wrong: run `python -m scripts.models validate` and check the model strings in `.env`.
- OpenRouter tasks fail immediately: set `OPENROUTER_API_KEY` and confirm `OPENROUTER_BASE_URL`.
- A task routes to the wrong specialist: inspect `/route <task_id>` and adjust the task wording or the profile `task_types`.
- Budget downgrades are too aggressive: raise `MAX_COST_PER_TASK_USD` or `DAILY_MODEL_BUDGET_USD`.
- Generated sub-agent creation fails: check whether the proposal includes admin-only tools such as `safe_shell` without explicit approval metadata.

## Worker looks stuck

The worker is polling the task queue. It now logs:

- `Worker ready`
- `Polling task queue every X seconds`
- `No pending tasks, sleeping...`
- `Press Ctrl+C to stop`

## CLI exits after the first message

The CLI gateway should now stay alive after normal text and command responses. If it exits early, run:

```powershell
python -m scripts.doctor
```

If the database is not ready, recover with:

```powershell
docker compose up -d postgres redis qdrant
alembic upgrade head
python scripts/seed_dev.py
```

## Telegram token appears in logs

Rotate the token immediately in BotFather.

The runtime now forces `httpx`, `httpcore`, `telegram`, and `telegram.ext` to `WARNING` unless gateway debug logging is enabled, and it redacts Telegram bot URLs from logs.

## Postgres connection refused

Run:

```powershell
docker compose up -d postgres redis qdrant
```

Then run migrations and seed data:

```powershell
alembic upgrade head
python scripts/seed_dev.py
```

## DEFAULT_WORKSPACE_ID missing

Run:

```powershell
python scripts/seed_dev.py
```

On Linux production hosts, `bash deployment/linux/bootstrap-app.sh` now auto-seeds `DEFAULT_USER_ID` and `DEFAULT_WORKSPACE_ID` when they are missing.

## One-command production preview

Use these commands when you want a safe preview or an automatic repair pass:

```bash
bash install.sh --dry-run
python -m scripts.setup --dry-run
python -m scripts.doctor --production --fix
```

## Production service status and logs

Use the shorthand helper commands:

```bash
bash deployment/linux/service-control.sh status
bash deployment/linux/service-control.sh logs
bash deployment/linux/service-control.sh restart api
```

## Alembic ProactorEventLoop error on Windows

Use the existing entrypoints that already configure the selector loop policy, such as:

```powershell
python -m scripts.setup
python -m scripts.doctor
python -m workers.main
python -m gateways.cli.main
python -m gateways.telegram.main
```

## Telegram CancelledError on Ctrl+C

Expected shutdown `CancelledError` exceptions are now suppressed during Telegram gateway shutdown. If you still see a token in logs while investigating, rotate it immediately.