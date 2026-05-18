from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation_type: str = "observation"
    success: bool = True
    summary: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ShellObservation(Observation):
    observation_type: Literal["shell"] = "shell"
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    duration_ms: int | None = None
    status: str = "completed"
    tool_call_id: UUID | None = None
    approval_id: UUID | None = None


class BrowserObservation(Observation):
    observation_type: Literal["browser"] = "browser"
    url: str | None = None
    title: str | None = None
    text: str | None = None
    screenshot_path: str | None = None
    captcha_detected: bool = False


class FileObservation(Observation):
    observation_type: Literal["file"] = "file"
    path: str | None = None
    content: str | None = None
    patch_id: str | None = None
    changed: bool = False


class GitObservation(Observation):
    observation_type: Literal["git"] = "git"
    branch: str | None = None
    commit_sha: str | None = None
    output: str | None = None


class GitHubObservation(Observation):
    observation_type: Literal["github"] = "github"
    url: str | None = None
    number: int | None = None
    state: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class SourceCacheObservation(Observation):
    observation_type: Literal["source_cache"] = "source_cache"
    cache_path: str | None = None
    matches: list[dict[str, Any]] = Field(default_factory=list)


class ErrorObservation(Observation):
    observation_type: Literal["error"] = "error"
    success: bool = False
    error_type: str = "error"
    error_message: str = ""


class ApprovalObservation(Observation):
    observation_type: Literal["approval"] = "approval"
    success: bool = False
    approval_id: UUID | None = None
    status: Literal["pending", "approved", "rejected"] = "pending"


AnyObservation = (
    Observation
    | ShellObservation
    | BrowserObservation
    | FileObservation
    | GitObservation
    | GitHubObservation
    | SourceCacheObservation
    | ErrorObservation
    | ApprovalObservation
)