# Gateway System

Agent Sam uses a transport-only gateway layer. Telegram, CLI, webhook, and future chat transports all adapt external messages into the shared `AgentGatewayService`, which owns task creation, queue inspection, approvals, and message persistence.

## Implemented gateways

- `telegram`: implemented polling adapter using `python-telegram-bot`
- `cli`: implemented local terminal adapter with the same command surface as Telegram
- `webhook`: authenticated inbound route served by the API process
- `whatsapp`: placeholder with config validation for Twilio or Meta Cloud API
- `discord`: placeholder with config validation for a future `discord.py` adapter
- `slack`: placeholder with config validation for a future Slack Bolt adapter

## Required environment

These values belong in `.env`:

```dotenv
ENABLED_GATEWAYS=telegram,cli
DEFAULT_WORKSPACE_ID=
DEFAULT_USER_ID=
TELEGRAM_BOT_TOKEN=
WEBHOOK_GATEWAY_SECRET=
```

`DEFAULT_WORKSPACE_ID` and `DEFAULT_USER_ID` must reference real rows. Generate them with:

```powershell
alembic upgrade head
python scripts/seed_dev.py
```

## Setup wizard

Use the interactive wizard to update `.env` without overwriting unrelated settings:

```powershell
python -m gateways.setup
python -m gateways.setup --show-enabled
python -m gateways.setup --test-config
python -m gateways.setup --disable telegram
```

The wizard:

- shows the available gateways
- asks which gateways to enable
- collects the required tokens, secrets, and provider-specific values
- updates `ENABLED_GATEWAYS`
- preserves existing `.env` values that you did not change
- prints the next gateway start commands without echoing secret values

If a Telegram token ever appears in logs, rotate it immediately in BotFather.

## Start commands

Start all enabled gateways from `.env`:

```powershell
agent-sam-gateways
```

Start a single gateway directly:

```powershell
agent-sam-cli
agent-sam-telegram
python -m gateways.runner
python -m gateways.cli.main
python -m gateways.telegram.main
```

The CLI gateway accepts the same commands as Telegram and exits with `/exit`.

## Supported commands

All implemented gateways share the same command surface:

```text
/start
/help
/new <task description>
/status <task_id>
/queue
/prioritize <task_id> <low|normal|high|urgent>
/pause <task_id>
/resume <task_id>
/approve <approval_id>
/cancel <task_id>
```

Normal conversation gets an inline reply through the shared service. Explicit work requests still create tracked tasks, and `/new` always creates a task.

## Webhook contract

When `webhook` is enabled, the API exposes `POST /gateways/webhook`.

Authentication:

- Header: `X-Gateway-Secret: <WEBHOOK_GATEWAY_SECRET>`

Payload:

```json
{
  "gateway_user_id": "external-user-123",
  "gateway_chat_id": "conversation-456",
  "text": "Create a follow-up task for this conversation"
}
```

Response:

```json
{
  "text": "Task created.\nID: ...",
  "task_id": "...",
  "approval_id": null,
  "should_reply": true
}
```

## Placeholder status

WhatsApp, Discord, and Slack currently validate configuration and then stop with a clear `not implemented` error if you try to start them. That is intentional: the config surface is in place, but those transports do not fake working behavior.