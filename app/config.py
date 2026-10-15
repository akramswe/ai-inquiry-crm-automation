from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    app_name: str = "AI Inquiry & CRM Automation"
    database_url: str = "sqlite:///./data/app.db"
    internal_api_token: str = "local-dev-token-change-me"
    admin_username: str = "admin"
    admin_password: str = "change-me-before-deployment"
    ai_mode: str = "mock"  # mock or openai
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    smtp_host: str = ""  # empty: safe console/outbox mode
    smtp_port: int = 1025
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = False
    mail_from: str = "demo@example.com"
    followup_delay_hours: int = Field(default=48, ge=0)
    log_level: str = "INFO"
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
