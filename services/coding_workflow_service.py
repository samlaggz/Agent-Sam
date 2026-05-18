from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID


MAX_WORKFLOW_TASK_TITLE_LENGTH = 80


@dataclass(frozen=True)
class WorkflowTaskDraft:
    title: str
    description: str
    metadata_overrides: dict[str, Any]
    parent_task_id: UUID | None = None


def build_task_title(text: str, *, max_length: int = MAX_WORKFLOW_TASK_TITLE_LENGTH) -> str:
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), text.strip())
    if len(first_line) <= max_length:
        return first_line
    return f"{first_line[: max_length - 3].rstrip()}..."


def build_code_task_draft(*, task_text: str, conversation_context: str | None = None) -> WorkflowTaskDraft:
    normalized_text = task_text.strip()
    if not normalized_text:
        raise ValueError("Cannot create a task from an empty message.")

    description = normalized_text
    if conversation_context:
        description = f"{normalized_text}\n\n## Recent conversation context:\n{conversation_context}"

    return WorkflowTaskDraft(
        title=build_task_title(normalized_text),
        description=description,
        metadata_overrides={
            "requested_agent": "coding_agent",
            "harness_requested": True,
            "workflow": "code",
        },
    )


def build_test_task_draft(*, parent_task_id: UUID, parent_task_title: str) -> WorkflowTaskDraft:
    return _build_followup_workflow_draft(
        parent_task_id=parent_task_id,
        parent_task_title=parent_task_title,
        title_prefix="Test",
        description_prefix="Run focused tests and validation for task",
        requested_agent="testing_agent",
        workflow="test",
    )


def build_pr_task_draft(*, parent_task_id: UUID, parent_task_title: str) -> WorkflowTaskDraft:
    return _build_followup_workflow_draft(
        parent_task_id=parent_task_id,
        parent_task_title=parent_task_title,
        title_prefix="Prepare PR",
        description_prefix="Create a branch, commit changes, push, and open a PR for task",
        requested_agent="coding_agent",
        workflow="pr",
    )


def _build_followup_workflow_draft(
    *,
    parent_task_id: UUID,
    parent_task_title: str,
    title_prefix: str,
    description_prefix: str,
    requested_agent: str,
    workflow: str,
) -> WorkflowTaskDraft:
    return WorkflowTaskDraft(
        title=f"{title_prefix}: {parent_task_title}",
        description=f"{description_prefix} {parent_task_id}: {parent_task_title}",
        parent_task_id=parent_task_id,
        metadata_overrides={
            "requested_agent": requested_agent,
            "harness_requested": True,
            "workflow": workflow,
            "target_task_id": str(parent_task_id),
        },
    )