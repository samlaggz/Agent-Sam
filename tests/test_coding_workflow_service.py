from __future__ import annotations

from uuid import uuid4

from services.coding_workflow_service import build_code_task_draft, build_pr_task_draft, build_test_task_draft


def test_build_code_task_draft_includes_context_and_metadata() -> None:
    draft = build_code_task_draft(
        task_text="Implement harness command routing",
        conversation_context="user: please wire the new commands",
    )

    assert draft.title == "Implement harness command routing"
    assert "## Recent conversation context:" in draft.description
    assert draft.metadata_overrides["requested_agent"] == "coding_agent"
    assert draft.metadata_overrides["workflow"] == "code"


def test_build_followup_workflow_drafts_include_parent_task_metadata() -> None:
    parent_task_id = uuid4()

    test_draft = build_test_task_draft(parent_task_id=parent_task_id, parent_task_title="Implement harness command routing")
    pr_draft = build_pr_task_draft(parent_task_id=parent_task_id, parent_task_title="Implement harness command routing")

    assert test_draft.parent_task_id == parent_task_id
    assert test_draft.metadata_overrides["requested_agent"] == "testing_agent"
    assert test_draft.metadata_overrides["target_task_id"] == str(parent_task_id)
    assert pr_draft.title == "Prepare PR: Implement harness command routing"
    assert pr_draft.metadata_overrides["workflow"] == "pr"