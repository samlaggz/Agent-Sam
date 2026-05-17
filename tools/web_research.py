from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.models import ToolCall


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
        await self._record_tool_call(
            task_id=task_id,
            task_run_id=task_run_id,
            tool_name="web_search",
            input_payload={"query": query, "max_results": max_results},
            output_text=summary,
            metadata={"sources": [source.__dict__ for source in sources]},
            duration_ms=duration_ms,
        )
        return WebSearchResponse(query=query, summary=summary, sources=tuple(sources))

    async def open(self, *, task_id: UUID | None, task_run_id: UUID | None, url: str) -> str:
        start = time.perf_counter()
        content = await self._provider.open(url)
        duration_ms = max(1, int((time.perf_counter() - start) * 1000))
        await self._record_tool_call(
            task_id=task_id,
            task_run_id=task_run_id,
            tool_name="web_open",
            input_payload={"url": url},
            output_text=content[:4000],
            metadata={"url": url},
            duration_ms=duration_ms,
        )
        return content

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
    ) -> None:
        async with self._session_factory() as session:
            session.add(
                ToolCall(
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
            )
            await session.commit()


def summarize_sources(query: str, sources: tuple[WebSource, ...]) -> str:
    source_lines = [f"- {source.title} ({source.url})".strip() for source in sources if source.title or source.url]
    if not source_lines:
        return f"No sources found for {query}."
    return f"Research summary for {query}:\n" + "\n".join(source_lines)