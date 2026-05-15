from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RiskRule:
    name: str
    pattern: str
    reason: str

    def matches(self, command: str) -> bool:
        return re.search(self.pattern, command, re.IGNORECASE) is not None


BLOCKED_RULES = [
    RiskRule(
        name="delete-root-filesystem",
        pattern=r"(^|\s)rm\s+-rf\s+/(\s|$)",
        reason="Deleting the root filesystem is blocked.",
    ),
    RiskRule(
        name="format-disk",
        pattern=r"(^|\s)(mkfs(\.[a-z0-9]+)?|diskpart|fdisk|format)(\s|$)",
        reason="Disk formatting commands are blocked.",
    ),
    RiskRule(
        name="shutdown-host",
        pattern=r"(^|\s)(shutdown|reboot|halt|poweroff)(\s|$)",
        reason="Host shutdown and reboot commands are blocked.",
    ),
    RiskRule(
        name="delete-user",
        pattern=r"\b(userdel|deluser|net\s+user\s+[^\s]+\s+/delete)\b",
        reason="User deletion commands are blocked.",
    ),
    RiskRule(
        name="secret-exfiltration",
        pattern=(
            r"((\.env|id_rsa|id_dsa|\.ssh|\.aws|\.npmrc|\.pypirc|/etc/shadow|/etc/passwd).*(curl|wget|scp|sftp|ftp|nc|ncat|netcat|Invoke-WebRequest|Invoke-RestMethod))|"
            r"((curl|wget|scp|sftp|ftp|nc|ncat|netcat|Invoke-WebRequest|Invoke-RestMethod).*(\.env|id_rsa|id_dsa|\.ssh|\.aws|\.npmrc|\.pypirc|/etc/shadow|/etc/passwd))"
        ),
        reason="Commands that appear to exfiltrate secrets are blocked.",
    ),
]


DANGEROUS_RULES = [
    RiskRule(
        name="privilege-escalation",
        pattern=r"\b(sudo|su|doas|runas)\b",
        reason="Privilege escalation requires explicit human review.",
    ),
    RiskRule(
        name="user-management",
        pattern=r"\b(useradd|adduser|usermod|passwd|net\s+user)\b",
        reason="User account changes are dangerous.",
    ),
    RiskRule(
        name="filesystem-mounting",
        pattern=r"\b(mount|umount|diskutil|chkdsk)\b",
        reason="Mount and disk maintenance commands are dangerous.",
    ),
]


HIGH_RISK_RULES = [
    RiskRule(
        name="shell-interpreter",
        pattern=r"^(bash|sh|zsh|pwsh|powershell|cmd)(\s|$)",
        reason="Invoking an interactive shell requires approval.",
    ),
    RiskRule(
        name="destructive-file-ops",
        pattern=r"\b(rm|del|erase|rmdir|rd|mv|move|copy|cp|xcopy|robocopy|sed\s+-i|perl\s+-pi)\b",
        reason="Commands that modify or remove files are high risk.",
    ),
    RiskRule(
        name="service-control",
        pattern=r"^(systemctl|service|sc|taskkill|kill|pkill|killall|docker|kubectl|helm|terraform|ansible)\b",
        reason="System and orchestration commands are high risk.",
    ),
    RiskRule(
        name="git-write-ops",
        pattern=r"^git\s+(push|rebase|reset\s+--hard|clean|stash\s+drop|stash\s+clear)\b",
        reason="Git commands that rewrite history or publish changes are high risk.",
    ),
    RiskRule(
        name="permission-changes",
        pattern=r"\b(chmod|chown|takeown|icacls)\b",
        reason="Permission changes are high risk.",
    ),
]


MEDIUM_RISK_RULES = [
    RiskRule(
        name="package-management",
        pattern=r"^(pip|pip3|uv|poetry|npm|pnpm|yarn|apt|apt-get|brew|choco|winget)\b",
        reason="Installing or updating packages requires approval.",
    ),
    RiskRule(
        name="network-request",
        pattern=r"^(curl|wget|Invoke-WebRequest|Invoke-RestMethod)\b",
        reason="Network requests require approval.",
    ),
    RiskRule(
        name="git-mutable-local",
        pattern=r"^git\s+(add|commit|merge|pull|checkout|switch|restore|reset|stash|apply)\b",
        reason="Git commands that mutate the working copy require approval.",
    ),
]


SAFE_RULES = [
    RiskRule(
        name="git-read-only",
        pattern=r"^git\s+(status|diff|log|show|branch|rev-parse|fetch\s+--dry-run)\b",
        reason="Read-only git inspection is safe.",
    ),
    RiskRule(
        name="filesystem-inspection",
        pattern=r"^(pwd|whoami|id|uname|ls|dir|Get-ChildItem|Get-Location|type|cat|head|tail|find|rg|ripgrep|where|which)\b",
        reason="Read-only inspection command is safe.",
    ),
    RiskRule(
        name="python-version",
        pattern=r"^(python|python3|py)\s+--version\b",
        reason="Checking the Python version is safe.",
    ),
]