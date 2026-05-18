from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel

from app.config import Settings
from harness.actions import ApprovalAction, BrowserAction, FilePatchAction, FileReadAction, FileWriteAction, GitHubAction, InstallLibraryAction, InstallRepoAction, ShellAction


BLOCKED_SHELL_RE = re.compile(
    r"(^|\s)(rm\s+-rf\s+/|shutdown\b|reboot\b|halt\b|mkfs\b|dd\b|poweroff\b|format\b)",
    re.IGNORECASE,
)
APPROVAL_SHELL_RE = re.compile(
    r"(^|\s)(apt(-get)?\s+install|pip\s+install\s+-g|npm\s+install\s+-g|systemctl\b|service\b|chmod\b|chown\b|git\s+clone\b|curl\b|wget\b)",
    re.IGNORECASE,
)
LOGIN_BROWSER_RE = re.compile(r"login|sign[_ -]?in|submit|upload", re.IGNORECASE)


class PolicyDecision(BaseModel):
    allowed: bool = True
    requires_approval: bool = False
    risk_level: str = "safe"
    reason: str = "Allowed"


class HarnessPolicies:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def evaluate(self, action, *, workspace_root: Path) -> PolicyDecision:
        if isinstance(action, ApprovalAction):
            return PolicyDecision(allowed=True, reason="Approval resolution is always allowed.")
        if isinstance(action, (FileReadAction, FileWriteAction)):
            return self._evaluate_path(action.path, workspace_root)
        if isinstance(action, FilePatchAction):
            return PolicyDecision(allowed=True, reason="Patch-based file edits are allowed inside the workspace.")
        if isinstance(action, ShellAction):
            return self._evaluate_shell(action)
        if isinstance(action, InstallLibraryAction):
            return self._evaluate_library_install(action)
        if isinstance(action, InstallRepoAction):
            return self._evaluate_repo_install(action, workspace_root)
        if isinstance(action, BrowserAction):
            return self._evaluate_browser(action)
        if isinstance(action, GitHubAction):
            if action.operation == "open_pr" and not self._settings.github_auto_create_pr:
                return PolicyDecision(
                    allowed=True,
                    requires_approval=True,
                    risk_level="medium",
                    reason="Opening a pull request requires approval unless auto-create is enabled.",
                )
        return PolicyDecision(allowed=True)

    def _evaluate_path(self, path: str, workspace_root: Path) -> PolicyDecision:
        resolved = self._resolve_path(path, workspace_root)
        if not self._is_inside(resolved, workspace_root):
            return PolicyDecision(
                allowed=False,
                risk_level="dangerous",
                reason="Path is outside the task workspace.",
            )
        return PolicyDecision(allowed=True, reason="Path is inside the workspace.")

    def _evaluate_shell(self, action: ShellAction) -> PolicyDecision:
        normalized = " ".join(action.command.split())
        if BLOCKED_SHELL_RE.search(normalized):
            return PolicyDecision(
                allowed=False,
                risk_level="dangerous",
                reason="Command is destructive and blocked.",
            )
        if APPROVAL_SHELL_RE.search(normalized):
            return PolicyDecision(
                allowed=True,
                requires_approval=True,
                risk_level="high",
                reason="Command matches an approval-required shell operation.",
            )
        return PolicyDecision(allowed=True, risk_level="safe", reason="Shell command allowed.")

    def _evaluate_library_install(self, action: InstallLibraryAction) -> PolicyDecision:
        if action.scope == "global":
            return PolicyDecision(
                allowed=False,
                risk_level="dangerous",
                reason="Global package installation is blocked by default.",
            )
        if action.ecosystem == "system":
            return PolicyDecision(
                allowed=True,
                requires_approval=True,
                risk_level="high",
                reason="System package installation requires approval.",
            )
        if self._settings.agent_require_approval_for_installs:
            return PolicyDecision(
                allowed=True,
                requires_approval=True,
                risk_level="medium",
                reason="Install actions require approval by configuration.",
            )
        return PolicyDecision(allowed=True, reason="Project-scoped install allowed.")

    def _evaluate_repo_install(self, action: InstallRepoAction, workspace_root: Path) -> PolicyDecision:
        if action.destination:
            destination = self._resolve_path(action.destination, workspace_root)
            if not self._is_inside(destination, workspace_root):
                return PolicyDecision(
                    allowed=False,
                    risk_level="dangerous",
                    reason="Repository clones must stay inside the task workspace.",
                )
        if not self._settings.agent_allow_network_installs:
            return PolicyDecision(
                allowed=False,
                risk_level="dangerous",
                reason="Network installs are disabled by configuration.",
            )
        return PolicyDecision(
            allowed=True,
            requires_approval=True,
            risk_level="medium",
            reason="Repository cloning requires approval.",
        )

    def _evaluate_browser(self, action: BrowserAction) -> PolicyDecision:
        if action.url and "captcha" in action.url.lower():
            return PolicyDecision(
                allowed=False,
                risk_level="dangerous",
                reason="Browser automation must not bypass CAPTCHA.",
            )
        if any(LOGIN_BROWSER_RE.search(value or "") for value in (action.operation, action.url, action.ref, action.text)):
            return PolicyDecision(
                allowed=True,
                requires_approval=True,
                risk_level="medium",
                reason="Login, submit, and upload flows require approval.",
            )
        return PolicyDecision(allowed=True, reason="Browser action allowed.")

    def _resolve_path(self, path: str, workspace_root: Path) -> Path:
        raw_path = Path(path)
        return raw_path.resolve() if raw_path.is_absolute() else (workspace_root / raw_path).resolve()

    def _is_inside(self, path: Path, workspace_root: Path) -> bool:
        try:
            path.relative_to(workspace_root.resolve())
        except ValueError:
            return False
        return True