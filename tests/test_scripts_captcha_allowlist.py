from __future__ import annotations

import yaml

from scripts import captcha_allowlist as captcha_allowlist_module


def test_list_command_shows_default_allowlist_when_file_is_missing(monkeypatch, tmp_path) -> None:
    allowlist_path = tmp_path / "captcha_allowlist.yaml"
    outputs: list[str] = []

    monkeypatch.setattr(captcha_allowlist_module, "ALLOWLIST_PATH", allowlist_path)

    exit_code = captcha_allowlist_module.run(["list"], output=outputs.append)

    assert exit_code == 0
    assert any("Prompt for unlisted domains: yes" == line for line in outputs)
    assert any("- www.magnific.com" == line for line in outputs)


def test_add_and_remove_commands_update_allowlist_file(monkeypatch, tmp_path) -> None:
    allowlist_path = tmp_path / "captcha_allowlist.yaml"
    outputs: list[str] = []

    monkeypatch.setattr(captcha_allowlist_module, "ALLOWLIST_PATH", allowlist_path)

    assert captcha_allowlist_module.run(["add", "https://portal.example.test/login"], output=outputs.append) == 0

    saved_after_add = yaml.safe_load(allowlist_path.read_text(encoding="utf-8"))
    assert "portal.example.test" in saved_after_add["domains"]

    outputs.clear()
    assert captcha_allowlist_module.run(["remove", "portal.example.test"], output=outputs.append) == 0

    saved_after_remove = yaml.safe_load(allowlist_path.read_text(encoding="utf-8"))
    assert "portal.example.test" not in saved_after_remove["domains"]