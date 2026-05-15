from __future__ import annotations

from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from db.models import Message, Task


class ProgressReporter(Protocol):
    async def report(self, task_id: UUID, text: str, *, stage: str) -> None:
        ...


class TelegramProgressAdapter:
    def __init__(self, settings: Settings) -> None:
        self._token = settings.telegram_bot_token

    async def send(self, task: Task, text: str) -> None:
        if not self._token:
            return

        metadata = task.metadata_json if isinstance(task.metadata_json, dict) else {}
        if metadata.get("source") != "telegram":
            return

        chat_id = metadata.get("source_chat_id")
        if not chat_id:
            return

        try:
            from telegram import Bot

            bot = Bot(token=self._token)
            await bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            return


class TaskProgressReporter:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self._session_factory = session_factory
        self._adapters = {"telegram": TelegramProgressAdapter(settings)}

    async def report(self, task_id: UUID, text: str, *, stage: str) -> None:
        async with self._session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return

            metadata = task.metadata_json if isinstance(task.metadata_json, dict) else {}
            transport = metadata.get("source")
            session.add(
                Message(
                    workspace_id=task.workspace_id,
                    user_id=task.created_by_user_id,
                    task_id=task.id,
                    role="assistant",
                    content=text,
                    metadata_json={
                        "source": "agent",
                        "stage": stage,
                        "transport": transport,
                    },
                )
            )
            await session.commit()

        adapter = self._adapters.get(str(transport)) if transport is not None else None
        if adapter is not None:
            await adapter.send(task, text)