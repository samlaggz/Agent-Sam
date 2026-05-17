from __future__ import annotations

import logging

from gateways.base import Gateway, GatewayHealth, OutgoingGatewayMessage


logger = logging.getLogger(__name__)


class SlackGateway(Gateway):
    name = "slack"

    async def start(self) -> None:
        logger.error(
            "Slack gateway is only a placeholder. TODO: integrate Slack Bolt for inbound events and outbound replies."
        )
        raise RuntimeError("Slack gateway is not implemented yet.")

    async def stop(self) -> None:
        logger.info("Slack gateway stopped")

    async def send_message(self, message: OutgoingGatewayMessage) -> None:
        del message
        raise RuntimeError("Slack outbound messaging is not implemented yet.")

    async def health_check(self) -> GatewayHealth:
        return GatewayHealth(status="not_implemented", detail="Placeholder for Slack Bolt integration")