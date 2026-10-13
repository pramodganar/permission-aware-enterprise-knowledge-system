import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings

VALID_SECRET = "x" * 40


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test without APP_ variables from the developer's shell."""
    for name in (
        "APP_ENVIRONMENT",
        "APP_LOG_LEVEL",
        "APP_DATABASE_URL",
        "APP_JWT_SECRET_KEY",
        "APP_ACCESS_TOKEN_EXPIRE_MINUTES",
    ):
        monkeypatch.delenv(name, raising=False)


def load() -> Settings:
    # _env_file=None keeps the developer's real .env out of the tests
    return Settings(_env_file=None)


def test_defaults_when_only_secret_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_JWT_SECRET_KEY", VALID_SECRET)

    settings = load()

    assert settings.environment == "local"
    assert settings.log_level == "INFO"
    assert settings.database_url == "sqlite:///./data/app.db"
    assert settings.access_token_expire_minutes == 30
    assert (settings.chunk_max_characters, settings.chunk_overlap_characters) == (800, 120)
    assert settings.embedding_model == "sentence-transformers/all-MiniLM-L6-v2"
    assert (settings.embedding_batch_size, settings.embedding_offline) == (32, False)
    assert settings.max_question_characters == 1000


def test_environment_variable_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_JWT_SECRET_KEY", VALID_SECRET)
    monkeypatch.setenv("APP_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("APP_DATABASE_URL", "postgresql://db/knowledge")

    settings = load()

    assert settings.log_level == "DEBUG"
    assert settings.database_url == "postgresql://db/knowledge"


def test_missing_secret_fails() -> None:
    with pytest.raises(ValidationError) as error:
        load()

    assert "jwt_secret_key" in str(error.value)


@pytest.mark.parametrize("secret", ["", "short", "x" * 31])
def test_short_secret_fails(monkeypatch: pytest.MonkeyPatch, secret: str) -> None:
    monkeypatch.setenv("APP_JWT_SECRET_KEY", secret)

    with pytest.raises(ValidationError):
        load()


def test_secret_is_not_shown_when_settings_are_printed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_JWT_SECRET_KEY", VALID_SECRET)

    settings = load()

    assert VALID_SECRET not in repr(settings)
    assert VALID_SECRET not in str(settings)
    assert VALID_SECRET not in settings.model_dump_json()
    assert settings.jwt_secret_key.get_secret_value() == VALID_SECRET


def test_short_secret_error_does_not_echo_the_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_JWT_SECRET_KEY", "leaky-short-secret")

    with pytest.raises(ValidationError) as error:
        load()

    assert "leaky-short-secret" not in str(error.value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("APP_LOG_LEVEL", "VERBOSE"),
        ("APP_ENVIRONMENT", "staging"),
        ("APP_ACCESS_TOKEN_EXPIRE_MINUTES", "0"),
        ("APP_ACCESS_TOKEN_EXPIRE_MINUTES", "-5"),
        ("APP_ACCESS_TOKEN_EXPIRE_MINUTES", "abc"),
        ("APP_CHUNK_MAX_CHARACTERS", "50"),
        ("APP_CHUNK_MAX_CHARACTERS", "100000"),
        ("APP_CHUNK_OVERLAP_CHARACTERS", "-1"),
        ("APP_CHUNK_OVERLAP_CHARACTERS", "401"),
        ("APP_EMBEDDING_MODEL", ""),
        ("APP_EMBEDDING_BATCH_SIZE", "0"),
        ("APP_EMBEDDING_OFFLINE", "sometimes"),
        ("APP_MAX_QUESTION_CHARACTERS", "5"),
        ("APP_MAX_QUESTION_CHARACTERS", "100000"),
    ],
)
def test_invalid_values_fail(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv("APP_JWT_SECRET_KEY", VALID_SECRET)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        load()


def test_get_settings_reads_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_JWT_SECRET_KEY", VALID_SECRET)
    get_settings.cache_clear()

    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()


def test_overlap_may_be_half_of_the_chunk_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_JWT_SECRET_KEY", VALID_SECRET)
    monkeypatch.setenv("APP_CHUNK_MAX_CHARACTERS", "400")
    monkeypatch.setenv("APP_CHUNK_OVERLAP_CHARACTERS", "200")

    assert load().chunk_overlap_characters == 200


def test_overlap_is_checked_against_a_changed_chunk_size(monkeypatch: pytest.MonkeyPatch) -> None:
    # The default overlap of 120 is too large for a chunk size of 200.
    monkeypatch.setenv("APP_JWT_SECRET_KEY", VALID_SECRET)
    monkeypatch.setenv("APP_CHUNK_MAX_CHARACTERS", "200")

    with pytest.raises(ValidationError, match="at most half"):
        load()
