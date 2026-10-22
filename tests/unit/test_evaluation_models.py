"""Choosing the model a measurement uses, without calling any of them."""

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from evaluation import models
from evaluation.company import measuring_settings
from evaluation.models import RepeatEverything, groq_key, provider_for


def test_the_worst_model_needs_no_settings() -> None:
    assert isinstance(provider_for("worst", measuring_settings()), RepeatEverything)


def test_groq_goes_through_the_services_own_provider() -> None:
    settings = measuring_settings(
        llm_provider="groq", llm_api_key=SecretStr("k" * 20), llm_max_output_tokens=1000
    )

    provider = provider_for("groq", settings)

    assert provider.name == "groq"
    assert settings.llm_max_output_tokens == 1000
    close = getattr(provider, "close", None)
    if callable(close):
        close()


def test_groq_without_a_provider_in_the_settings_is_refused() -> None:
    with pytest.raises(ValueError, match="no language model"):
        provider_for("groq", measuring_settings())


def test_the_key_is_read_the_way_the_service_reads_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "Settings", lambda: SimpleNamespace(llm_api_key=SecretStr("abc")))

    assert groq_key().get_secret_value() == "abc"


def test_no_key_stops_the_run_and_says_where_to_put_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "Settings", lambda: SimpleNamespace(llm_api_key=None))

    with pytest.raises(SystemExit, match="APP_LLM_API_KEY"):
        groq_key()
