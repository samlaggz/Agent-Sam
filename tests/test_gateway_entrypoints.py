from gateways.cli import main as cli_main
from gateways.telegram import main as telegram_main


def test_cli_main_run_delegates_to_runner(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(cli_main, "configure_windows_event_loop_policy", lambda: calls.append("policy"))
    monkeypatch.setattr(cli_main, "run_named_gateway", lambda name: calls.append(name))

    cli_main.run()

    assert calls == ["policy", "cli"]


def test_telegram_main_run_delegates_to_runner(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(telegram_main, "configure_windows_event_loop_policy", lambda: calls.append("policy"))
    monkeypatch.setattr(telegram_main, "run_named_gateway", lambda name: calls.append(name))

    telegram_main.run()

    assert calls == ["policy", "telegram"]