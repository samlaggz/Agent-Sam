from __future__ import annotations

import logging

from gateways.base import Gateway, GatewayHealth, OutgoingGatewayMessage


logger = logging.getLogger(__name__)


class WhatsAppGateway(Gateway):
    name = "whatsapp"

    def __init__(self, *, provider: str) -> None:
        self._provider = provider

    async def start(self) -> None:
        logger.error(
            "WhatsApp gateway is configured for provider '%s' but the adapter is still a placeholder. "
            "TODO: implement inbound webhook handling and outbound send support.",
            self._provider,
        )
        raise RuntimeError("WhatsApp gateway is not implemented yet.")

    async def stop(self) -> None:
        logger.info("WhatsApp gateway stopped")

    async def send_message(self, message: OutgoingGatewayMessage) -> None:
        del message
        raise RuntimeError("WhatsApp outbound messaging is not implemented yet.")

    async def health_check(self) -> GatewayHealth:
        return GatewayHealth(status="not_implemented", detail=f"Placeholder for {self._provider} integration")