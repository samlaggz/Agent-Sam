from __future__ import annotations

from uuid import uuid4

import pytest

from db.repositories import export_events_jsonl
from harness.events import EventStore, ToolCallEvent


@pytest.mark.asyncio
async def test_event_store_appends_lists_and_exports(session_factory) -> None:
    store = EventStore(session_factory)
    task_id = uuid4()
    run_id = uuid4()

    appended = await store.append(
        ToolCallEvent(
            task_id=task_id,
            run_id=run_id,
            agent_slug="coding_agent",
            content="token=shhh",
            metadata={"secret": "password=hunter2"},
        )
    )

    assert appended.sequence == 1
    events = await store.list_events(task_id=task_id, run_id=run_id)
    assert len(events) == 1
    assert "[REDACTED]" in events[0].content

    exported = await store.export_jsonl(task_id=task_id, run_id=run_id)
    assert "tool_call" in exported
    assert "[REDACTED]" in exported


@pytest.mark.asyncio
async def test_export_events_jsonl_repository_returns_ordered_lines(session, workspace) -> None:
    from db.repositories import append_event

    task_id = uuid4()
    await append_event(session, task_id=task_id, agent_run_id=None, agent_slug="a", event_type="one", content="1")
    await append_event(session, task_id=task_id, agent_run_id=None, agent_slug="a", event_type="two", content="2")

    exported = await export_events_jsonl(session, task_id=task_id)
    lines = [line for line in exported.splitlines() if line]
    assert len(lines) == 2
    assert '"sequence": 1' in lines[0]
    assert '"sequence": 2' in lines[1]