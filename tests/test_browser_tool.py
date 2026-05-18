from __future__ import annotations

import pytest

from tools.browser_tool import BrowserTool


@pytest.mark.asyncio
async def test_browser_tool_close_delegates_to_close_all() -> None:
    tool = BrowserTool(session_factory=None)  # type: ignore[arg-type]
    called = False

    async def fake_close_all() -> None:
        nonlocal called
        called = True

    tool.close_all = fake_close_all  # type: ignore[method-assign]

    await tool.close()

    assert called is True