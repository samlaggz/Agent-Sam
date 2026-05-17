from __future__ import annotations

import html
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from typing import Protocol
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.models import ToolCall


DEFAULT_WEB_SEARCH_URL = "https://html.duckduckgo.com/html/"
DEFAULT_WEB_USER_AGENT = "Agent-Sam/0.1 (+https://github.com/samlaggz/Agent-Sam)"
DEFAULT_WEB_TIMEOUT_SECONDS = 20.0
DEFAULT_WEB_MAX_OPEN_CHARS = 8000
_RESULT_LINK_RE = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class WebSource:
    title: str
    url: str
    snippet: str


@dataclass(frozen=True)
class WebSearchResponse:
    query: str
    summary: str
    sources: tuple[WebSource, ...]
    tool_call_id: UUID | None = None


@dataclass(frozen=True)
class WebOpenResponse:
    url: str
    content: str
    tool_call_id: UUID | None = None


class WebResearchProvider(Protocol):
    async def search(self, query: str, *, max_results: int = 5) -> tuple[WebSource, ...]:
        ...

    async def open(self, url: str) -> str:
        ...


class NullWebResearchProvider:
    async def search(self, query: str, *, max_results: int = 5) -> tuple[WebSource, ...]:
        del max_results
        return (WebSource(title="No provider configured", url="", snippet=f"No results for {query}"),)

    async def open(self, url: str) -> str:
        return f"No provider configured for {url}"


class HttpWebResearchProvider:
    def __init__(
        self,
        *,
        search_url: str = DEFAULT_WEB_SEARCH_URL,
        user_agent: str = DEFAULT_WEB_USER_AGENT,
        timeout_seconds: float = DEFAULT_WEB_TIMEOUT_SECONDS,
        max_open_chars: int = DEFAULT_WEB_MAX_OPEN_CHARS,
    ) -> None:
        self._search_url = search_url
        self._headers = {"User-Agent": user_agent}
        self._timeout_seconds = timeout_seconds
        self._max_open_chars = max_open_chars

    async def search(self, query: str, *, max_results: int = 5) -> tuple[WebSource, ...]:
        async with httpx.AsyncClient(
            headers=self._headers,
            timeout=self._timeout_seconds,
            follow_redirects=True,
        ) as client:
            response = await client.get(self._search_url, params={"q": query})
            response.raise_for_status()

        sources: list[WebSource] = []
        html_text = response.text
        for match in _RESULT_LINK_RE.finditer(html_text):
            title = _clean_text(match.group("title"))
            url = _normalize_result_url(match.group("href"))
            if not title or not url:
                continue
            snippet = _extract_snippet(html_text[match.end() : match.end() + 1200])
            sources.append(WebSource(title=title, url=url, snippet=snippet))
            if len(sources) >= max_results:
                break

        if not sources:
            return (
                WebSource(
                    title=f"No search results found for {query}",
                    url=self._search_url,
                    snippet="The search provider returned no parseable public results.",
                ),
            )
        return tuple(sources)

    async def open(self, url: str) -> str:
        async with httpx.AsyncClient(
            headers=self._headers,
            timeout=self._timeout_seconds,
            follow_redirects=True,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()

        content_type = response.headers.get("content-type", "")
        text = response.text
        if "html" in content_type.lower():
            text = _html_to_text(text)
        text = _clean_text(text)
        if len(text) > self._max_open_chars:
            return text[: self._max_open_chars].rstrip() + "..."
        return text


class WebResearchTool:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], provider: WebResearchProvider | None = None) -> None:
        self._session_factory = session_factory
        self._provider = provider or NullWebResearchProvider()

    async def search(
        self,
        *,
        task_id: UUID | None,
        task_run_id: UUID | None,
        query: str,
        allow_private: bool = False,
        max_results: int = 5,
    ) -> WebSearchResponse:
        if not allow_private and any(token in query.lower() for token in ("secret", "internal", "private", "customer data")):
            raise ValueError("Web research is blocked for high-risk private data queries unless explicitly allowed.")

        start = time.perf_counter()
        sources = await self._provider.search(query, max_results=max_results)
        duration_ms = max(1, int((time.perf_counter() - start) * 1000))
        summary = summarize_sources(query, sources)
        tool_call_id = await self._record_tool_call(
            task_id=task_id,
            task_run_id=task_run_id,
            tool_name="web_search",
            input_payload={"query": query, "max_results": max_results},
            output_text=summary,
            metadata={"sources": [source.__dict__ for source in sources]},
            duration_ms=duration_ms,
        )
        return WebSearchResponse(query=query, summary=summary, sources=tuple(sources), tool_call_id=tool_call_id)

    async def open(self, *, task_id: UUID | None, task_run_id: UUID | None, url: str) -> WebOpenResponse:
        start = time.perf_counter()
        content = await self._provider.open(url)
        duration_ms = max(1, int((time.perf_counter() - start) * 1000))
        tool_call_id = await self._record_tool_call(
            task_id=task_id,
            task_run_id=task_run_id,
            tool_name="web_open",
            input_payload={"url": url},
            output_text=content[:4000],
            metadata={"url": url},
            duration_ms=duration_ms,
        )
        return WebOpenResponse(url=url, content=content, tool_call_id=tool_call_id)

    async def _record_tool_call(
        self,
        *,
        task_id: UUID | None,
        task_run_id: UUID | None,
        tool_name: str,
        input_payload: dict,
        output_text: str,
        metadata: dict,
        duration_ms: int,
    ) -> UUID:
        async with self._session_factory() as session:
            tool_call = ToolCall(
                task_id=task_id,
                task_run_id=task_run_id,
                tool_name=tool_name,
                input_payload=json.loads(json.dumps(input_payload)),
                output_text=output_text,
                stderr_text=None,
                duration_ms=duration_ms,
                status="completed",
                risk_level="safe",
                approved_by_user=False,
            )
            session.add(tool_call)
            await session.commit()
            await session.refresh(tool_call)
            return tool_call.id


def summarize_sources(query: str, sources: tuple[WebSource, ...]) -> str:
    source_lines = [f"- {source.title} ({source.url})".strip() for source in sources if source.title or source.url]
    if not source_lines:
        return f"No sources found for {query}."
    return f"Research summary for {query}:\n" + "\n".join(source_lines)


def _normalize_result_url(raw_url: str) -> str:
    resolved = html.unescape(raw_url).strip()
    if not resolved:
        return ""
    if resolved.startswith("/"):
        resolved = urljoin(DEFAULT_WEB_SEARCH_URL, resolved)
    parsed = urlparse(resolved)
    if parsed.netloc.endswith("duckduckgo.com"):
        uddg = parse_qs(parsed.query).get("uddg")
        if uddg:
            return unquote(uddg[0])
    return resolved


def _extract_snippet(fragment: str) -> str:
    cleaned = _clean_text(fragment)
    if not cleaned:
        return ""
    return cleaned[:240]


def _html_to_text(raw_html: str) -> str:
    stripped = _SCRIPT_STYLE_RE.sub(" ", raw_html)
    stripped = _TAG_RE.sub(" ", stripped)
    return _clean_text(stripped)


def _clean_text(value: str) -> str:
    normalized = html.unescape(value)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()