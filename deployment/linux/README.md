# Linux Deployment

This deployment path is for a Linux host without Docker. The application runs as the dedicated `agentos` user and the service-management scripts are designed to be launched from a regular operator account with narrowly scoped `sudo` permissions.

## Layout

- Application checkout: choose a path such as `/srv/agentos/agent-sam/current`
- Runtime env file: `<app-dir>/.env`
- Virtual environment: `<app-dir>/.venv`
- Systemd units: `/etc/systemd/system/agent-api.service`, `/etc/systemd/system/agent-worker.service`, `/etc/systemd/system/agent-telegram.service`

## Bootstrap flow

1. Ensure the repository is checked out at the target application path, for example `/srv/agentos/agent-sam/current`.

2. From a sudo-capable operator account, install the service units and create the `agentos` user if it does not exist:

   ```bash
   ./deployment/linux/install-host-assets.sh /srv/agentos/agent-sam/current
   ```

3. Switch to the dedicated app user and run the application bootstrap:

   ```bash
   sudo -iu agentos
   cd /srv/agentos/agent-sam/current
   ./deployment/linux/bootstrap-app.sh
   ```

4. Review and complete `.env`. The bootstrap script creates it from `.env.example` if it is missing.

5. Start the services:

   ```bash
   ./deployment/linux/service-control.sh start all
   ```

## Service commands

Use the service helper from a non-root account with the documented `sudo` permissions:

```bash
./deployment/linux/service-control.sh start all
./deployment/linux/service-control.sh stop all
./deployment/linux/service-control.sh restart api
./deployment/linux/service-control.sh status worker
./deployment/linux/service-control.sh logs telegram -n 200 -f
```

## Nginx example

An nginx reverse-proxy example is provided at `deployment/nginx/agent-api.conf`. Copy it to your nginx site configuration, adjust `server_name`, then validate and reload nginx:

```bash
sudo install -m 0644 deployment/nginx/agent-api.conf /etc/nginx/sites-available/agent-api.conf
sudo nginx -t
sudo systemctl reload nginx
```

## Notes

- The application services run as `agentos`, not root.
- The host-assets installer expects the application directory to already contain the repository checkout.
- The bootstrap script requires Python 3.11 or newer.
- The Telegram service will not start until `TELEGRAM_BOT_TOKEN`, `DEFAULT_WORKSPACE_ID`, and `DEFAULT_USER_ID` are set.
- See `deployment/linux/SUDO_PERMISSIONS.md` for the exact elevated commands needed by the operator account.