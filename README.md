# Agent Sam

Private AI agent operating system built with FastAPI, LangGraph, LiteLLM, OpenRouter model routing, Postgres, Redis, Qdrant, and modular chat gateways.

Agent_Sam now includes a specialist multi-agent runtime with profile-based routing, per-agent model escalation, budget controls, learning events, and approval-gated skill or sub-agent proposals.

## Quick Start Windows Local

1. `cd F:\personal\Agent_Sam`
2. `python -m pip install -e ".[dev]"`
3. `python -m scripts.setup`
4. `python -m scripts.doctor`
5. `python -m scripts.run`

`python -m scripts.run` now opens the Agent_Sam Control Center with:

1. Setup / Reconfigure
2. Doctor
3. Start API
4. Start Worker
5. Start Gateways
6. Start CLI Gateway
7. Show Agents
8. Show Models
9. Show Budgets
10. Show Pending Skill Proposals
11. Show Pending Sub-Agent Proposals
12. Approve Skill Proposal
13. Approve Sub-Agent Proposal
14. Exit

## Project structure

```text
app/                FastAPI application
agent/              LangGraph state, nodes, and graph assembly
gateways/           External entry points such as Telegram, CLI, and webhooks
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

Configure gateways with the gateway-only wizard before starting them:

```powershell
python -m gateways.setup
```

Or use the full Hermes-style setup wizard:

```powershell
python -m scripts.setup
```

5. Start the API:

   ```powershell
   uvicorn app.main:app --reload
   ```

6. Start the worker in a second terminal:

   ```powershell
   agent-sam-worker
   ```

7. Start one or more gateways after setting `DEFAULT_WORKSPACE_ID` and `DEFAULT_USER_ID` to existing database IDs. The shared runner reads `ENABLED_GATEWAYS` from `.env`:

   ```powershell
   agent-sam-gateways
   ```

   Direct gateway entrypoints still work when you want to run a single transport:

   ```powershell
   agent-sam-cli
   agent-sam-telegram
   ```

   The built-in CLI gateway exits with `/exit`. The webhook gateway is served by the API process when `webhook` is included in `ENABLED_GATEWAYS`.

See `docs/gateways.md` for gateway architecture, the setup wizard, env vars, and the webhook payload contract.

## Manual Start

```powershell
docker compose up -d postgres redis qdrant
alembic upgrade head
python scripts/seed_dev.py
python -m workers.main
python -m gateways.cli.main
python -m gateways.telegram.main
python -m gateways.runner
```

## Gateway Setup

```powershell
python -m gateways.setup
python -m gateways.setup --show-enabled
python -m gateways.setup --test-config
python -m gateways.setup --disable telegram
```

Common runtime commands from the CLI or Telegram gateway:

Normal conversation gets an inline reply. Use `/new <task description>` or an explicit work request when you want a tracked task.

If `ENABLE_WEB_RESEARCH=true`, requests such as checking the internet, finding the latest docs, or browsing public sources are routed as tracked research work instead of answered with a shallow inline guess.

- `/agents`
- `/agent <slug>`
- `/route <task_id>`
- `/models`
- `/budget`
- `/skills pending`
- `/skills approve <proposal_id>`
- `/subagents pending`
- `/subagents approve <proposal_id>`

## Troubleshooting

- Worker looks stuck: it is polling the task queue; the worker now logs when it is ready and when there are no pending tasks.
- CLI exits after first message: this should now be fixed; run `python -m scripts.doctor` and verify Postgres plus seeded IDs.
- Telegram token appears in logs: rotate the token in BotFather immediately.
- Postgres connection refused: run `docker compose up -d postgres`.
- DEFAULT_WORKSPACE_ID missing: run `python scripts/seed_dev.py`.
- Alembic ProactorEventLoop error on Windows: use the `WindowsSelectorEventLoopPolicy` helper paths already wired into entrypoints.
- Telegram CancelledError on Ctrl+C: expected shutdown is now suppressed; if you still see a token in logs, rotate it.

See `docs/setup.md`, `docs/doctor.md`, `docs/gateways.md`, and `docs/troubleshooting.md` for the full walkthroughs.

Specialist-agent docs:

- `docs/agents.md`
- `docs/model-routing.md`
- `docs/openrouter.md`
- `docs/budgets.md`
- `docs/self-evolution.md`

## Dev Containers

Open the folder in VS Code and choose Reopen in Container.

The devcontainer uses the root `docker-compose.yml` for Postgres, Redis, and Qdrant, then adds a dedicated `workspace` service for Python development. When the app runs inside the devcontainer, use service hostnames in `.env`:

- `postgres` instead of `localhost`
- `redis` instead of `localhost`
- `qdrant` instead of `localhost`

## Linux production without Docker

Use the deployment bundle in `deployment/linux/`.

Recommended server sequence:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/samlaggz/Agent-Sam/main/install.sh)
```

