# Setup Guide

## Windows local development

Recommended flow:

```powershell
cd F:\personal\Agent_Sam
python -m pip install -e ".[dev]"
python -m scripts.setup
python -m scripts.doctor
python -m scripts.run
```

`python -m scripts.run` opens the Agent_Sam Control Center so you can launch runtime processes and inspect or approve specialist-agent state from one menu.

The setup wizard:

- detects the current OS, Python version, and project root
- creates `.env` from `.env.example` if needed
- exposes a Hermes-style menu for local setup, production setup, gateway config, LLM config, migrations, seeding, and doctor runs
- runs migrations and development seeding for local mode
- writes `DEFAULT_USER_ID` and `DEFAULT_WORKSPACE_ID` into `.env`
- supports `--dry-run` previews for safe validation
- writes provider-specific LLM settings and hides secrets while prompting
- configures OpenRouter or alternate LiteLLM providers
- writes per-agent model defaults and escalation models
- writes specialist budget and self-evolution toggles

## Local development mode

The local development flow offers to run:

```powershell
docker compose up -d postgres redis qdrant
python -m pip install -e ".[dev]"
python -m alembic upgrade head
python scripts/seed_dev.py
```

After seeding, the wizard updates `.env` automatically and prints the next commands.

The wizard now also asks for:

- `OPENROUTER_API_KEY` when OpenRouter-backed models are selected
- `DEFAULT_MODEL` and the per-agent model keys
- `MAX_COST_PER_TASK_USD` and `DAILY_MODEL_BUDGET_USD`
- `ALLOW_MODEL_ESCALATION`
- `ALLOW_SKILL_AUTO_PROPOSAL`
- `ALLOW_SKILL_AUTO_ACTIVATION`
- `ALLOW_SUB_AGENT_PROPOSAL`
- `ALLOW_SUB_AGENT_AUTO_CREATION`
- `ENABLE_WEB_RESEARCH`

## CLI-only mode

CLI-only mode enables the `cli` gateway and checks for seeded IDs. If the database is not ready, it prints the exact recovery command:

```powershell
docker compose up -d postgres redis qdrant && alembic upgrade head && python scripts/seed_dev.py
```

## Linux production mode

The setup wizard now prepares the production env values and prints the exact one-command installer:

```bash
python -m scripts.setup --production
bash <(curl -fsSL https://raw.githubusercontent.com/samlaggz/Agent-Sam/main/install.sh)
```

For a private GitHub repo, use:

```bash
export AGENT_SAM_GITHUB_TOKEN=<github_pat_with_repo_read>
bash <(curl -fsSL -H "Authorization: Bearer ${AGENT_SAM_GITHUB_TOKEN}" -H "Accept: application/vnd.github.raw" https://api.github.com/repos/samlaggz/Agent-Sam/contents/install.sh?ref=main)
```

If the repo is already checked out on the host, the fallback remains:

```bash
bash install.sh
```

It also keeps the manual fallback commands visible:

```bash
sudo bash deployment/linux/install-host-assets.sh /opt/agent-sam
sudo -iu agentos
cd /opt/agent-sam
bash deployment/linux/bootstrap-app.sh
sudo systemctl start agent-api agent-worker agent-telegram
```

Use `python -m scripts.setup --dry-run` to preview the menu without modifying `.env` or executing commands.