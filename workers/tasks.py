from agent.graph import AgentRunResult, get_default_agent_runner
from db.models import Task


async def handle_task(task: Task) -> AgentRunResult:
    runner = get_default_agent_runner()
    return await runner.run_task(task.id)
