"""The demo page's budget holds across visits (threat T31).

The page is run headless with the fake embedder and model in place of the
real ones, and a budget of two questions. Each ``AppTest`` is a new visit
with its own session, as a reload is. The demo, and its budget, is built
once for the process and shared by every visit.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from app.core.config import Settings
from app.core.rate_limit import RateLimiter, Window
from app.rag.llm import LLMProvider
from fakes import FakeEmbedder, FakeLLM
from ui import demo as demo_module
from ui.demo import Demo

PAGE = Path(__file__).resolve().parents[2] / "ui" / "streamlit_app.py"
ANSWER = "The hotel limit is 160 EUR [S1]."
SPENT = "The demo has answered as many questions as it will for now"


@pytest.fixture
def builds(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Demo]]:
    built: list[Demo] = []
    real = demo_module.build_demo

    def build(settings: Settings, _provider: LLMProvider | None, *_: Any) -> Demo:
        demo = real(settings, FakeLLM([ANSWER]), FakeEmbedder(), RateLimiter([Window(2, 3600)]))
        built.append(demo)
        return demo

    monkeypatch.setenv("APP_LLM_PROVIDER", "none")
    monkeypatch.setattr(demo_module, "build_demo", build)
    st.cache_resource.clear()
    yield built
    st.cache_resource.clear()


def visit() -> AppTest:
    page = AppTest.from_file(str(PAGE), default_timeout=120)
    page.run()
    assert not page.exception
    return page


def ask(page: AppTest, person: str, question: str) -> AppTest:
    page.sidebar.selectbox[0].set_value(person).run()
    page.text_input[0].set_value(question)
    page.button[0].click().run()
    assert not page.exception
    return page


def answered(page: AppTest) -> bool:
    return any(ANSWER in m.value for m in page.tabs[0].markdown)


def spent(page: AppTest) -> bool:
    return any(SPENT in w.value for w in page.tabs[0].warning)


def test_reloading_the_page_does_not_start_a_new_budget(builds: list[Demo]) -> None:
    first = visit()
    assert answered(ask(first, "frank", "What is the hotel limit when travelling?"))
    assert answered(ask(first, "david", "What is the hotel limit when travelling?"))

    again = visit()
    refused = ask(again, "carol", "What is the hotel limit when travelling?")

    assert spent(refused)
    assert not answered(refused)
    assert len(builds) == 1


def test_a_question_already_answered_is_still_shown_when_the_budget_is_spent(
    builds: list[Demo],
) -> None:
    first = visit()
    ask(first, "frank", "What is the hotel limit when travelling?")
    ask(first, "david", "What is the hotel limit when travelling?")

    again = ask(visit(), "frank", "What is the hotel limit when travelling?")

    assert answered(again)
    assert not spent(again)
