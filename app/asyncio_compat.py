from __future__ import annotations

import asyncio
import sys


def configure_windows_event_loop_policy() -> None:
    if sys.platform != "win32":
        return

    policy_class = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if policy_class is None:
        return

    current_policy = asyncio.get_event_loop_policy()
    if isinstance(current_policy, policy_class):
        return

    asyncio.set_event_loop_policy(policy_class())