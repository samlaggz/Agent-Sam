#!/usr/bin/env bash
set -euo pipefail

SCRIPT_SOURCE="${BASH_SOURCE[0]:-install.sh}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${SCRIPT_SOURCE}")" >/dev/null 2>&1 && pwd -P || pwd -P)"
AUTH_TOKEN="${AGENT_SAM_GITHUB_TOKEN:-${GITHUB_TOKEN:-}}"

download_installer_asset() {
  local url destination
  url="$1"
  destination="$2"

  if command -v curl >/dev/null 2>&1; then
    if [[ -n "${AUTH_TOKEN}" ]]; then
      curl -fsSL -H "Authorization: Bearer ${AUTH_TOKEN}" "${url}" -o "${destination}"
    else
      curl -fsSL "${url}" -o "${destination}"
    fi
    return 0
  fi

  if command -v wget >/dev/null 2>&1; then
    if [[ -n "${AUTH_TOKEN}" ]]; then
      wget --header="Authorization: Bearer ${AUTH_TOKEN}" -qO "${destination}" "${url}"
    else
      wget -qO "${destination}" "${url}"
    fi
    return 0
  fi

  echo "curl or wget is required to download the Agent_Sam installer bundle." >&2
  exit 1
}

bootstrap_remote_bundle() {
  local repo_slug repo_ref archive_url bootstrap_dir archive_path

  repo_slug="${AGENT_SAM_INSTALL_REPO:-samlaggz/Agent-Sam}"
  repo_ref="${AGENT_SAM_INSTALL_REF:-main}"
  if [[ -n "${AUTH_TOKEN}" ]]; then
    archive_url="${AGENT_SAM_INSTALL_ARCHIVE_URL:-https://api.github.com/repos/${repo_slug}/tarball/${repo_ref}}"
  else
    archive_url="${AGENT_SAM_INSTALL_ARCHIVE_URL:-https://github.com/${repo_slug}/archive/refs/heads/${repo_ref}.tar.gz}"
  fi
  bootstrap_dir="$(mktemp -d)"
  archive_path="${bootstrap_dir}/agent-sam.tar.gz"

  cleanup_bootstrap_dir() {
    rm -rf "${bootstrap_dir}"
  }

  trap cleanup_bootstrap_dir EXIT

  echo "Bootstrapping Agent_Sam installer from ${archive_url}"
  download_installer_asset "${archive_url}" "${archive_path}"

  tar -xzf "${archive_path}" -C "${bootstrap_dir}" --strip-components=1
  cd "${bootstrap_dir}"
}

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required to run the Agent_Sam installer." >&2
  exit 1
fi

if [[ -f "${SCRIPT_DIR}/scripts/install.py" ]]; then
  cd "${SCRIPT_DIR}"
else
  bootstrap_remote_bundle
fi

if [[ ! -f "scripts/install.py" ]]; then
  echo "Unable to locate scripts/install.py after bootstrap." >&2
  exit 1
fi

if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  echo "Installer is running as root; host setup will use root privileges and application bootstrap will run as agentos."
else
  echo "Installer is running as $(id -un); sudo will be used for host setup when needed."
fi

python3 -m scripts.install --production "$@"