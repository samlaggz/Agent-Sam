# Sudo Permissions

The deployment scripts are intended to run from a regular operator account, not from a root shell. Grant only the commands needed for host provisioning and service management.

## Required elevated actions

For `deployment/linux/install-host-assets.sh`:

- `useradd --system --create-home --shell /bin/bash agentos`
- `install -m 0644 <rendered-service> /etc/systemd/system/agent-api.service`
- `install -m 0644 <rendered-service> /etc/systemd/system/agent-worker.service`
- `install -m 0644 <rendered-service> /etc/systemd/system/agent-telegram.service`
- `systemctl daemon-reload`
- `systemctl enable agent-api.service agent-worker.service agent-telegram.service`

For `deployment/linux/service-control.sh`:

- `systemctl start agent-api.service agent-worker.service agent-telegram.service`
- `systemctl stop agent-api.service agent-worker.service agent-telegram.service`
- `systemctl restart agent-api.service agent-worker.service agent-telegram.service`
- `systemctl status agent-api.service agent-worker.service agent-telegram.service`
- `journalctl -u agent-api.service`
- `journalctl -u agent-worker.service`
- `journalctl -u agent-telegram.service`

For nginx operations after editing the example config:

- `install -m 0644 deployment/nginx/agent-api.conf /etc/nginx/sites-available/agent-api.conf`
- `nginx -t`
- `systemctl reload nginx`

## Example sudoers sketch

Adapt this to your distro and operator account name. Review carefully before use.

```sudoers
Cmnd_Alias AGENTOS_PROVISION = /usr/sbin/useradd --system --create-home --shell /bin/bash agentos, \
    /usr/bin/install -m 0644 * /etc/systemd/system/agent-api.service, \
    /usr/bin/install -m 0644 * /etc/systemd/system/agent-worker.service, \
    /usr/bin/install -m 0644 * /etc/systemd/system/agent-telegram.service, \
    /usr/bin/systemctl daemon-reload, \
    /usr/bin/systemctl enable agent-api.service agent-worker.service agent-telegram.service

Cmnd_Alias AGENTOS_RUNTIME = /usr/bin/systemctl start agent-api.service agent-worker.service agent-telegram.service, \
    /usr/bin/systemctl stop agent-api.service agent-worker.service agent-telegram.service, \
    /usr/bin/systemctl restart agent-api.service agent-worker.service agent-telegram.service, \
    /usr/bin/systemctl status agent-api.service agent-worker.service agent-telegram.service, \
    /usr/bin/journalctl -u agent-api.service *, \
    /usr/bin/journalctl -u agent-worker.service *, \
    /usr/bin/journalctl -u agent-telegram.service *

Cmnd_Alias AGENTOS_NGINX = /usr/bin/install -m 0644 * /etc/nginx/sites-available/agent-api.conf, \
    /usr/sbin/nginx -t, \
    /usr/bin/systemctl reload nginx

deployer ALL=(root) NOPASSWD: AGENTOS_PROVISION, AGENTOS_RUNTIME, AGENTOS_NGINX
```

Keep the sudoers policy as narrow as possible for your environment.