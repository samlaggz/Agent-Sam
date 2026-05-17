from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from uuid import UUID

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Conflict, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from gateways.base import Gateway, GatewayChat, GatewayHealth, GatewayResponse, GatewayUser, IncomingGatewayMessage, OutgoingGatewayMessage
from gateways.service import AgentGatewayService


logger = logging.getLogger(__name__)

SUPPORTED_COMMANDS = (
    "start",
    "help",
    "agents",
    "agent",
    "route",
    "models",
    "budget",
    "skills",
    "subagents",
    "status",
    "queue",
    "new",
    "prioritize",
    "pause",
    "resume",
    "approve",
    "cancel",
)


class TelegramGateway(Gateway):
    name = "telegram"

    def __init__(
        self,
        *,
        service: AgentGatewayService,
        token: str,
        workspace_id: UUID,
        user_id: UUID,
        debug_logging: bool = False,
    ) -> None:
        self._service = service
        self._workspace_id = workspace_id
        self._user_id = user_id
        self._debug_logging = debug_logging
        self._application = Application.builder().token(token).build()
        self._started = False
        self._polling_stop_requested = False
        self._register_handlers()

    async def start(self) -> None:
        logger.info("Telegram gateway starting")
        await self._application.initialize()
        await self._application.start()
        if self._application.updater is None:
            raise RuntimeError("Telegram gateway could not start polling because the updater is unavailable.")
        await self._application.updater.start_polling(error_callback=self._handle_polling_error)
        self._started = True
        logger.info("Telegram gateway ready")
        logger.info("Listening for Telegram messages")

    async def stop(self) -> None:
        if not self._started:
            return

        logger.info("Telegram gateway stopping")
        if self._application.updater is not None:
            with suppress(asyncio.CancelledError):
                await self._application.updater.stop()
        with suppress(asyncio.CancelledError):
            await self._application.stop()
        with suppress(asyncio.CancelledError):
            await self._application.shutdown()
        self._started = False
        self._polling_stop_requested = False
        logger.info("Telegram gateway stopped")

    async def send_message(self, message: OutgoingGatewayMessage) -> None:
        if not message.gateway_chat_id:
            raise ValueError("Telegram messages require gateway_chat_id.")
        await self._application.bot.send_message(
            chat_id=message.gateway_chat_id,
            text=message.text,
            reply_to_message_id=message.reply_to_message_id,
        )

    async def health_check(self) -> GatewayHealth:
        return GatewayHealth(status="ready" if self._started else "stopped")

    def _handle_polling_error(self, error: TelegramError) -> None:
        if isinstance(error, Conflict):
            if self._polling_stop_requested:
                return
            self._polling_stop_requested = True
            logger.error(
                "Telegram polling conflict: another bot instance is already using this token. Stop the other instance before starting this gateway."
            )
            asyncio.get_running_loop().create_task(self.stop())
            return

        logger.error("Telegram polling failed: %s", error)

    def _register_handlers(self) -> None:
        for command_name in SUPPORTED_COMMANDS:
            self._application.add_handler(CommandHandler(command_name, self._handle_update))
        self._application.add_handler(CallbackQueryHandler(self._handle_callback_query, pattern=r"^approve:"))
        self._application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_update))

    async def _handle_update(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context

        incoming = self._build_incoming_message(update)
        message = update.effective_message
        if incoming is None or message is None:
            return

        logger.info(
            "Telegram message received from chat %s user %s",
            incoming.gateway_chat.gateway_chat_id,
            incoming.gateway_user.gateway_user_id,
        )
        if self._debug_logging:
            logger.debug("Telegram message text: %s", incoming.text)

        try:
            response = await self._service.handle_incoming_message(incoming)
        except Exception:
            logger.exception("Telegram gateway request failed")
            response = GatewayResponse(
                text="Request failed. Check gateway configuration and database connectivity.",
            )

        if response.should_reply:
            reply_markup = self._build_reply_markup(response)
            await message.reply_text(response.text, reply_markup=reply_markup)

    async def _handle_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        query = update.callback_query
        if query is None or update.effective_chat is None or update.effective_user is None:
            return

        data = query.data or ""
        if not data.startswith("approve:"):
            return

        approval_id = data.split(":", 1)[1].strip()
        incoming = IncomingGatewayMessage(
            workspace_id=self._workspace_id,
            user_id=self._user_id,
            gateway_name=self.name,
            gateway_user=GatewayUser(
                gateway_user_id=str(update.effective_user.id),
                username=update.effective_user.username,
                display_name=update.effective_user.full_name,
            ),
            gateway_chat=GatewayChat(
                gateway_chat_id=str(update.effective_chat.id),
                title=getattr(update.effective_chat, "title", None),
                chat_type=getattr(update.effective_chat, "type", None),
            ),
            text=f"/approve {approval_id}",
            gateway_message_id=str(query.message.message_id) if query.message is not None else None,
            raw_payload={"callback_query": True, "callback_data": data},
        )

        try:
            response = await self._service.handle_incoming_message(incoming)
        except Exception:
            logger.exception("Telegram gateway callback request failed")
            await query.answer("Approval failed", show_alert=True)
            return

        await query.answer("Approved")
        if query.message is not None:
            with suppress(TelegramError):
                await query.edit_message_reply_markup(reply_markup=None)
        if response.should_reply:
            reply_markup = self._build_reply_markup(response)
            await query.message.reply_text(response.text, reply_markup=reply_markup)

    def _build_incoming_message(self, update: Update) -> IncomingGatewayMessage | None:
        message = update.effective_message
        if message is None or message.text is None or update.effective_chat is None or update.effective_user is None:
            return None

        return IncomingGatewayMessage(
            workspace_id=self._workspace_id,
            user_id=self._user_id,
            gateway_name=self.name,
            gateway_user=GatewayUser(
                gateway_user_id=str(update.effective_user.id),
                username=update.effective_user.username,
                display_name=update.effective_user.full_name,
            ),
            gateway_chat=GatewayChat(
                gateway_chat_id=str(update.effective_chat.id),
                title=getattr(update.effective_chat, "title", None),
                chat_type=getattr(update.effective_chat, "type", None),
            ),
            text=message.text,
            gateway_message_id=str(message.message_id),
        )

    def _build_reply_markup(self, response: GatewayResponse) -> InlineKeyboardMarkup | None:
        approval_id = response.metadata.get("approval_id") if isinstance(response.metadata, dict) else None
        if approval_id is None:
            approval_id = str(response.approval_id) if response.approval_id is not None else None
        if not approval_id:
            return None
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("Approve", callback_data=f"approve:{approval_id}")]]
        )
