#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bootstrap-app.sh [--seed-dev] [--assume-yes]

Options:
  --seed-dev    Run python scripts/seed_dev.py after migrations if you confirm it.
  --assume-yes  Skip the confirmation prompt for --seed-dev.
EOF
}

RUN_DEV_SEED=false
ASSUME_YES=false

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --seed-dev)
      RUN_DEV_SEED=true
      ;;
    --assume-yes|--yes)
      ASSUME_YES=true
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 1
      ;;
  esac
  shift
done

if [[ "${EUID}" -eq 0 ]]; then
  echo "Run this script as the dedicated application user, not root." >&2
  exit 1
fi

EXPECTED_USER="${APP_USER:-agentos}"
CURRENT_USER="$(id -un)"
if [[ "${CURRENT_USER}" != "${EXPECTED_USER}" ]]; then
  echo "Run this script as ${EXPECTED_USER}. Current user: ${CURRENT_USER}." >&2
  exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-${REPO_ROOT}/.venv}"
ENV_FILE="${ENV_FILE:-${REPO_ROOT}/.env}"
ENV_EXAMPLE="${ENV_EXAMPLE:-${REPO_ROOT}/.env.example}"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "Python interpreter not found: ${PYTHON_BIN}" >&2
  exit 1
fi

PYTHON_VERSION="$(${PYTHON_BIN} - <<'PY'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}")
PY
)"
if ! "${PYTHON_BIN}" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
then
  echo "Python 3.11 or newer is required. Found ${PYTHON_VERSION}." >&2
  exit 1
fi

cd "${REPO_ROOT}"

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "Creating virtual environment at ${VENV_DIR}"
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

echo "Installing application dependencies into ${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --upgrade pip setuptools wheel hatchling
"${VENV_DIR}/bin/pip" install --upgrade .

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Creating ${ENV_FILE} from ${ENV_EXAMPLE}"
  cp "${ENV_EXAMPLE}" "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
  echo "Run python -m scripts.setup --production or edit ${ENV_FILE}, then rerun this script."
  exit 0
fi

mapfile -t MISSING_ENV_KEYS < <("${PYTHON_BIN}" - "${ENV_FILE}" <<'PY'
from pathlib import Path
import sys

from scripts.env_writer import load_env_values

env_path = Path(sys.argv[1])
values = load_env_values(env_path)

required = [
    "DATABASE_URL",
    "REDIS_URL",
    "QDRANT_URL",
    "ENABLED_GATEWAYS",
  "LITELLM_MODEL",
]
enabled_gateways = {
    token.strip().lower()
    for token in values.get("ENABLED_GATEWAYS", "").split(",")
    if token.strip()
}
if "telegram" in enabled_gateways:
    required.append("TELEGRAM_BOT_TOKEN")

for key in required:
    if not values.get(key, "").strip():
        print(key)
PY
)

if [[ "${#MISSING_ENV_KEYS[@]}" -gt 0 ]]; then
  echo "Configure ${ENV_FILE} before running migrations. Missing values:" >&2
  for key in "${MISSING_ENV_KEYS[@]}"; do
    echo "  - ${key}" >&2
  done
  echo "Run python -m scripts.setup --production or edit .env" >&2
  exit 1
fi

echo "Validating database connectivity"
if ! "${VENV_DIR}/bin/python" -m scripts.bootstrap_preflight --env-file "${ENV_FILE}"; then
  echo "Database connectivity preflight failed. Update ${ENV_FILE} and rerun this script." >&2
  exit 1
fi

echo "Running database migrations"
"${VENV_DIR}/bin/python" -m alembic upgrade head

DEFAULT_IDS_MISSING="$(${VENV_DIR}/bin/python - "${ENV_FILE}" <<'PY'
from pathlib import Path
import sys

from scripts.env_writer import load_env_values

values = load_env_values(Path(sys.argv[1]))
missing = []
for key in ("DEFAULT_USER_ID", "DEFAULT_WORKSPACE_ID"):
    if not values.get(key, "").strip():
        missing.append(key)
print(" ".join(missing))
PY
)"

if [[ -n "${DEFAULT_IDS_MISSING}" || "${RUN_DEV_SEED}" == "true" ]]; then
  if [[ "${RUN_DEV_SEED}" == "true" && "${ASSUME_YES}" != "true" ]]; then
    read -r -p "Run development seed data on this host? [y/N] " seed_answer
    case "${seed_answer:-}" in
      [Yy]|[Yy][Ee][Ss])
        ;;
      *)
        echo "Skipping development seed data."
        DEFAULT_IDS_MISSING=""
        ;;
    esac
  fi

  if [[ -n "${DEFAULT_IDS_MISSING}" || "${RUN_DEV_SEED}" == "true" ]]; then
    echo "Running development seed data"
    SEED_OUTPUT="$(${VENV_DIR}/bin/python scripts/seed_dev.py)"
    printf '%s\n' "${SEED_OUTPUT}"
    "${VENV_DIR}/bin/python" - "${ENV_FILE}" "${SEED_OUTPUT}" <<'PY'
from pathlib import Path
import sys

from scripts.common import parse_seed_output
from scripts.env_writer import update_env_file

env_path = Path(sys.argv[1])
seed_output = sys.argv[2]
user_id, workspace_id = parse_seed_output(seed_output)
updates = {}
if user_id:
    updates["DEFAULT_USER_ID"] = user_id
if workspace_id:
    updates["DEFAULT_WORKSPACE_ID"] = workspace_id
if updates:
    update_env_file(env_path, updates, preserve_existing_values=False)
PY
  fi
else
  echo "DEFAULT_USER_ID and DEFAULT_WORKSPACE_ID are already configured; seed skipped."
fi

echo "Running production doctor"
"${VENV_DIR}/bin/python" -m scripts.doctor --production --skip-runtime-checks --env-path "${ENV_FILE}" --app-dir "${REPO_ROOT}"

cat <<EOF
Bootstrap complete.

Next commands:
  python -m scripts.doctor --production
  sudo systemctl enable agent-api agent-worker agent-telegram
  sudo systemctl start agent-api agent-worker agent-telegram
  sudo systemctl status agent-api agent-worker agent-telegram
  sudo journalctl -u agent-api -f
  sudo journalctl -u agent-worker -f
  sudo journalctl -u agent-telegram -f
EOF