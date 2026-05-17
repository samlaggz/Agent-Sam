from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Sequence
from contextlib import suppress

from app.asyncio_compat import configure_windows_event_loop_policy
from app.config import Settings, get_settings
from gateways.base import Gateway, GatewayConfigurationError
from gateways.registry import build_enabled_gateway_registry, build_gateway
from gateways.runtime_logging import configure_gateway_logging


logger = logging.getLogger(__name__)


async def run_enabled_gateways(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    registry = build_enabled_gateway_registry(settings)
    if not registry.gateways:
        raise GatewayConfigurationError(
            "No gateways are enabled. Set ENABLED_GATEWAYS to a comma-separated list such as 'telegram,cli'."
        )

    logger.info("Enabled gateways: %s", ", ".join(registry.names))
    await _run_gateways(registry.gateways)


async def run_single_gateway(name: str, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    gateway = build_gateway(name, settings)
    logger.info("Starting direct gateway: %s", gateway.name)
    await _run_gateways((gateway,))


def run() -> None:
    configure_windows_event_loop_policy()
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    configure_gateway_logging(debug_logging=settings.gateway_debug_logging)
    try:
        asyncio.run(run_enabled_gateways(settings))
    except GatewayConfigurationError as exc:
        logger.error("Gateway configuration error: %s", exc)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        logger.info("Gateway runner interrupted")


def run_named_gateway(name: str) -> None:
    configure_windows_event_loop_policy()
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    configure_gateway_logging(debug_logging=settings.gateway_debug_logging)
    try:
        asyncio.run(run_single_gateway(name, settings))
    except GatewayConfigurationError as exc:
        logger.error("Gateway configuration error: %s", exc)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        logger.info("Gateway '%s' interrupted", name)


async def _run_gateways(gateways: Sequence[Gateway]) -> None:
    started: list[Gateway] = []
    interrupted = False
    try:
        for gateway in gateways:
            await gateway.start()
            started.append(gateway)

        await _wait_for_gateways(gateways)
    except asyncio.CancelledError:
        interrupted = True
    finally:
        for gateway in reversed(started):
            try:
                with suppress(asyncio.CancelledError):
                    await asyncio.shield(gateway.stop())
            except Exception:
                logger.exception("Gateway '%s' failed during shutdown", gateway.name)

    if interrupted:
        raise asyncio.CancelledError


async def _wait_for_gateways(gateways: Sequence[Gateway]) -> None:
    waiters: list[Awaitable[object]] = []
    for gateway in gateways:
        waiter = getattr(gateway, "wait_until_closed", None)
        if waiter is None:
            continue
        result = waiter()
        if inspect.isawaitable(result):
            waiters.append(result)

    if waiters:
        await asyncio.gather(*waiters)
        return

    await asyncio.Event().wait()


if __name__ == "__main__":
    run()