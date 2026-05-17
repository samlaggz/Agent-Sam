#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  install-host-assets.sh [--dry-run] [--skip-nginx] [target-dir]
EOF
}

DRY_RUN=false
SKIP_NGINX=false
POSITIONAL_ARGS=()

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=true
      ;;
    --skip-nginx)
      SKIP_NGINX=true
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      POSITIONAL_ARGS+=("$1")
      ;;
  esac
  shift
done

set -- "${POSITIONAL_ARGS[@]}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
APP_USER="${APP_USER:-agentos}"
APP_GROUP="${APP_GROUP:-${APP_USER}}"
APP_DIR="${1:-/opt/agent-sam}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"

if [[ "${EUID}" -eq 0 ]]; then
  echo "Host setup is running as root; app bootstrap will run as ${APP_USER}."
  SUDO_PREFIX=()
else
  if [[ "${DRY_RUN}" != "true" ]] && ! command -v sudo >/dev/null 2>&1; then
    echo "sudo is required when install-host-assets.sh is run from a non-root account." >&2
    exit 1
  fi
  SUDO_PREFIX=(sudo)
fi

run_privileged() {
  if [[ "${DRY_RUN}" == "true" ]]; then
    echo "DRY RUN: ${*}"
    return 0
  fi
  if [[ "${#SUDO_PREFIX[@]}" -gt 0 ]]; then
    "${SUDO_PREFIX[@]}" "$@"
    return
  fi
  "$@"
}

create_app_group() {
  if getent group "${APP_GROUP}" >/dev/null 2>&1; then
    return
  fi

  echo "Creating dedicated Linux group ${APP_GROUP}"
  run_privileged groupadd --system "${APP_GROUP}"
}

create_app_user() {
  if id -u "${APP_USER}" >/dev/null 2>&1; then
    return
  fi

  echo "Creating dedicated Linux user ${APP_USER}"
  run_privileged useradd --system --create-home --gid "${APP_GROUP}" --shell /bin/bash "${APP_USER}"
}

prepare_app_directory() {
  echo "Ensuring application directory ${APP_DIR} exists and is owned by ${APP_USER}"
  run_privileged install -d -m 0750 -o "${APP_USER}" -g "${APP_GROUP}" "${APP_DIR}"
  run_privileged chown -R "${APP_USER}:${APP_GROUP}" "${APP_DIR}"
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

  run_privileged install -m 0644 "${temp_file}" "${destination_file}"
  rm -f "${temp_file}"
}

create_app_group
create_app_user
prepare_app_directory

echo "Installing systemd units into ${SYSTEMD_DIR}"
render_template "${REPO_ROOT}/deployment/systemd/agent-api.service" "${SYSTEMD_DIR}/agent-api.service"
render_template "${REPO_ROOT}/deployment/systemd/agent-worker.service" "${SYSTEMD_DIR}/agent-worker.service"
render_template "${REPO_ROOT}/deployment/systemd/agent-telegram.service" "${SYSTEMD_DIR}/agent-telegram.service"

echo "Reloading systemd daemon"
run_privileged systemctl daemon-reload

if [[ "${SKIP_NGINX}" == "true" ]]; then
  echo "Skipping nginx example install."
elif command -v nginx >/dev/null 2>&1; then
  if [[ -d /etc/nginx/sites-available ]]; then
    echo "Installing nginx example into /etc/nginx/sites-available/agent-api.conf"
    run_privileged install -m 0644 "${REPO_ROOT}/deployment/nginx/agent-api.conf" "/etc/nginx/sites-available/agent-api.conf"
  elif [[ -d /etc/nginx/conf.d ]]; then
    echo "Installing nginx example into /etc/nginx/conf.d/agent-api.conf.example"
    run_privileged install -m 0644 "${REPO_ROOT}/deployment/nginx/agent-api.conf" "/etc/nginx/conf.d/agent-api.conf.example"
  else
    echo "nginx detected but no supported config directory was found; leaving the example in the repository"
  fi
else
  echo "nginx not detected; skipping nginx example install"
fi

cat <<EOF
Host assets installed.

Next steps:
  1. Sync the repository into ${APP_DIR}
    sudo rsync -a --delete --exclude '.git' --exclude '.venv' "${REPO_ROOT}/" "${APP_DIR}/"
    sudo chown -R ${APP_USER}:${APP_GROUP} "${APP_DIR}"
  2. Switch to ${APP_USER} and bootstrap the app
    sudo -iu ${APP_USER}
    cd ${APP_DIR}
    bash deployment/linux/bootstrap-app.sh
  3. Or run the one-command installer from the repo root
    bash install.sh

Nginx example:
  ${REPO_ROOT}/deployment/nginx/agent-api.conf
EOF