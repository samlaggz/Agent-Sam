from functools import lru_cache
from typing import Annotated
from uuid import UUID

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Agent Sam"
    environment: str = Field(default="development", validation_alias=AliasChoices("ENVIRONMENT", "APP_ENV"))
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    database_url: str = "postgresql+psycopg://agent_sam:agent_sam@localhost:5432/agent_sam"
    redis_url: str = "redis://localhost:6379/0"
    qdrant_url: str = "http://localhost:6333"
    vector_backend: str = "qdrant"
    enabled_gateways: Annotated[tuple[str, ...], NoDecode] = ()
    gateway_debug_logging: bool = False
    telegram_bot_token: str = ""
    whatsapp_provider: str = "twilio"
    whatsapp_twilio_account_sid: str = ""
    whatsapp_twilio_auth_token: str = ""
    whatsapp_twilio_from: str = ""
    whatsapp_meta_access_token: str = ""
    whatsapp_meta_phone_number_id: str = ""
    whatsapp_meta_verify_token: str = ""
    discord_bot_token: str = ""
    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    webhook_gateway_secret: str = ""
    default_workspace_id: UUID | None = None
    default_user_id: UUID | None = None
    default_model: str = "openrouter/openai/gpt-4.1-mini"
    litellm_model: str = "gpt-4.1-mini"
    litellm_api_key: str = ""
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    router_agent_model: str = "openrouter/openai/gpt-4.1-mini"
    coding_agent_model: str = "openrouter/openai/gpt-4.1-mini"
    coding_agent_escalation_model: str = "openrouter/anthropic/claude-3.7-sonnet"
    testing_agent_model: str = "openrouter/google/gemini-2.0-flash-001"
    testing_agent_escalation_model: str = "openrouter/openai/gpt-4.1"
    research_agent_model: str = "openrouter/google/gemini-2.0-flash-001"
    research_agent_escalation_model: str = "openrouter/anthropic/claude-3.7-sonnet"
    planning_agent_model: str = "openrouter/openai/gpt-4.1-mini"
    planning_agent_escalation_model: str = "openrouter/anthropic/claude-3.7-sonnet"
    server_ops_agent_model: str = "openrouter/openai/gpt-4.1-mini"
    server_ops_agent_escalation_model: str = "openrouter/anthropic/claude-3.7-sonnet"
    graphics_agent_model: str = "openrouter/google/gemini-2.0-flash-001"
    graphics_agent_escalation_model: str = "openrouter/anthropic/claude-3.7-sonnet"
    data_agent_model: str = "openrouter/openai/gpt-4.1-mini"
    data_agent_escalation_model: str = "openrouter/anthropic/claude-3.7-sonnet"
    qa_agent_model: str = "openrouter/openai/gpt-4.1-mini"
    qa_agent_escalation_model: str = "openrouter/anthropic/claude-3.7-sonnet"
    max_cost_per_task_usd: float = 0.25
    daily_model_budget_usd: float = 5.0
    allow_model_escalation: bool = True
    allow_skill_auto_proposal: bool = True
    allow_skill_auto_activation: bool = False
    allow_sub_agent_proposal: bool = True
    allow_sub_agent_auto_creation: bool = False
    enable_web_research: bool = False
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    ollama_base_url: str = "http://127.0.0.1:11434"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("default_workspace_id", "default_user_id", mode="before")
    @classmethod
    def normalize_optional_uuid(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("enabled_gateways", mode="before")
    @classmethod
    def parse_enabled_gateways(cls, value: object) -> tuple[str, ...]:
        if value in (None, ""):
            return ()

        if isinstance(value, str):
            raw_items = value.split(",")
        elif isinstance(value, (list, tuple, set)):
            raw_items = list(value)
        else:
            raise TypeError("ENABLED_GATEWAYS must be a comma-separated string or a sequence of names.")

        normalized: list[str] = []
        seen: set[str] = set()
        for raw_item in raw_items:
            name = str(raw_item).strip().lower()
            if not name or name in seen:
                continue
            normalized.append(name)
            seen.add(name)
        return tuple(normalized)

    @field_validator("whatsapp_provider", mode="before")
    @classmethod
    def normalize_whatsapp_provider(cls, value: object) -> str:
        if value in (None, ""):
            return "twilio"
        return str(value).strip().lower()


@lru_cache
def get_settings() -> Settings:
    return Settings()
