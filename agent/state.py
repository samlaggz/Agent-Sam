from typing import Any, TypedDict


class TaskSnapshot(TypedDict):
    id: str
    title: str
    description: str
    workspace_id: str
    created_by_user_id: str | None
    priority: str
    metadata_json: dict[str, Any]


class PlanStepState(TypedDict, total=False):
    position: int
    title: str
    description: str
    tool_name: str | None
    command: str | None
    reason: str | None
    task_step_id: str
    subtask_id: str | None
    status: str
    tool_call_id: str | None
    approval_id: str | None


class AgentState(TypedDict, total=False):
    task_id: str
    task_run_id: str | None
    worker_name: str | None
    task: TaskSnapshot
    memory_context: list[dict[str, Any]]
    skill_context: list[dict[str, Any]]
    plan_steps: list[PlanStepState]
    step_summaries: list[str]
    final_summary: str
    final_status: str
    pending_approval_id: str | None
