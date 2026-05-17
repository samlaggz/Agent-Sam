from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.config import Settings
from gateways.base import GatewayChat, GatewayResponse, GatewayUser, IncomingGatewayMessage
from gateways.registry import require_gateway_context
from gateways.service import AgentGatewayService


logger = logging.getLogger(__name__)


class WebhookPayload(BaseModel):
    gateway_user_id: str = Field(..., min_length=1)
    gateway_chat_id: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)


class WebhookResponse(BaseModel):
    text: str
    task_id: str | None = None
    approval_id: str | None = None
    should_reply: bool = True


def build_webhook_router(settings: Settings, service: AgentGatewayService) -> APIRouter:
    if not settings.webhook_gateway_secret.strip():
        raise RuntimeError("WEBHOOK_GATEWAY_SECRET must be set before enabling the webhook gateway.")

    workspace_id, user_id = require_gateway_context(settings, "webhook")
    router = APIRouter()

    @router.post("/webhook", response_model=WebhookResponse)
    async def receive_webhook(
        payload: WebhookPayload,
        x_gateway_secret: str | None = Header(default=None, alias="X-Gateway-Secret"),
    ) -> WebhookResponse:
        if x_gateway_secret != settings.webhook_gateway_secret:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid gateway secret.")

        logger.info(
            "Webhook message received from chat %s user %s",
            payload.gateway_chat_id,
            payload.gateway_user_id,
        )
        if settings.gateway_debug_logging:
            logger.debug("Webhook message text: %s", payload.text)

        incoming = IncomingGatewayMessage(
            workspace_id=workspace_id,
            user_id=user_id,
            gateway_name="webhook",
            gateway_user=GatewayUser(gateway_user_id=payload.gateway_user_id),
            gateway_chat=GatewayChat(gateway_chat_id=payload.gateway_chat_id),
            text=payload.text,
            raw_payload=payload.model_dump(),
        )
        response = await service.handle_incoming_message(incoming)
        return _serialize_response(response)

    return router


def _serialize_response(response: GatewayResponse) -> WebhookResponse:
    return WebhookResponse(
        text=response.text,
        task_id=_serialize_optional_uuid(response.task_id),
        approval_id=_serialize_optional_uuid(response.approval_id),
        should_reply=response.should_reply,
    )


def _serialize_optional_uuid(value: UUID | None) -> str | None:
    if value is None:
        return None
    return str(value)