If you already have a checkout on the server, `bash install.sh` still works from the repo root.

For a private GitHub repo, export a read-capable token first and use the GitHub contents API:

```bash
export AGENT_SAM_GITHUB_TOKEN=<github_pat_with_repo_read>
bash <(curl -fsSL -H "Authorization: Bearer ${AGENT_SAM_GITHUB_TOKEN}" -H "Accept: application/vnd.github.raw" https://api.github.com/repos/samlaggz/Agent-Sam/contents/install.sh?ref=main)
```

Use the pipe form only for fully non-interactive installs where all required env vars are already exported.

The installer is idempotent, writes `.env` without printing secrets, bootstraps the app as `agentos`, runs migrations, auto-seeds `DEFAULT_USER_ID` and `DEFAULT_WORKSPACE_ID` when needed, runs the production doctor, and can optionally install nginx plus start services.

Preview the full flow safely with:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/samlaggz/Agent-Sam/main/install.sh) --dry-run
bash install.sh --dry-run
python -m scripts.setup --dry-run
python -m scripts.doctor --production --fix
```

Manual fallback sequence:

```bash
bash deployment/linux/install-host-assets.sh /opt/agent-sam
sudo rsync -a --delete --exclude '.git' --exclude '.venv' ./ /opt/agent-sam/
sudo chown -R agentos:agentos /opt/agent-sam
sudo -iu agentos
cd /opt/agent-sam
bash deployment/linux/bootstrap-app.sh
# edit /opt/agent-sam/.env
bash deployment/linux/bootstrap-app.sh
python -m scripts.doctor --production
exit
sudo systemctl daemon-reload
sudo systemctl enable agent-api agent-worker agent-telegram
sudo systemctl start agent-api agent-worker agent-telegram
sudo systemctl status agent-api agent-worker agent-telegram
sudo journalctl -u agent-api -f
sudo journalctl -u agent-worker -f
sudo journalctl -u agent-telegram -f
```

The systemd units run as `agentos` from `/opt/agent-sam`, the API binds to `127.0.0.1:8000`, and `deployment/nginx/agent-api.conf` is the reverse-proxy example. `deployment/linux/service-control.sh` now supports the shorthand forms below if you prefer not to type the raw `systemctl` commands:

```bash
bash deployment/linux/service-control.sh status
bash deployment/linux/service-control.sh logs
bash deployment/linux/service-control.sh restart api
```

When installing the nginx config after the app has been synced to `/opt/agent-sam`, use the absolute path:

```bash
sudo install -m 0644 /opt/agent-sam/deployment/nginx/agent-api.conf /etc/nginx/sites-available/agent-api.conf
```

See `deployment/linux/README.md` for the full flow.

## Next build steps

- Flesh out the LangGraph workflow in `agent/`.
- Replace the placeholder worker loop with a real queue consumer.
- Add database migrations and richer domain models.
- Flesh out the WhatsApp, Discord, Slack, and outbound webhook adapters.
