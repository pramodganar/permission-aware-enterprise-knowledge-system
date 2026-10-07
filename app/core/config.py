"""Application settings.

Values come from environment variables prefixed with APP_, or from a .env file
in the project root. Environment variables win over the file.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Validation errors name the field but don't print the rejected value,
        # so a mistyped secret doesn't end up in startup logs.
        hide_input_in_errors=True,
    )

    environment: Literal["local", "test", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    database_url: str = "sqlite:///./data/app.db"

    # No default on purpose: the service must not start with a known secret.
    jwt_secret_key: SecretStr = Field(min_length=32)
    access_token_expire_minutes: int = Field(default=30, gt=0, le=1440)


@lru_cache
def get_settings() -> Settings:
    """Return the settings, read once per process."""
    return Settings()
