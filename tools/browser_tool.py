"""
Browser automation tool using Playwright (Hermes-style).

Provides browser_navigate, browser_snapshot, browser_click, browser_type,
browser_press, browser_scroll, browser_back — all using the accessibility
tree so the LLM can see element refs like @e1, @e5 and interact with them.

Usage:
    tool = BrowserTool(session_factory)
    result = await tool.navigate(task_id=..., url="https://example.com")
    result = await tool.snapshot(task_id=...)
    result = await tool.click(task_id=..., ref="@e5")
    result = await tool.type_text(task_id=..., ref="@e3", text="hello")
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

# Lazy import — Playwright may not be installed
_playwright_available: bool | None = None
_browser_instance = None
_browser_context = None
_active_pages: dict[str, Any] = {}  # task_id -> page


def _check_playwright() -> bool:
    global _playwright_available
    if _playwright_available is not None:
        return _playwright_available
    try:
        from playwright.async_api import async_playwright  # noqa: F401
        _playwright_available = True
    except ImportError:
        _playwright_available = False
    return _playwright_available


@dataclass(frozen=True)
class BrowserResult:
    success: bool
    data: dict[str, Any]
    error: str | None = None

    def to_json(self) -> str:
        result = {"success": self.success, **self.data}
        if self.error:
            result["error"] = self.error
        return json.dumps(result, ensure_ascii=False, default=str)


class BrowserTool:
    """Hermes-style browser automation using Playwright."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        headless: bool = True,
        timeout_ms: int = 30000,
    ) -> None:
        self._session_factory = session_factory
        self._headless = headless
        self._timeout_ms = timeout_ms
        self._playwright = None
        self._browser = None
        self._contexts: dict[str, Any] = {}  # task_id -> context
        self._pages: dict[str, Any] = {}  # task_id -> page

    async def _ensure_browser(self) -> None:
        """Lazy-init the browser on first use."""
        if self._browser is not None:
            return

        if not _check_playwright():
            raise RuntimeError(
                "Playwright is not installed. Install with:\n"
                "  pip install playwright && python -m playwright install chromium"
            )

        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self._headless,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )

    async def _get_page(self, task_id: str) -> Any:
        """Get or create a page for the given task."""
        if task_id in self._pages:
            return self._pages[task_id]

        await self._ensure_browser()
        context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        self._contexts[task_id] = context
        self._pages[task_id] = page
        return page

    async def navigate(self, *, task_id: str, url: str) -> BrowserResult:
        """Navigate to a URL and return a snapshot."""
        try:
            page = await self._get_page(task_id)
            await page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
            await page.wait_for_timeout(1000)  # Let JS settle

            title = await page.title()
            current_url = page.url

            # Auto-snapshot after navigation
            snapshot = await self._build_snapshot(page)

            return BrowserResult(
                success=True,
                data={
                    "url": current_url,
                    "title": title,
                    "snapshot": snapshot,
                },
            )
        except Exception as e:
            return BrowserResult(success=False, data={}, error=str(e))

    async def snapshot(self, *, task_id: str, full: bool = False) -> BrowserResult:
        """Get an accessibility tree snapshot of the current page."""
        try:
            page = self._pages.get(task_id)
            if page is None:
                return BrowserResult(
                    success=False, data={},
                    error="No browser session. Call browser_navigate first.",
                )

            snapshot = await self._build_snapshot(page, full=full)
            return BrowserResult(success=True, data={"snapshot": snapshot})
        except Exception as e:
            return BrowserResult(success=False, data={}, error=str(e))

    async def click(self, *, task_id: str, ref: str) -> BrowserResult:
        """Click an element by its ref ID (e.g., @e5)."""
        try:
            page = self._pages.get(task_id)
            if page is None:
                return BrowserResult(
                    success=False, data={},
                    error="No browser session. Call browser_navigate first.",
                )

            selector = self._ref_to_selector(ref)
            await page.click(selector, timeout=self._timeout_ms)
            await page.wait_for_timeout(500)

            return BrowserResult(success=True, data={"clicked": ref, "url": page.url})
        except Exception as e:
            return BrowserResult(success=False, data={}, error=f"Click failed on {ref}: {e}")

    async def type_text(self, *, task_id: str, ref: str, text: str) -> BrowserResult:
        """Type text into an input field by ref ID."""
        try:
            page = self._pages.get(task_id)
            if page is None:
                return BrowserResult(
                    success=False, data={},
                    error="No browser session. Call browser_navigate first.",
                )

            selector = self._ref_to_selector(ref)
            # Clear then type
            await page.click(selector, timeout=self._timeout_ms)
            await page.fill(selector, text, timeout=self._timeout_ms)

            return BrowserResult(success=True, data={"typed": text[:50], "ref": ref})
        except Exception as e:
            return BrowserResult(success=False, data={}, error=f"Type failed on {ref}: {e}")

    async def press_key(self, *, task_id: str, key: str) -> BrowserResult:
        """Press a keyboard key (Enter, Tab, Escape, etc.)."""
        try:
            page = self._pages.get(task_id)
            if page is None:
                return BrowserResult(
                    success=False, data={},
                    error="No browser session. Call browser_navigate first.",
                )

            await page.keyboard.press(key)
            await page.wait_for_timeout(500)
            return BrowserResult(success=True, data={"pressed": key, "url": page.url})
        except Exception as e:
            return BrowserResult(success=False, data={}, error=f"Press failed: {e}")

    async def scroll(self, *, task_id: str, direction: str = "down") -> BrowserResult:
        """Scroll the page up or down."""
        try:
            page = self._pages.get(task_id)
            if page is None:
                return BrowserResult(
                    success=False, data={},
                    error="No browser session. Call browser_navigate first.",
                )

            pixels = 600 if direction == "down" else -600
            await page.evaluate(f"window.scrollBy(0, {pixels})")
            await page.wait_for_timeout(300)
            return BrowserResult(success=True, data={"scrolled": direction})
        except Exception as e:
            return BrowserResult(success=False, data={}, error=f"Scroll failed: {e}")

    async def go_back(self, *, task_id: str) -> BrowserResult:
        """Navigate back in browser history."""
        try:
            page = self._pages.get(task_id)
            if page is None:
                return BrowserResult(
                    success=False, data={},
                    error="No browser session. Call browser_navigate first.",
                )

            await page.go_back(timeout=self._timeout_ms)
            await page.wait_for_timeout(500)
            return BrowserResult(
                success=True,
                data={"url": page.url, "title": await page.title()},
            )
        except Exception as e:
            return BrowserResult(success=False, data={}, error=f"Back failed: {e}")

    async def close_session(self, task_id: str) -> None:
        """Close the browser session for a task."""
        context = self._contexts.pop(task_id, None)
        self._pages.pop(task_id, None)
        if context:
            try:
                await context.close()
            except Exception:
                pass

    async def close_all(self) -> None:
        """Shut down the browser entirely."""
        for task_id in list(self._pages.keys()):
            await self.close_session(task_id)
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    async def close(self) -> None:
        """Compatibility alias for callers expecting a single close() method."""
        await self.close_all()

    # ── Snapshot building ─────────────────────────────────────────────

    async def _build_snapshot(self, page: Any, *, full: bool = False) -> str:
        """
        Build a Hermes-style accessibility tree snapshot.
        Interactive elements get ref IDs like [@e1], [@e2] that can be
        used with browser_click and browser_type.
        """
        try:
            # Use Playwright's accessibility tree
            snapshot = await page.accessibility.snapshot()
            if not snapshot:
                # Fallback: get page text content
                text = await page.inner_text("body")
                return text[:8000] if not full else text[:16000]

            lines: list[str] = []
            ref_counter = [0]
            self._walk_tree(snapshot, lines, ref_counter, depth=0, full=full)

            result = "\n".join(lines)
            max_chars = 16000 if full else 8000
            if len(result) > max_chars:
                result = result[:max_chars] + "\n... (truncated)"
            return result
        except Exception as e:
            # Fallback: try getting text content
            try:
                text = await page.inner_text("body")
                return text[:8000]
            except Exception:
                return f"(snapshot failed: {e})"

    def _walk_tree(
        self,
        node: dict[str, Any],
        lines: list[str],
        ref_counter: list[int],
        depth: int,
        full: bool,
    ) -> None:
        """Recursively walk the accessibility tree, assigning refs to interactive elements."""
        role = node.get("role", "")
        name = node.get("name", "").strip()
        value = node.get("value", "")

        # Interactive roles get refs
        interactive_roles = {
            "link", "button", "textbox", "checkbox", "radio",
            "combobox", "menuitem", "tab", "switch", "searchbox",
            "spinbutton", "slider", "option", "menuitemcheckbox",
            "menuitemradio", "treeitem",
        }

        indent = "  " * depth
        is_interactive = role in interactive_roles

        if is_interactive:
            ref_counter[0] += 1
            ref = f"@e{ref_counter[0]}"
            label = name or value or role
            if value and name:
                label = f"{name}: {value}"
            lines.append(f"{indent}[{ref}] {role}: {label}")
        elif full or role in ("heading", "text", "paragraph", "img", "navigation", "main", "banner"):
            if name:
                lines.append(f"{indent}{role}: {name}")

        # Walk children
        for child in node.get("children", []):
            self._walk_tree(child, lines, ref_counter, depth + 1, full)

    def _ref_to_selector(self, ref: str) -> str:
        """
        Convert a ref like @e5 to a Playwright selector.
        We use the accessibility tree index — click the Nth interactive element.
        """
        clean = ref.lstrip("@")
        match = re.match(r"e(\d+)", clean)
        if not match:
            raise ValueError(f"Invalid ref: {ref}. Expected format like @e5")

        index = int(match.group(1))
        # We use a JS-based selector that finds the Nth interactive element
        return (
            f">> nth={index - 1} >> visible=true >> "
            f"role=link,button,textbox,checkbox,radio,combobox,menuitem,tab,switch,searchbox"
        )


def check_browser_available() -> tuple[bool, str]:
    """Check if browser tools can be used."""
    if not _check_playwright():
        return False, (
            "Playwright is not installed. Install with:\n"
            "  pip install playwright && python -m playwright install chromium"
        )
    return True, "Browser tools available."
