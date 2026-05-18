from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from harness.exceptions import GitHubAutomationError
from harness.github import GitHubService


def test_github_service_blocks_protected_branch() -> None:
    service = GitHubService(Settings(github_default_branch="main"))
    with pytest.raises(GitHubAutomationError):
        service.create_branch(Path("."), "main")


def test_github_service_redacts_token() -> None:
    service = GitHubService(Settings(github_token="secret-token"))
    assert service.redact_token("Bearer secret-token") == "Bearer [REDACTED]"


def test_github_service_pr_payload(monkeypatch) -> None:
    service = GitHubService(Settings(github_token="abc", github_repository="owner/repo"))
    captured: dict[str, object] = {}

    def fake_api_json(method: str, url: str, payload):
        captured["method"] = method
        captured["url"] = url
        captured["payload"] = payload
        return {"html_url": "https://github.com/owner/repo/pull/1", "number": 1, "state": "open"}

    monkeypatch.setattr(service, "_api_json", fake_api_json)
    result = service.open_pull_request(title="Test", body="Body", head="agent-sam/test")

    assert result["number"] == 1
    assert captured["method"] == "POST"
    assert captured["payload"] == {
        "title": "Test",
        "body": "Body",
        "head": "agent-sam/test",
        "base": "main",
    }