from pathlib import Path

from scripts import bootstrap_preflight as preflight_module


def test_run_preflight_keeps_working_database_url(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "DATABASE_URL=postgresql+psycopg://agent_sam_user:custom@127.0.0.1:5432/agent_sam\n",
        encoding="utf-8",
    )
    outputs: list[str] = []

    exit_code = preflight_module.run_preflight(
        env_path=env_path,
        output=outputs.append,
        database_probe=lambda database_url: database_url.endswith("custom@127.0.0.1:5432/agent_sam"),
    )

    assert exit_code == 0
    assert "agent_sam_user:custom" in env_path.read_text(encoding="utf-8")
    assert outputs == ["DATABASE_URL connection check passed."]


def test_run_preflight_switches_to_bundled_local_database_url_when_it_works(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "DATABASE_URL=postgresql+psycopg://agent_sam_user:custom@127.0.0.1:5432/agent_sam\n",
        encoding="utf-8",
    )
    outputs: list[str] = []

    exit_code = preflight_module.run_preflight(
        env_path=env_path,
        output=outputs.append,
        database_probe=lambda database_url: database_url == preflight_module.BUNDLED_LOCAL_DATABASE_URL,
    )

    assert exit_code == 0
    assert f"DATABASE_URL={preflight_module.BUNDLED_LOCAL_DATABASE_URL}" in env_path.read_text(encoding="utf-8")
    assert outputs == [
        "Configured DATABASE_URL could not authenticate against the local loopback Postgres service. Switched DATABASE_URL to the bundled local Postgres defaults."
    ]


def test_run_preflight_fails_when_database_url_and_bundled_fallback_both_fail(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "DATABASE_URL=postgresql+psycopg://agent_sam_user:custom@127.0.0.1:5432/agent_sam\n",
        encoding="utf-8",
    )
    outputs: list[str] = []

    exit_code = preflight_module.run_preflight(
        env_path=env_path,
        output=outputs.append,
        database_probe=lambda database_url: False,
    )

    assert exit_code == 1
    assert "agent_sam_user:custom" in env_path.read_text(encoding="utf-8")
    assert outputs == ["DATABASE_URL connection check failed. Update DATABASE_URL and rerun the bootstrap."]


def test_run_preflight_does_not_try_bundled_fallback_for_non_loopback_database_url(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "DATABASE_URL=postgresql+psycopg://agent_sam_user:custom@db.example.com:5432/agent_sam\n",
        encoding="utf-8",
    )
    outputs: list[str] = []
    observed_urls: list[str] = []

    def probe(database_url: str) -> bool:
        observed_urls.append(database_url)
        return False

    exit_code = preflight_module.run_preflight(
        env_path=env_path,
        output=outputs.append,
        database_probe=probe,
    )

    assert exit_code == 1
    assert observed_urls == ["postgresql+psycopg://agent_sam_user:custom@db.example.com:5432/agent_sam"]
    assert outputs == ["DATABASE_URL connection check failed. Update DATABASE_URL and rerun the bootstrap."]