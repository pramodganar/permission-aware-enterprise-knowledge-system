"""Application settings.

Values come from environment variables prefixed with APP_, or from a .env file
in the project root. Environment variables win over the file.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
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

    max_upload_bytes: int = Field(default=10 * 1024 * 1024, gt=0)

    # How documents are split. Changing either means re-chunking and
    # re-embedding every document. See docs/chunking.md.
    chunk_max_characters: int = Field(default=800, ge=100, le=8000)
    chunk_overlap_characters: int = Field(default=120, ge=0)

    # The model that embeds chunks and questions. It runs locally. Changing it
    # means embedding every chunk again.
    embedding_model: str = Field(default="sentence-transformers/all-MiniLM-L6-v2", min_length=1)
    embedding_batch_size: int = Field(default=32, gt=0, le=1024)
    # True loads the model from the local cache only and makes no network call.
    embedding_offline: bool = False

    # Where the vector index and its manifest are saved. The index holds
    # vectors of every document, restricted ones included, so this folder
    # needs the same protection as the database.
    index_directory: Path = Path("data/index")

    @model_validator(mode="after")
    def check_overlap(self) -> "Settings":
        # More than half would put most of every chunk into the next one.
        if self.chunk_overlap_characters * 2 > self.chunk_max_characters:
            raise ValueError(
                "chunk_overlap_characters must be at most half of chunk_max_characters"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the settings, read once per process."""
    return Settings()
