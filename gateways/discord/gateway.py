from __future__ import annotations

import logging

from gateways.base import Gateway, GatewayHealth, OutgoingGatewayMessage


logger = logging.getLogger(__name__)


class DiscordGateway(Gateway):
    name = "discord"

    async def start(self) -> None:
        logger.error(
            "Discord gateway is only a placeholder. TODO: integrate discord.py and map Discord events into IncomingGatewayMessage."
        )
        raise RuntimeError("Discord gateway is not implemented yet.")

    async def stop(self) -> None:
        logger.info("Discord gateway stopped")

    async def send_message(self, message: OutgoingGatewayMessage) -> None:
        del message
        raise RuntimeError("Discord outbound messaging is not implemented yet.")

    async def health_check(self) -> GatewayHealth:
        return GatewayHealth(status="not_implemented", detail="Placeholder for discord.py integration")