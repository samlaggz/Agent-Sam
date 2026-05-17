# Linux Deployment

This deployment path is for a Linux host without Docker. The application runs as the dedicated `agentos` user from `/opt/agent-sam`, while Postgres, Redis, and Qdrant are provided by external services or local host installs.

## Layout

- Application checkout: `/opt/agent-sam`
- Runtime env file: `/opt/agent-sam/.env`
- Virtual environment: `/opt/agent-sam/.venv`
- Systemd units: `/etc/systemd/system/agent-api.service`, `/etc/systemd/system/agent-worker.service`, `/etc/systemd/system/agent-telegram.service`

## Bootstrap flow

1. The preferred path is the Hermes-style one-line installer:

   ```bash
   bash <(curl -fsSL https://raw.githubusercontent.com/samlaggz/Agent-Sam/main/install.sh)
   ```

   For a private GitHub repo, export a read-capable token first and use:

   ```bash
   export AGENT_SAM_GITHUB_TOKEN=<github_pat_with_repo_read>
   bash <(curl -fsSL -H "Authorization: Bearer ${AGENT_SAM_GITHUB_TOKEN}" -H "Accept: application/vnd.github.raw" https://api.github.com/repos/samlaggz/Agent-Sam/contents/install.sh?ref=main)
   ```

   Useful flags:

   ```bash
   bash <(curl -fsSL https://raw.githubusercontent.com/samlaggz/Agent-Sam/main/install.sh) --dry-run
   bash <(curl -fsSL https://raw.githubusercontent.com/samlaggz/Agent-Sam/main/install.sh) --non-interactive --skip-nginx --skip-start
   bash <(curl -fsSL https://raw.githubusercontent.com/samlaggz/Agent-Sam/main/install.sh) --target /opt/agent-sam
   ```

   If you already have a checkout on the host, `bash install.sh` from the repo root still uses the same installer.

2. Manual fallback: from a sudo-capable operator account, create the dedicated user, `/opt/agent-sam`, the systemd units, and the nginx example if nginx is installed:

   ```bash
   bash deployment/linux/install-host-assets.sh /opt/agent-sam
   ```

3. Sync the application checkout into `/opt/agent-sam` and hand ownership to `agentos`:

   ```bash
   sudo rsync -a --delete --exclude '.git' --exclude '.venv' --exclude '.env' ./ /opt/agent-sam/
   sudo chown -R agentos:agentos /opt/agent-sam
   ```

4. Switch to the dedicated app user and bootstrap the application:

   ```bash
   sudo -iu agentos
   cd /opt/agent-sam
   bash deployment/linux/bootstrap-app.sh
   ```

5. If `.env` did not exist, the bootstrap script stops after copying `.env.example`. Edit `/opt/agent-sam/.env` with production secrets and service URLs, then rerun:

   ```bash
   python -m scripts.setup --production
   bash deployment/linux/bootstrap-app.sh
   ```

   `bootstrap-app.sh` now auto-seeds `DEFAULT_USER_ID` and `DEFAULT_WORKSPACE_ID` when they are missing and runs the production doctor at the end.

6. Run the production doctor manually when you need a separate verification pass:

   ```bash
   python -m scripts.doctor --production
   python -m scripts.doctor --production --fix
   ```

7. Return to the operator account, enable the services, and start them:

   ```bash
   exit
   sudo systemctl daemon-reload
   sudo systemctl enable agent-api agent-worker agent-telegram
   sudo systemctl start agent-api agent-worker agent-telegram
   ```

## Updating an existing deployment

`/opt/agent-sam` may be a synced application directory rather than a git checkout. If `git pull` fails with `not a git repository`, use one of these update paths instead.

Preferred update path:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/samlaggz/Agent-Sam/main/install.sh) --target /opt/agent-sam
```

Manual update path:

```bash
tmpdir="$(mktemp -d)"
curl -fsSL https://github.com/samlaggz/Agent-Sam/archive/refs/heads/main.tar.gz -o "${tmpdir}/agent-sam.tar.gz"
tar -xzf "${tmpdir}/agent-sam.tar.gz" -C "${tmpdir}" --strip-components=1
sudo rsync -a --delete --exclude '.git' --exclude '.venv' --exclude '.env' "${tmpdir}/" /opt/agent-sam/
sudo chown -R agentos:agentos /opt/agent-sam
sudo -u agentos bash -lc 'cd /opt/agent-sam && /opt/agent-sam/.venv/bin/pip install --upgrade . && /opt/agent-sam/.venv/bin/python -m alembic upgrade head'
sudo systemctl restart agent-api agent-worker agent-telegram
sudo -u agentos bash -lc 'cd /opt/agent-sam && /opt/agent-sam/.venv/bin/python -m scripts.doctor --production --env-path /opt/agent-sam/.env --app-dir /opt/agent-sam'
rm -rf "${tmpdir}"
```

## Direct production commands

Use these exact commands when operating the deployed services:

```bash
sudo systemctl daemon-reload
sudo systemctl enable agent-api agent-worker agent-telegram
sudo systemctl start agent-api agent-worker agent-telegram
sudo systemctl status agent-api agent-worker agent-telegram
sudo journalctl -u agent-api -f
sudo journalctl -u agent-worker -f
sudo journalctl -u agent-telegram -f
sudo systemctl restart agent-api agent-worker agent-telegram
sudo systemctl stop agent-api agent-worker agent-telegram
```

## Service commands

Use the service helper from a non-root account with the documented `sudo` permissions:

```bash
./deployment/linux/service-control.sh start
./deployment/linux/service-control.sh stop
./deployment/linux/service-control.sh restart api
./deployment/linux/service-control.sh status
./deployment/linux/service-control.sh logs
./deployment/linux/service-control.sh logs telegram -n 200 -f
```

The helper wraps the same `systemctl` and `journalctl` commands listed above.

## Nginx example

An nginx reverse-proxy example is provided at `deployment/nginx/agent-api.conf`. It proxies to `127.0.0.1:8000`, includes upgrade headers, and disables proxy buffering for streaming responses. Adjust `server_name`, then validate and reload nginx:

```bash
sudo install -m 0644 /opt/agent-sam/deployment/nginx/agent-api.conf /etc/nginx/sites-available/agent-api.conf
sudo nginx -t
sudo systemctl reload-or-restart nginx
```

## Rollback example

If a deploy needs to be rolled back, stop the services, restore the previous checkout, reinstall the package, rerun migrations, and start the services again:

```bash
sudo systemctl stop agent-api agent-worker agent-telegram
cd /opt/agent-sam
sudo -u agentos git checkout <previous-tag-or-commit>
sudo -u agentos /opt/agent-sam/.venv/bin/pip install --upgrade .
sudo -u agentos /opt/agent-sam/.venv/bin/python -m alembic upgrade head
sudo systemctl start agent-api agent-worker agent-telegram
```

## Notes

- The application services run as `agentos`, not root.
- `install-host-assets.sh` can run as root or from a sudo-capable operator account.
- `bootstrap-app.sh` refuses to run as root and will not run migrations until `.env` has the required production values.
- The bootstrap script requires Python 3.11 or newer.
- The Telegram service will not start until `TELEGRAM_BOT_TOKEN`, `DEFAULT_WORKSPACE_ID`, and `DEFAULT_USER_ID` are set.
- See `deployment/linux/SUDO_PERMISSIONS.md` for the exact elevated commands needed by the operator account.