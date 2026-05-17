from __future__ import annotations

import subprocess

from scripts import run as run_module


def test_run_menu_launches_api_in_new_powershell_window_on_windows(monkeypatch) -> None:
    outputs: list[str] = []
    launched_commands: list[list[str]] = []
    prompt_values = iter(["3", "14"])

    monkeypatch.setattr(run_module.sys, "platform", "win32")
    monkeypatch.setattr(run_module.subprocess, "CREATE_NEW_CONSOLE", 0, raising=False)
    monkeypatch.setattr(
        run_module.subprocess,
        "Popen",
        lambda command, **kwargs: launched_commands.append(command),
    )

    exit_code = run_module.run_menu(prompt=lambda text: next(prompt_values), output=outputs.append)

    assert exit_code == 0
    assert launched_commands
    assert launched_commands[0][0] == "powershell"
    assert "app.main" in launched_commands[0][-1]
    assert any("Launched Start API in a new PowerShell window." == message for message in outputs)


def test_run_menu_executes_doctor_inline(monkeypatch) -> None:
    outputs: list[str] = []
    prompt_values = iter(["2", "14"])
    executed_commands: list[list[str]] = []

    monkeypatch.setattr(
        run_module.subprocess,
        "run",
        lambda command, **kwargs: executed_commands.append(command) or subprocess.CompletedProcess(command, 0),
    )

    exit_code = run_module.run_menu(prompt=lambda text: next(prompt_values), output=outputs.append)

    assert exit_code == 0
    assert executed_commands
    assert executed_commands[0][:3] == [run_module.sys.executable, "-m", "scripts.doctor"]
    assert outputs[-1] == "Exiting control center."


def test_run_menu_shows_agents(monkeypatch) -> None:
    outputs: list[str] = []
    prompt_values = iter(["7", "14"])
    observed_calls: list[str] = []

    monkeypatch.setattr(
        run_module,
        "_show_agents",
        lambda *, output: observed_calls.append("show_agents") or output("Available agents:\n- coding_agent: code"),
    )

    exit_code = run_module.run_menu(prompt=lambda text: next(prompt_values), output=outputs.append)

    assert exit_code == 0
    assert observed_calls == ["show_agents"]
    assert any("Available agents:" in message for message in outputs)