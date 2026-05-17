from __future__ import annotations

import logging

from gateways.base import Gateway, GatewayHealth, OutgoingGatewayMessage


logger = logging.getLogger(__name__)


class WebhookGateway(Gateway):
    name = "webhook"

    def __init__(self) -> None:
        self._started = False

    async def start(self) -> None:
        self._started = True
        logger.info("Webhook gateway ready")
        logger.info("Listening for authenticated webhook messages via the API process")

    async def stop(self) -> None:
        if self._started:
            logger.info("Webhook gateway stopped")
        self._started = False

    async def send_message(self, message: OutgoingGatewayMessage) -> None:
        del message
        raise RuntimeError("Webhook outbound messaging is not implemented yet.")

    async def health_check(self) -> GatewayHealth:
        return GatewayHealth(
            status="ready" if self._started else "stopped",
            detail="Inbound requests are served by the API webhook route.",
        )