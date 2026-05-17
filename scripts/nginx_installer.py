from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Sequence

from scripts.common import format_command, run_subprocess


OutputFunc = Callable[[str], None]
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def install_nginx_config(
    *,
    source_config: Path,
    server_name: str,
    dry_run: bool,
    output: OutputFunc,
    command_runner: CommandRunner = run_subprocess,
    privileged_prefix: Sequence[str] = (),
) -> bool:
    if shutil.which("nginx") is None:
        output("nginx is not installed; skipping nginx configuration.")
        return True

    destination = Path("/etc/nginx/sites-available/agent-api.conf")
    enabled_symlink = Path("/etc/nginx/sites-enabled/agent-api.conf")
    rendered_config = source_config.read_text(encoding="utf-8").replace(
        "agent-sam.example.com",
        server_name.strip() or "agent-sam.example.com",
    )

    if dry_run:
        output(f"Dry run: would install nginx config to {destination}")
        output(f"Dry run: would link {enabled_symlink} -> {destination}")
        output("Dry run: would run nginx -t and reload or start nginx")
        return True

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(rendered_config)
        temp_path = Path(handle.name)

    try:
        install_command = [*privileged_prefix, "install", "-m", "0644", str(temp_path), str(destination)]
        if command_runner(install_command, capture_output=True).returncode != 0:
            output(f"Failed: {format_command(install_command)}")
            return False

        if enabled_symlink.parent.exists():
            link_command = [*privileged_prefix, "ln", "-sf", str(destination), str(enabled_symlink)]
            if command_runner(link_command, capture_output=True).returncode != 0:
                output(f"Failed: {format_command(link_command)}")
                return False

        test_command = [*privileged_prefix, "nginx", "-t"]
        test_result = command_runner(test_command, capture_output=True)
        if test_result.returncode != 0:
            if test_result.stderr:
                output(test_result.stderr.strip())
            output(f"Failed: {format_command(test_command)}")
            return False

        reload_command = [*privileged_prefix, "systemctl", "reload-or-restart", "nginx"]
        reload_result = command_runner(reload_command, capture_output=True)
        if reload_result.returncode != 0:
            if reload_result.stderr:
                output(reload_result.stderr.strip())
            output(
                "nginx configuration was installed, but nginx could not be reloaded automatically. "
                "Inspect systemctl status nginx and journalctl -xeu nginx, then reload or restart nginx manually."
            )
            return True
        output("nginx configuration installed and applied.")
        return True
    finally:
        temp_path.unlink(missing_ok=True)