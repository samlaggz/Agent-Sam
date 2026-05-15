#!/usr/bin/env bash
set -euo pipefail

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
if [[ "${PYTHON_VERSION}" < "3.11" ]]; then
  echo "Python 3.11 or newer is required. Found ${PYTHON_VERSION}." >&2
  exit 1
fi

cd "${REPO_ROOT}"

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "Creating virtual environment at ${VENV_DIR}"
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

echo "Installing application dependencies"
"${VENV_DIR}/bin/pip" install --upgrade pip setuptools wheel
"${VENV_DIR}/bin/pip" install --upgrade .

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Creating ${ENV_FILE} from ${ENV_EXAMPLE}"
  cp "${ENV_EXAMPLE}" "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
fi

echo "Running database migrations"
"${VENV_DIR}/bin/alembic" upgrade head

echo "Bootstrap complete. Review ${ENV_FILE} before starting services."