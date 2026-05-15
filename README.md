# Agent Sam

Skeleton for a private AI agent operating system built with FastAPI, LangGraph, LiteLLM, Postgres, Redis, Qdrant, and a Telegram gateway.

The repository is intentionally minimal. The runtime surfaces exist, but the actual agent orchestration, task processing, and tool execution are left as TODOs.

## Project structure

```text
app/                FastAPI application
agent/              LangGraph state, nodes, and graph assembly
gateways/           External entry points such as Telegram
workers/            Background task worker skeleton
db/                 SQLAlchemy models and session helpers
skills/             YAML skill definitions
tools/              Safe tool wrapper registry
.devcontainer/      VS Code Dev Container configuration
deployment/         Linux deployment scripts, systemd units, and nginx example
```

## Local setup on Windows

1. Copy `.env.example` to `.env`.
2. Start local infrastructure:

   ```powershell
   docker compose up -d
   ```

3. Create and activate a virtual environment:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

4. Install the project in editable mode:

   ```powershell
   pip install --upgrade pip
   pip install -e .[dev]
   ```

Seed development IDs before running agent flows that depend on `DEFAULT_WORKSPACE_ID` and `DEFAULT_USER_ID`:

```powershell
docker compose up -d postgres redis qdrant
alembic upgrade head
python scripts/seed_dev.py
```

Copy the printed `DEFAULT_WORKSPACE_ID` and `DEFAULT_USER_ID` values into `.env`.

5. Start the API:

   ```powershell
   uvicorn app.main:app --reload
   ```

6. Start the worker in a second terminal:

   ```powershell
   agent-sam-worker
   ```

7. Start the Telegram gateway after setting `TELEGRAM_BOT_TOKEN`, `DEFAULT_WORKSPACE_ID`, and `DEFAULT_USER_ID` to existing database IDs:

   ```powershell
   agent-sam-telegram
   ```

## Dev Containers

Open the folder in VS Code and choose Reopen in Container.

The devcontainer uses the root `docker-compose.yml` for Postgres, Redis, and Qdrant, then adds a dedicated `workspace` service for Python development. When the app runs inside the devcontainer, use service hostnames in `.env`:

- `postgres` instead of `localhost`
- `redis` instead of `localhost`
- `qdrant` instead of `localhost`

## Linux production without Docker

Use the deployment bundle in `deployment/linux/`.

Highlights:

1. Check out the repository at the target app path first, then run `deployment/linux/install-host-assets.sh` to create the dedicated `agentos` user if needed and install the systemd unit files.
2. `deployment/linux/bootstrap-app.sh` must be run as `agentos`; it creates `.venv`, installs dependencies, creates `.env` from `.env.example` if needed, and runs Alembic migrations.
3. `deployment/linux/service-control.sh` provides `start`, `stop`, `restart`, `status`, and `logs` commands for `agent-api.service`, `agent-worker.service`, and `agent-telegram.service`.
4. `deployment/nginx/agent-api.conf` is the reverse-proxy example.
5. `deployment/linux/SUDO_PERMISSIONS.md` documents the minimal elevated commands separately.

See `deployment/linux/README.md` for the full flow.

## Next build steps

- Flesh out the LangGraph workflow in `agent/`.
- Replace the placeholder worker loop with a real queue consumer.
- Add database migrations and richer domain models.
- Implement Telegram message routing and auth controls.
