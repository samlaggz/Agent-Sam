from app.asyncio_compat import configure_windows_event_loop_policy
from gateways.runner import run_named_gateway


def run() -> None:
    configure_windows_event_loop_policy()
    run_named_gateway("telegram")


if __name__ == "__main__":
    run()
