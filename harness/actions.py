from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ActionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_type: str


class MessageAction(ActionModel):
    action_type: Literal["message"] = "message"
    content: str


class ShellAction(ActionModel):
    action_type: Literal["shell"] = "shell"
    command: str
    cwd: str | None = None
    reason: str = "Agent requested shell execution."
    timeout_seconds: float | None = None


class BrowserAction(ActionModel):
    action_type: Literal["browser"] = "browser"
    operation: Literal["open", "screenshot", "extract_text", "click", "type", "wait", "close", "snapshot", "press", "scroll"]
    url: str | None = None
    ref: str | None = None
    text: str | None = None
    key: str | None = None
    wait_ms: int | None = None
    direction: Literal["up", "down"] | None = None


class FileReadAction(ActionModel):
    action_type: Literal["file_read"] = "file_read"
    path: str


class FileWriteAction(ActionModel):
    action_type: Literal["file_write"] = "file_write"
    path: str
    content: str
    create_parents: bool = True
    dry_run: bool = False


class FilePatchAction(ActionModel):
    action_type: Literal["file_patch"] = "file_patch"
    diff: str
    dry_run: bool = False


class SearchAction(ActionModel):
    action_type: Literal["search"] = "search"
    query: str
    path: str | None = None
    max_results: int = 50


class InstallLibraryAction(ActionModel):
    action_type: Literal["install_library"] = "install_library"
    ecosystem: Literal["python", "node", "system", "other"]
    package_name: str
    version: str | None = None
    reason: str
    scope: Literal["workspace", "project", "global"] = "workspace"


class InstallRepoAction(ActionModel):
    action_type: Literal["install_repo"] = "install_repo"
    repo_url: str
    destination: str | None = None
    reason: str


class GitAction(ActionModel):
    action_type: Literal["git"] = "git"
    operation: Literal["status", "branch", "commit", "push", "checkout", "diff"]
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    message: str | None = None


class GitHubAction(ActionModel):
    action_type: Literal["github"] = "github"
    operation: Literal["open_pr", "comment_pr", "read_issue"]
    payload: dict[str, Any] = Field(default_factory=dict)


class SourceCacheAction(ActionModel):
    action_type: Literal["source_cache"] = "source_cache"
    operation: Literal["fetch", "search", "path", "read", "summarize"]
    package_spec: str
    query: str | None = None
    path: str | None = None


class ApprovalAction(ActionModel):
    action_type: Literal["approval"] = "approval"
    approval_id: UUID
    decision: Literal["approve", "reject"]
    reason: str | None = None


class FinishAction(ActionModel):
    action_type: Literal["finish"] = "finish"
    summary: str


ACTION_MODELS = {
    "message": MessageAction,
    "shell": ShellAction,
    "browser": BrowserAction,
    "file_read": FileReadAction,
    "file_write": FileWriteAction,
    "file_patch": FilePatchAction,
    "search": SearchAction,
    "install_library": InstallLibraryAction,
    "install_repo": InstallRepoAction,
    "git": GitAction,
    "github": GitHubAction,
    "source_cache": SourceCacheAction,
    "approval": ApprovalAction,
    "finish": FinishAction,
}

AnyAction = (
    MessageAction
    | ShellAction
    | BrowserAction
    | FileReadAction
    | FileWriteAction
    | FilePatchAction
    | SearchAction
    | InstallLibraryAction
    | InstallRepoAction
    | GitAction
    | GitHubAction
    | SourceCacheAction
    | ApprovalAction
    | FinishAction
)


def parse_action(payload: dict[str, Any]) -> AnyAction:
    action_type = str(payload.get("action_type", "")).strip().lower()
    model = ACTION_MODELS.get(action_type)
    if model is None:
        raise ValueError(f"Unsupported action_type: {action_type or '<missing>'}")
    return model.model_validate(payload)