from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.env_writer import load_env_values, update_env_file
from scripts.install import BUNDLED_LOCAL_DATABASE_URL


OutputFunc = Callable[[str], None]
DatabaseProbe = Callable[[str], bool]


def run() -> None:
    parser = argparse.ArgumentParser(description="Validate production dependency connectivity before running migrations.")
    parser.add_argument("--env-file", type=Path, required=True)
    arguments = parser.parse_args()
    raise SystemExit(run_preflight(env_path=arguments.env_file))


def run_preflight(
    *,
    env_path: Path,
    output: OutputFunc = print,
    database_probe: DatabaseProbe | None = None,
) -> int:
    env_values = load_env_values(env_path)
    database_url = env_values.get("DATABASE_URL", "").strip()

    if not database_url:
        output("DATABASE_URL is missing; cannot validate database connectivity.")
        return 1

    probe = database_probe or _probe_database_url
    if probe(database_url):
        output("DATABASE_URL connection check passed.")
        return 0

    if database_url != BUNDLED_LOCAL_DATABASE_URL and _is_loopback_database_url(database_url) and probe(BUNDLED_LOCAL_DATABASE_URL):
        update_env_file(env_path, {"DATABASE_URL": BUNDLED_LOCAL_DATABASE_URL}, preserve_existing_values=False)
        output(
            "Configured DATABASE_URL could not authenticate against the local loopback Postgres service. "
            "Switched DATABASE_URL to the bundled local Postgres defaults."
        )
        return 0

    output("DATABASE_URL connection check failed. Update DATABASE_URL and rerun the bootstrap.")
    return 1


def _is_loopback_database_url(database_url: str) -> bool:
    parsed = urlparse(database_url.strip())
    host = (parsed.hostname or "").strip().lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def _probe_database_url(database_url: str) -> bool:
    return asyncio.run(_probe_database_url_async(database_url))


async def _probe_database_url_async(database_url: str) -> bool:
    engine = create_async_engine(database_url, future=True, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        return False
    finally:
        await engine.dispose()
    return True


if __name__ == "__main__":
    run()