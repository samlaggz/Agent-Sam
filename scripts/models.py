from __future__ import annotations

import argparse

from agents.registry import list_agents
from app.config import Settings
from db.session import AsyncSessionLocal
from services.model_router import ModelRouter


def run() -> None:
    parser = argparse.ArgumentParser(description="Inspect and validate configured models.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="List configured agent models.")
    validate_parser = subparsers.add_parser("validate", help="Validate configured model names.")
    validate_parser.add_argument("--live", action="store_true", help="Run a live lightweight completion against each model.")
    arguments = parser.parse_args()

    settings = Settings()
    router = ModelRouter(settings, AsyncSessionLocal)
    profiles = list_agents()
    models = router.list_configured_models(profiles)

    if arguments.command == "list":
        for model_name in models:
            print(model_name)
        raise SystemExit(0)

    import asyncio

    results = asyncio.run(router.validate_models(models, live=arguments.live))
    exit_code = 0
    for result in results:
        prefix = "OK" if result.valid else "FAIL"
        print(f"[{prefix}] {result.model} ({result.provider}) - {result.reason}")
        if not result.valid:
            exit_code = 1
    raise SystemExit(exit_code)


if __name__ == "__main__":
    run()