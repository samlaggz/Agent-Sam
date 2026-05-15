import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from app.config import Settings
from db.session import AsyncSessionLocal
from gateways.service import AgentGatewayService, IncomingGatewayMessage


logger = logging.getLogger(__name__)


def _get_gateway_service(context: ContextTypes.DEFAULT_TYPE) -> AgentGatewayService:
    return context.application.bot_data["gateway_service"]


def _build_incoming_message(update: Update) -> IncomingGatewayMessage | None:
    message = update.effective_message
    if message is None or message.text is None:
        return None

    return IncomingGatewayMessage(
        text=message.text,
        source="telegram",
        source_user_id=str(update.effective_user.id) if update.effective_user is not None else None,
        source_chat_id=str(update.effective_chat.id) if update.effective_chat is not None else None,
        source_message_id=str(message.message_id),
        source_username=update.effective_user.username if update.effective_user is not None else None,
    )


async def _reply_with_gateway_response(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    command: str | None = None,
) -> None:
    message = update.effective_message
    incoming = _build_incoming_message(update)
    if message is None or incoming is None:
        return

    service = _get_gateway_service(context)

    try:
        if command is None:
            response_text = await service.handle_text_message(incoming)
        else:
            response_text = await service.handle_command(command, incoming, list(context.args))
    except Exception:
        logger.exception("Telegram gateway request failed")
        response_text = "Request failed. Check gateway configuration and database connectivity."

    await message.reply_text(response_text)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_with_gateway_response(update, context, command="start")


async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_with_gateway_response(update, context, command="new")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_with_gateway_response(update, context, command="status")


async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_with_gateway_response(update, context, command="queue")


async def prioritize_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_with_gateway_response(update, context, command="prioritize")


async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_with_gateway_response(update, context, command="pause")


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_with_gateway_response(update, context, command="resume")


async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_with_gateway_response(update, context, command="approve")


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_with_gateway_response(update, context, command="cancel")


async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_with_gateway_response(update, context)


def build_application(settings: Settings) -> Application:
    application = Application.builder().token(settings.telegram_bot_token).build()
    application.bot_data["gateway_service"] = AgentGatewayService(settings, AsyncSessionLocal)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("new", new_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("queue", queue_command))
    application.add_handler(CommandHandler("prioritize", prioritize_command))
    application.add_handler(CommandHandler("pause", pause_command))
    application.add_handler(CommandHandler("resume", resume_command))
    application.add_handler(CommandHandler("approve", approve_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))
    return application
