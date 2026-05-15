#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -eq 0 ]]; then
  echo "Run this script from a sudo-capable operator account, not a root shell." >&2
  exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
APP_USER="${APP_USER:-agentos}"
APP_GROUP="${APP_GROUP:-${APP_USER}}"
APP_DIR="${1:-${REPO_ROOT}}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"

if [[ ! -d "${APP_DIR}" ]]; then
  echo "Application directory does not exist: ${APP_DIR}" >&2
  exit 1
fi
if [[ ! -f "${APP_DIR}/pyproject.toml" ]]; then
  echo "Application directory must contain an Agent Sam checkout: ${APP_DIR}" >&2
  exit 1
fi

create_app_user() {
  if id -u "${APP_USER}" >/dev/null 2>&1; then
    return
  fi

  echo "Creating dedicated Linux user ${APP_USER}"
  sudo useradd --system --create-home --shell /bin/bash "${APP_USER}"
}

render_template() {
  local source_file="$1"
  local destination_file="$2"
  local temp_file
  temp_file="$(mktemp)"

  APP_DIR="${APP_DIR}" APP_USER="${APP_USER}" APP_GROUP="${APP_GROUP}" python3 - "${source_file}" "${temp_file}" <<'PY'
from pathlib import Path
import os
import sys

source = Path(sys.argv[1]).read_text(encoding="utf-8")
rendered = (
    source.replace("__APP_DIR__", os.environ["APP_DIR"])
    .replace("__APP_USER__", os.environ["APP_USER"])
    .replace("__APP_GROUP__", os.environ["APP_GROUP"])
)
Path(sys.argv[2]).write_text(rendered, encoding="utf-8")
PY

  sudo install -m 0644 "${temp_file}" "${destination_file}"
  rm -f "${temp_file}"
}

create_app_user

echo "Installing systemd units into ${SYSTEMD_DIR}"
render_template "${REPO_ROOT}/deployment/systemd/agent-api.service" "${SYSTEMD_DIR}/agent-api.service"
render_template "${REPO_ROOT}/deployment/systemd/agent-worker.service" "${SYSTEMD_DIR}/agent-worker.service"
render_template "${REPO_ROOT}/deployment/systemd/agent-telegram.service" "${SYSTEMD_DIR}/agent-telegram.service"

echo "Reloading systemd daemon and enabling services"
sudo systemctl daemon-reload
sudo systemctl enable agent-api.service agent-worker.service agent-telegram.service

cat <<EOF
Host assets installed.

Next steps:
  1. Switch to ${APP_USER} and run deployment/linux/bootstrap-app.sh
  2. Review ${APP_DIR}/.env
  3. Start services with deployment/linux/service-control.sh start all

Nginx example:
  ${REPO_ROOT}/deployment/nginx/agent-api.conf
EOF