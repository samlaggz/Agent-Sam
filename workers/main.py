from __future__ import annotations

import argparse
import asyncio
import logging
import os
from collections.abc import Sequence

from app.asyncio_compat import configure_windows_event_loop_policy
from db.session import AsyncSessionLocal
from db.task_queue import claim_next_task, complete_task, fail_task, pause_task
from workers.tasks import handle_task


logger = logging.getLogger(__name__)
POLL_INTERVAL_SECONDS = 5
DEFAULT_WORKER_NAME = "worker-1"
DEFAULT_WORKER_ONCE = False


async def worker_loop(
    worker_name: str = DEFAULT_WORKER_NAME,
    *,
    poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    once: bool = DEFAULT_WORKER_ONCE,
) -> None:
    while True:
        async with AsyncSessionLocal() as session:
            task = await claim_next_task(session, worker_name=worker_name)

        if task is None:
            if once:
                logger.info("No pending tasks, worker exiting because --once was set")
                return

            logger.info("No pending tasks, sleeping...")
            await asyncio.sleep(poll_interval_seconds)
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

        if once:
            logger.info("Worker once mode processed one task and is exiting")
            return


def run(argv: Sequence[str] | None = None) -> None:
    configure_windows_event_loop_policy()
    logging.basicConfig(level=logging.INFO)
    options = _parse_worker_options(argv)
    logger.info("Starting Agent_Sam worker")
    logger.info("Worker ready")
    logger.info("Polling task queue every %s seconds", _format_poll_interval(options.poll_interval_seconds))
    logger.info("Press Ctrl+C to stop")
    try:
        asyncio.run(
            worker_loop(
                worker_name=options.worker_name,
                poll_interval_seconds=options.poll_interval_seconds,
                once=options.once,
            )
        )
    except KeyboardInterrupt:
        logger.info("Worker interrupted")
    finally:
        logger.info("Worker stopped")


def _parse_worker_options(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Agent_Sam background worker.")
    parser.add_argument("--once", action="store_true", default=None)
    parser.add_argument("--poll-interval-seconds", type=float, default=None)
    parser.add_argument("--worker-name", default=None)
    options = parser.parse_args(argv)

    return argparse.Namespace(
        once=_resolve_once_value(options.once),
        poll_interval_seconds=_resolve_poll_interval(options.poll_interval_seconds),
        worker_name=options.worker_name or os.getenv("WORKER_NAME", DEFAULT_WORKER_NAME),
    )


def _resolve_once_value(cli_value: bool | None) -> bool:
    if cli_value is not None:
        return cli_value

    raw_value = os.getenv("WORKER_ONCE", str(DEFAULT_WORKER_ONCE)).strip().lower()
    return raw_value in {"1", "true", "yes", "on"}


def _resolve_poll_interval(cli_value: float | None) -> float:
    if cli_value is not None:
        return cli_value

    raw_value = os.getenv("WORKER_POLL_INTERVAL_SECONDS", str(POLL_INTERVAL_SECONDS)).strip()
    try:
        return float(raw_value)
    except ValueError:
        logger.warning(
            "Invalid WORKER_POLL_INTERVAL_SECONDS value '%s'; falling back to %s seconds",
            raw_value,
            POLL_INTERVAL_SECONDS,
        )
        return float(POLL_INTERVAL_SECONDS)


def _format_poll_interval(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return str(value)


if __name__ == "__main__":
    run()
