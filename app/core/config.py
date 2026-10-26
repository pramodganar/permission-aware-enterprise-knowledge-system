"""Application settings.

Values come from environment variables prefixed with APP_, or from a .env file
in the project root. Environment variables win over the file.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Where an API key may go without TLS: this machine, as Ollama runs.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# A signing key made by secrets.token_urlsafe(48) has around 40 different
# characters. One with fewer than this was typed, not generated.
MIN_DISTINCT_KEY_CHARACTERS = 16


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
    # Failed logins allowed for one email within the window before further
    # attempts are refused without checking the password (threat T10).
    login_max_failures: int = Field(default=5, ge=1, le=100)
    login_failure_window_seconds: int = Field(default=900, ge=1, le=86_400)
    # Questions one person may ask, in any minute and in any 24 hours, and
    # searches in any minute (threat T24). Every call counts, whatever it
    # asks and however it ends. The daily limit is the cap on what one person
    # can spend on the model. See docs/api.md.
    ask_limit_per_minute: int = Field(default=10, ge=1, le=10_000)
    ask_limit_per_day: int = Field(default=200, ge=1, le=100_000)
    search_limit_per_minute: int = Field(default=30, ge=1, le=10_000)

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
    # How many index versions to keep, the current one included, so a bad
    # build can be rolled back.
    index_keep_versions: int = Field(default=3, ge=1, le=100)

    # The longest question accepted, before it is looked at any further.
    max_question_characters: int = Field(default=1000, ge=10, le=10_000)

    # How many chunks a question retrieves.
    retrieval_top_k: int = Field(default=5, ge=1, le=100)
    # If the best chunk scores below this, nothing is relevant and nothing is
    # returned. A cosine similarity, so it only means something for the model
    # it was measured with. See docs/retrieval.md.
    retrieval_min_score: float = Field(default=0.35, ge=-1.0, le=1.0)

    # A cross-encoder that reorders the chunks found, or None for no
    # reranking. Off by default: on the company data it didn't change which
    # document came first. See docs/retrieval.md.
    reranker_model: str | None = Field(default=None, min_length=1)
    # How many chunks the reranker chooses the top ones from.
    rerank_candidates: int = Field(default=20, ge=1, le=100)

    # The most document text sent to the LLM for one question (NFR-3).
    # Characters, not tokens: every provider counts tokens differently, and
    # this has to hold before any provider is asked.
    context_max_characters: int = Field(default=6000, ge=500, le=100_000)

    # Which language model answers questions. "none" sends nothing anywhere:
    # questions get search results but no answers. "ollama" runs the model on
    # this machine; "groq" sends the prompt, company text, to Groq. See
    # app/rag/providers.py and docs/generation.md.
    llm_provider: Literal["none", "ollama", "groq"] = "none"
    # Each is optional and overrides the provider's preset.
    llm_model: str | None = Field(default=None, min_length=1)
    llm_base_url: str | None = Field(default=None, min_length=1)
    llm_context_tokens: int | None = Field(default=None, ge=512, le=2_000_000)
    llm_api_key: SecretStr | None = None

    # Every call to the language model (NFR-3, NFR-5). The longest a question
    # can wait is about (retries + 1) x timeout, plus a few seconds of pauses.
    # A local model on a laptop CPU needs minutes, not seconds.
    llm_max_output_tokens: int = Field(default=600, ge=50, le=4000)
    llm_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    llm_max_retries: int = Field(default=2, ge=0, le=5)

    @field_validator("llm_api_key", mode="before")
    @classmethod
    def empty_key_is_no_key(cls, value: object) -> object:
        # Docker Compose passes a variable that isn't set as an empty one. An
        # empty key is no key, so a provider that needs one says so at startup
        # instead of sending an empty key on every call.
        if value == "" or (isinstance(value, SecretStr) and not value.get_secret_value()):
            return None
        return value

    @model_validator(mode="after")
    def check_sizes(self) -> "Settings":
        # More than half would put most of every chunk into the next one.
        if self.chunk_overlap_characters * 2 > self.chunk_max_characters:
            raise ValueError(
                "chunk_overlap_characters must be at most half of chunk_max_characters"
            )
        # Otherwise the best chunk alone might not fit, and a question with a
        # good answer would get no context at all.
        if self.context_max_characters < self.chunk_max_characters:
            raise ValueError("context_max_characters must be at least chunk_max_characters")
        if self.llm_provider == "groq" and self.llm_api_key is None:
            raise ValueError("llm_provider groq needs llm_api_key")
        return self

    @model_validator(mode="after")
    def check_secrets(self) -> "Settings":
        # The key goes in a header on every call, so over plain HTTP anyone
        # on the way can read it.
        if self.llm_api_key is not None and self.llm_base_url is not None:
            address = urlsplit(self.llm_base_url)
            if address.scheme != "https" and address.hostname not in LOCAL_HOSTS:
                raise ValueError("llm_base_url must use https when an llm_api_key is set")
        # Not a measure of randomness, only a stop for a key someone typed,
        # such as a word repeated to reach the length.
        key = self.jwt_secret_key.get_secret_value()
        if self.environment == "production" and len(set(key)) < MIN_DISTINCT_KEY_CHARACTERS:
            raise ValueError(
                "jwt_secret_key looks typed rather than generated; make one with "
                'python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the settings, read once per process."""
    return Settings()
