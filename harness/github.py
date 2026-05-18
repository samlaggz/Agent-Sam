from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any
from urllib import request

from app.config import Settings
from harness.exceptions import GitHubAutomationError


class GitHubService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def git_status(self, repo_path: str | Path) -> str:
        return self._run_git(repo_path, ["status", "--short", "--branch"])

    def create_branch(self, repo_path: str | Path, branch_name: str) -> str:
        self._ensure_not_protected(branch_name)
        return self._run_git(repo_path, ["checkout", "-b", branch_name])

    def commit_changes(self, repo_path: str | Path, message: str) -> str:
        self._run_git(repo_path, ["add", "-A"])
        return self._run_git(repo_path, ["commit", "-m", message])

    def push_branch(self, repo_path: str | Path, branch_name: str) -> str:
        self._ensure_not_protected(branch_name)
        return self._run_git(repo_path, ["push", "-u", "origin", branch_name])

    def open_pull_request(
        self,
        *,
        title: str,
        body: str,
        head: str,
        base: str | None = None,
        repository: str | None = None,
    ) -> dict[str, Any]:
        repo = repository or self._settings.github_repository
        if not repo:
            raise GitHubAutomationError("GITHUB_REPOSITORY is not configured.")
        payload = {"title": title, "body": body, "head": head, "base": base or self._settings.github_default_branch}
        return self._api_json(
            "POST",
            f"https://api.github.com/repos/{repo}/pulls",
            payload,
        )

    def comment_on_pr(self, *, pull_number: int, body: str, repository: str | None = None) -> dict[str, Any]:
        repo = repository or self._settings.github_repository
        if not repo:
            raise GitHubAutomationError("GITHUB_REPOSITORY is not configured.")
        return self._api_json(
            "POST",
            f"https://api.github.com/repos/{repo}/issues/{pull_number}/comments",
            {"body": body},
        )

    def read_issue(self, *, issue_number: int, repository: str | None = None) -> dict[str, Any]:
        repo = repository or self._settings.github_repository
        if not repo:
            raise GitHubAutomationError("GITHUB_REPOSITORY is not configured.")
        return self._api_json(
            "GET",
            f"https://api.github.com/repos/{repo}/issues/{issue_number}",
            None,
        )

    def redact_token(self, value: str) -> str:
        token = self._settings.github_token.strip()
        if token and token in value:
            return value.replace(token, "[REDACTED]")
        return value

    def _api_json(self, method: str, url: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        token = self._settings.github_token.strip()
        if not token:
            raise GitHubAutomationError("GITHUB_TOKEN is not configured.")
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        req = request.Request(url, data=data, headers=headers, method=method)
        with request.urlopen(req) as response:
            raw = response.read().decode("utf-8")
        return json.loads(raw)

    def _run_git(self, repo_path: str | Path, args: list[str]) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=str(Path(repo_path)),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise GitHubAutomationError(result.stderr.strip() or result.stdout.strip() or "git command failed")
        return result.stdout.strip()

    def _ensure_not_protected(self, branch_name: str) -> None:
        protected = {self._settings.github_default_branch, "master"}
        if branch_name in protected:
            raise GitHubAutomationError(f"Protected branch operations are blocked: {branch_name}")