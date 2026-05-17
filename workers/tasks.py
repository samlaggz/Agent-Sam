from agent.graph import AgentRunResult
from agents.runtime import get_default_specialist_runtime
from db.models import Task


async def handle_task(task: Task) -> AgentRunResult:
    runner = get_default_specialist_runtime()
    return await runner.run_task(task.id)
