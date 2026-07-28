"""Application settings loaded from environment variables."""
import logging
from pathlib import Path
from typing import Annotated, List

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Load .env into os.environ early so LangChain/LangGraph can read LANGCHAIN_* vars directly
load_dotenv()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Telegram
    telegram_bot_token: str = Field(..., description="Telegram bot token from @BotFather")
    # Annotated[..., NoDecode] prevents pydantic-settings from JSON-decoding the raw
    # env value before parse_user_ids() runs. Without it, a comma-separated value like
    # "123,456" is fed to json.loads() first and raises SettingsError at startup.
    telegram_allowed_user_ids: Annotated[List[int], NoDecode] = Field(
        default_factory=list, description="Allowed Telegram user IDs"
    )

    # OpenRouter
    openrouter_api_key: str = Field(..., description="OpenRouter API key")
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1", description="OpenRouter base URL"
    )

    # Kleinanzeigen
    kleinanzeigen_config_path: Path = Field(
        default=Path("./kleinanzeigen-config"),
        description="Path to kleinanzeigen-bot config directory",
    )
    discogs_user_token: str = Field(
        default="",
        description="Discogs user token for inventory sync",
    )
    discogs_username: str = Field(
        default="",
        description="Discogs username (owner of the inventory)",
    )

    # Scheduler
    daily_check_hour: int = Field(default=6, ge=0, le=23, description="Hour for daily check")
    daily_check_minute: int = Field(
        default=0, ge=0, le=59, description="Minute for daily check"
    )
    shipping_expiring_days: int = Field(
        default=10, ge=1, description="Days threshold for SHIPPING listings to be considered expiring soon"
    )
    pickup_expiring_days: int = Field(
        default=30, ge=1, description="Days threshold for PICKUP listings to be considered expiring soon (2× more frequent renewal)"
    )

    # LangSmith / LangChain tracing (optional)
    langchain_tracing_v2: str = Field(
        default="false", description="Enable LangSmith tracing (true/false)"
    )
    langchain_api_key: str = Field(
        default="", description="LangSmith API key (same as LANGCHAIN_API_KEY)"
    )
    langchain_project: str = Field(
        default="kleinanzeigen-bot", description="LangSmith project name"
    )
    # Legacy alias kept for backwards compatibility
    langsmith_api_key: str = Field(
        default="", description="Deprecated: use langchain_api_key instead"
    )

    # General
    log_level: str = Field(default="INFO", description="Logging level")
    database_path: Path = Field(
        default=Path("./data/bot.db"), description="Path to SQLite database"
    )

    @field_validator("telegram_allowed_user_ids", mode="before")
    @classmethod
    def parse_user_ids(cls, v: object) -> List[int]:
        if isinstance(v, str):
            return [int(uid.strip()) for uid in v.split(",") if uid.strip()]
        if isinstance(v, int):
            return [v]
        if isinstance(v, list):
            return [int(uid) for uid in v]
        return []

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid_levels:
            raise ValueError(f"Invalid log level: {v}")
        return upper

    def get_log_level(self) -> int:
        return getattr(logging, self.log_level)


settings = Settings()  # type: ignore[call-arg]
