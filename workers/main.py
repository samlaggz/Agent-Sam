import asyncio
import logging

from db.session import AsyncSessionLocal
from db.task_queue import claim_next_task, complete_task, fail_task, pause_task
from workers.tasks import handle_task


logger = logging.getLogger(__name__)
POLL_INTERVAL_SECONDS = 5
DEFAULT_WORKER_NAME = "worker-1"


async def worker_loop(worker_name: str = DEFAULT_WORKER_NAME) -> None:
    while True:
        async with AsyncSessionLocal() as session:
            task = await claim_next_task(session, worker_name=worker_name)

        if task is None:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            continue

        logger.info("Claimed task %s with priority %s", task.id, task.priority)

        try:
            result = await handle_task(task)
        except Exception as exc:
            logger.exception("Task %s failed during worker execution", task.id)
            async with AsyncSessionLocal() as session:
                await fail_task(session, task_id=task.id, error_text=str(exc))
        else:
            async with AsyncSessionLocal() as session:
                if result.status == "completed":
                    await complete_task(session, task_id=task.id)
                elif result.status == "paused":
                    await pause_task(session, task_id=task.id)
                elif result.status == "failed":
                    await fail_task(session, task_id=task.id, error_text=result.final_summary)
                else:
                    await fail_task(
                        session,
                        task_id=task.id,
                        error_text=f"Unexpected agent result status: {result.status}",
                    )


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    logger.info("Starting Agent Sam worker")
    asyncio.run(worker_loop())
