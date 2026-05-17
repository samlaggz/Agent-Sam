from pathlib import Path

import subprocess

from scripts import nginx_installer as nginx_module


def test_install_nginx_config_uses_reload_or_restart_for_inactive_service(tmp_path: Path, monkeypatch) -> None:
    source_config = tmp_path / "agent-api.conf"
    source_config.write_text("server_name agent-sam.example.com;\n", encoding="utf-8")
    commands: list[list[str]] = []
    outputs: list[str] = []

    monkeypatch.setattr(nginx_module.shutil, "which", lambda name: "/usr/sbin/nginx" if name == "nginx" else None)

    installed = nginx_module.install_nginx_config(
        source_config=source_config,
        server_name="ai.goonlinefast.com",
        dry_run=False,
        output=outputs.append,
        command_runner=lambda command, **kwargs: commands.append(list(command)) or __import__("subprocess").CompletedProcess(command, 0, stdout="ok", stderr=""),
    )

    assert installed is True
    assert ["systemctl", "reload-or-restart", "nginx"] in commands
    assert outputs[-1] == "nginx configuration installed and applied."


def test_install_nginx_config_warns_and_continues_when_nginx_apply_fails(tmp_path: Path, monkeypatch) -> None:
    source_config = tmp_path / "agent-api.conf"
    source_config.write_text("server_name agent-sam.example.com;\n", encoding="utf-8")
    outputs: list[str] = []

    monkeypatch.setattr(nginx_module.shutil, "which", lambda name: "/usr/sbin/nginx" if name == "nginx" else None)

    def command_runner(command, **kwargs):
        if command[:2] == ["systemctl", "reload-or-restart"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="Job for nginx.service failed.")
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    installed = nginx_module.install_nginx_config(
        source_config=source_config,
        server_name="ai.goonlinefast.com",
        dry_run=False,
        output=outputs.append,
        command_runner=command_runner,
    )

    assert installed is True
    assert outputs[-2] == "Job for nginx.service failed."
    assert outputs[-1] == (
        "nginx configuration was installed, but nginx could not be reloaded automatically. "
        "Inspect systemctl status nginx and journalctl -xeu nginx, then reload or restart nginx manually."
    )