#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required to run the Agent_Sam installer." >&2
  exit 1
fi

if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  echo "Installer is running as root; host setup will use root privileges and application bootstrap will run as agentos."
else
  echo "Installer is running as $(id -un); sudo will be used for host setup when needed."
fi

exec python3 -m scripts.install --production "$@"