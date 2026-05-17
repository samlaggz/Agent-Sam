from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent.graph import AgentRunResult
from workers import main as worker_main


def _fake_session_factory():
    @asynccontextmanager
    async def _context_manager():
        yield object()

    return _context_manager()


@pytest.mark.asyncio
async def test_worker_once_exits_cleanly_when_no_tasks(monkeypatch, caplog: pytest.LogCaptureFixture) -> None:
    async def fake_claim_next_task(session, *, worker_name):
        return None

    async def fake_sleep(seconds: float) -> None:
        raise AssertionError("worker_loop should not sleep in once mode when no tasks are pending")

    monkeypatch.setattr(worker_main, "AsyncSessionLocal", lambda: _fake_session_factory())
    monkeypatch.setattr(worker_main, "claim_next_task", fake_claim_next_task)
    monkeypatch.setattr(worker_main.asyncio, "sleep", fake_sleep)
    caplog.set_level("INFO")

    await worker_main.worker_loop(once=True, poll_interval_seconds=0)

    assert "No pending tasks, worker exiting because --once was set" in caplog.text


@pytest.mark.asyncio
async def test_worker_once_processes_one_pending_task(monkeypatch) -> None:
    task = SimpleNamespace(id=uuid4(), priority="normal")
    completed_task_ids: list[object] = []

    async def fake_claim_next_task(session, *, worker_name):
        return task

    async def fake_handle_task(claimed_task) -> AgentRunResult:
        assert claimed_task is task
        return AgentRunResult(task_id=task.id, status="completed", final_summary="done")

    async def fake_complete_task(session, *, task_id):
        completed_task_ids.append(task_id)

    async def fake_fail_task(session, *, task_id, error_text=None):
        raise AssertionError("fail_task should not be called for a completed worker result")

    async def fake_pause_task(session, *, task_id):
        raise AssertionError("pause_task should not be called for a completed worker result")

    monkeypatch.setattr(worker_main, "AsyncSessionLocal", lambda: _fake_session_factory())
    monkeypatch.setattr(worker_main, "claim_next_task", fake_claim_next_task)
    monkeypatch.setattr(worker_main, "handle_task", fake_handle_task)
    monkeypatch.setattr(worker_main, "complete_task", fake_complete_task)
    monkeypatch.setattr(worker_main, "fail_task", fake_fail_task)
    monkeypatch.setattr(worker_main, "pause_task", fake_pause_task)

    await worker_main.worker_loop(once=True, poll_interval_seconds=0)

    assert completed_task_ids == [task.id]


def test_worker_run_handles_keyboard_interrupt_cleanly(monkeypatch, caplog: pytest.LogCaptureFixture) -> None:
    def fake_asyncio_run(coro):
        coro.close()
        raise KeyboardInterrupt()

    monkeypatch.setattr(worker_main, "configure_windows_event_loop_policy", lambda: None)
    monkeypatch.setattr(worker_main.asyncio, "run", fake_asyncio_run)
    caplog.set_level("INFO")

    worker_main.run(["--once"])

    assert "Starting Agent_Sam worker" in caplog.text
    assert "Worker ready" in caplog.text
    assert "Polling task queue every 5 seconds" in caplog.text
    assert "Press Ctrl+C to stop" in caplog.text
    assert "Worker interrupted" in caplog.text
    assert "Worker stopped" in caplog.text