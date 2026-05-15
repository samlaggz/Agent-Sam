#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -eq 0 ]]; then
  echo "Run this script from a non-root account with the documented sudo permissions." >&2
  exit 1
fi

usage() {
  cat <<'EOF'
Usage:
  service-control.sh start <api|worker|telegram|all>
  service-control.sh stop <api|worker|telegram|all>
  service-control.sh restart <api|worker|telegram|all>
  service-control.sh status <api|worker|telegram|all>
  service-control.sh logs <api|worker|telegram> [journalctl args]
EOF
}

resolve_target() {
  case "$1" in
    api)
      printf '%s\n' agent-api.service
      ;;
    worker)
      printf '%s\n' agent-worker.service
      ;;
    telegram)
      printf '%s\n' agent-telegram.service
      ;;
    all)
      printf '%s\n' agent-api.service agent-worker.service agent-telegram.service
      ;;
    *)
      return 1
      ;;
  esac
}

if [[ "$#" -lt 2 ]]; then
  usage
  exit 1
fi

COMMAND="$1"
TARGET="$2"
shift 2

mapfile -t UNITS < <(resolve_target "${TARGET}") || {
  usage
  exit 1
}

case "${COMMAND}" in
  start|stop|restart|status)
    sudo systemctl "${COMMAND}" "${UNITS[@]}"
    ;;
  logs)
    if [[ "${TARGET}" == "all" ]]; then
      echo "Logs only support a single service target." >&2
      exit 1
    fi
    sudo journalctl -u "${UNITS[0]}" "$@"
    ;;
  *)
    usage
    exit 1
    ;;
esac