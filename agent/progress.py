from __future__ import annotations

import re
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

    async def send(self, task: Task, text: str, *, stage: str) -> None:
        if not self._token:
            return

        metadata = task.metadata_json if isinstance(task.metadata_json, dict) else {}
        if metadata.get("source") != "telegram":
            return

        if stage not in {"report_result", "approval_required"}:
            return

        chat_id = metadata.get("source_chat_id")
        if not chat_id:
            return

        try:
            from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

            bot = Bot(token=self._token)
            reply_markup = None
            approval_id = _extract_approval_id(text)
            if approval_id is not None and stage == "approval_required":
                reply_markup = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Approve", callback_data=f"approve:{approval_id}")]]
                )
            await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
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
                        "source_chat_id": metadata.get("source_chat_id"),
                    },
                )
            )
            await session.commit()

        adapter = self._adapters.get(str(transport)) if transport is not None else None
        if adapter is not None:
            await adapter.send(task, text, stage=stage)


def _extract_approval_id(text: str) -> str | None:
    match = re.search(r"Approval ID:\s*([0-9a-fA-F-]{36})", text)
    if match is None:
        return None
    return match.group(1)