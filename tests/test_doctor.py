from __future__ import annotations

from scripts.doctor import _check_llm_config, _validate_model_name


def test_doctor_requires_openrouter_credentials_for_openrouter_models() -> None:
    checks = _check_llm_config(
        {
            "LITELLM_MODEL": "openrouter/openai/gpt-4.1-mini",
            "DEFAULT_MODEL": "openrouter/openai/gpt-4.1-mini",
            "MAX_COST_PER_TASK_USD": "0.25",
            "DAILY_MODEL_BUDGET_USD": "5.0",
        }
    )

    assert any(check.status == "FAIL" and "OpenRouter credentials are missing" in check.message for check in checks)


def test_validate_model_name_rejects_bad_openrouter_syntax() -> None:
    assert _validate_model_name("openrouter/gpt-4.1-mini") == "OpenRouter models must use openrouter/provider/model-name syntax"
    assert _validate_model_name("openrouter/openai/gpt-4.1-mini") is None