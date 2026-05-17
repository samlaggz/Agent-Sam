#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -eq 0 ]]; then
  echo "Run this script from a non-root account with the documented sudo permissions." >&2
  exit 1
fi

usage() {
  cat <<'EOF'
Usage:
  service-control.sh start [api|worker|telegram|all]
  service-control.sh stop [api|worker|telegram|all]
  service-control.sh restart [api|worker|telegram|all]
  service-control.sh status [api|worker|telegram|all]
  service-control.sh logs [api|worker|telegram|all] [journalctl args]
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

if [[ "$#" -lt 1 ]]; then
  usage
  exit 1
fi

COMMAND="$1"
TARGET="${2:-all}"
if [[ "$#" -ge 2 ]]; then
  shift 2
else
  shift 1
fi

mapfile -t UNITS < <(resolve_target "${TARGET}") || {
  usage
  exit 1
}

case "${COMMAND}" in
  start|stop|restart|status)
    sudo systemctl "${COMMAND}" "${UNITS[@]}"
    ;;
  logs)
    journalctl_command=(sudo journalctl)
    for unit_name in "${UNITS[@]}"; do
      journalctl_command+=(-u "${unit_name}")
    done
    journalctl_command+=("$@")
    "${journalctl_command[@]}"
    ;;
  *)
    usage
    exit 1
    ;;
esac