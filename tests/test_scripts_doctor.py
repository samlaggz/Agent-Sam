from scripts import doctor as doctor_module


def test_minimum_python_version_parses_project_floor() -> None:
    assert doctor_module._minimum_python_version(">=3.10") == (3, 10, 0)
    assert doctor_module._minimum_python_version(">=3.10.11") == (3, 10, 11)


def test_required_env_checks_report_missing_values_with_fixes() -> None:
    checks = doctor_module._check_required_env_values({"DATABASE_URL": "postgres://example"})
    rendered = [(check.status, check.message) for check in checks]

    assert ("OK", "DATABASE_URL is set") in rendered
    assert ("FAIL", "DEFAULT_WORKSPACE_ID is missing") in rendered
    assert ("FIX", "Run: python scripts/seed_dev.py") in rendered
    assert ("FAIL", "ENABLED_GATEWAYS is missing") in rendered
    assert ("FIX", "Run: python -m gateways.setup") in rendered


def test_production_env_checks_use_production_fix_messages() -> None:
    checks = doctor_module._check_required_env_values({}, production=True)
    rendered = [(check.status, check.message) for check in checks]

    assert ("FAIL", "DEFAULT_WORKSPACE_ID is missing") in rendered
    assert (
        "FIX",
        "Run bash deployment/linux/bootstrap-app.sh to seed defaults, or set DEFAULT_USER_ID and DEFAULT_WORKSPACE_ID manually",
    ) in rendered
    assert ("FAIL", "ENABLED_GATEWAYS is missing") in rendered
    assert ("FIX", "Run: python -m scripts.setup --production") in rendered


def test_systemd_unit_checks_report_missing_production_units(tmp_path) -> None:
    checks = doctor_module._check_systemd_units(tmp_path)
    rendered = [(check.status, check.message) for check in checks]

    assert rendered[0][0] == "FAIL"
    assert "agent-api.service" in rendered[0][1]
    assert rendered[1] == (
        "FIX",
        "Run bash install.sh or deployment/linux/install-host-assets.sh from a sudo-capable operator account",
    )


def test_env_duplicate_checks_report_duplicate_keys(tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("DATABASE_URL=postgres://one\nDATABASE_URL=postgres://two\n", encoding="utf-8")

    checks = doctor_module._check_env_duplicates(env_path)
    rendered = [(check.status, check.message) for check in checks]

    assert rendered[0] == ("FAIL", "Duplicate .env keys found: DATABASE_URL")
    assert rendered[1] == ("FIX", "Remove duplicate keys from .env so each setting appears only once")


def test_llm_checks_require_provider_credentials() -> None:
    checks = doctor_module._check_llm_config({"LITELLM_MODEL": "openai/gpt-4o-mini"}, production=True)
    rendered = [(check.status, check.message) for check in checks]

    assert ("OK", "LITELLM_MODEL is set (openai/gpt-4o-mini)") in rendered
    assert ("FAIL", "OpenAI-compatible API credentials are missing") in rendered
    assert ("FIX", "Set OPENAI_API_KEY or LITELLM_API_KEY in .env") in rendered


def test_apply_safe_fixes_creates_env_and_app_dir(tmp_path) -> None:
    env_path = tmp_path / ".env"
    example_path = tmp_path / ".env.example"
    app_dir = tmp_path / "app"
    example_path.write_text("APP_ENV=production\n", encoding="utf-8")

    checks = doctor_module._apply_safe_fixes(env_path, production=True, app_dir=app_dir)
    rendered = [(check.status, check.message) for check in checks]

    assert env_path.exists()
    assert app_dir.exists()
    assert ("FIXED", f"Created .env at {env_path}") in rendered
    assert ("FIXED", f"Created app directory {app_dir}") in rendered