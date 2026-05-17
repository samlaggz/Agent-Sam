from __future__ import annotations

import asyncio
import getpass
import logging
from contextlib import suppress
from typing import Callable
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from gateways.base import Gateway, GatewayChat, GatewayHealth, GatewayResponse, GatewayUser, IncomingGatewayMessage, OutgoingGatewayMessage
from gateways.service import AgentGatewayService


logger = logging.getLogger(__name__)
DATABASE_NOT_READY_MESSAGE = (
    "Database is not ready. Run: agent-sam doctor or docker compose up -d postgres redis qdrant "
    "&& alembic upgrade head && python scripts/seed_dev.py"
)
EXIT_COMMANDS = {"/exit", "/quit"}
REQUEST_TIMEOUT_SECONDS = 8.0


class CLIGateway(Gateway):
    name = "cli"

    def __init__(
        self,
        *,
        service: AgentGatewayService,
        workspace_id: UUID,
        user_id: UUID,
        debug_logging: bool = False,
        input_reader: Callable[[str], str] | None = None,
        output_writer: Callable[[str], None] | None = None,
        request_timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._service = service
        self._workspace_id = workspace_id
        self._user_id = user_id
        self._debug_logging = debug_logging
        self._input_reader = input_reader or input
        self._output_writer = output_writer or print
        self._request_timeout_seconds = request_timeout_seconds
        self._runner_task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        logger.info("CLI gateway starting")
        self._stopping = False
        self._output_writer("Agent_Sam CLI gateway ready")
        self._output_writer("Type /help for commands or /exit to stop.")
        self._runner_task = asyncio.create_task(self._run_loop(), name="agent-sam-cli-gateway")
        logger.info("CLI gateway ready")
        logger.info("Listening for CLI messages")

    async def stop(self) -> None:
        if self._runner_task is None:
            return

        logger.info("CLI gateway stopping")
        self._stopping = True
        self._runner_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._runner_task
        self._runner_task = None
        logger.info("CLI gateway stopped")

    async def send_message(self, message: OutgoingGatewayMessage) -> None:
        self._output_writer(message.text)

    async def health_check(self) -> GatewayHealth:
        status = "ready" if self._runner_task is not None and not self._runner_task.done() else "stopped"
        return GatewayHealth(status=status)

    async def wait_until_closed(self) -> None:
        if self._runner_task is None:
            return
        with suppress(asyncio.CancelledError):
            await self._runner_task

    async def handle_cli_input(self, text: str) -> GatewayResponse | None:
        normalized_text = text.strip()
        if not normalized_text:
            return None

        if normalized_text.lower() in EXIT_COMMANDS:
            self._stopping = True
            return None

        incoming = IncomingGatewayMessage(
            workspace_id=self._workspace_id,
            user_id=self._user_id,
            gateway_name=self.name,
            gateway_user=GatewayUser(
                gateway_user_id="cli-local-user",
                username=getpass.getuser(),
                display_name="Local CLI User",
            ),
            gateway_chat=GatewayChat(
                gateway_chat_id="cli-terminal",
                title="Local CLI Terminal",
                chat_type="terminal",
            ),
            text=normalized_text,
        )

        logger.info("CLI message received from local terminal")
        if self._debug_logging:
            logger.debug("CLI message text: %s", normalized_text)

        try:
            response = await asyncio.wait_for(
                self._service.handle_incoming_message(incoming),
                timeout=self._request_timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning("CLI request timed out while waiting for the database or gateway service")
            if self._debug_logging:
                logger.debug("CLI timeout details", exc_info=True)
            response = GatewayResponse(text=DATABASE_NOT_READY_MESSAGE)
        except SQLAlchemyError:
            logger.warning("CLI request failed because the database is not ready")
            if self._debug_logging:
                logger.debug("CLI database failure details", exc_info=True)
            response = GatewayResponse(text=DATABASE_NOT_READY_MESSAGE)
        except Exception:
            logger.exception("CLI request failed")
            response = GatewayResponse(text="Request failed. Check logs for details.")

        if response.should_reply:
            self._output_writer(response.text)
        return response

    async def _run_loop(self) -> None:
        try:
            while not self._stopping:
                line = await asyncio.to_thread(self._input_reader, "> ")
                await self.handle_cli_input(line)
        except EOFError:
            self._stopping = True
        except KeyboardInterrupt:
            self._stopping = True
        except asyncio.CancelledError:
            raise
        finally:
            logger.info("CLI gateway loop stopped")