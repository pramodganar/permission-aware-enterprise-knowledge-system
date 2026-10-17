"""The demo page itself, run headless with the real embedding model. Run with: pytest -m model

No model is configured here, so the page shows passages instead of answers
and nothing is sent anywhere.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

pytestmark = pytest.mark.model

PAGE = Path(__file__).resolve().parents[2] / "ui" / "streamlit_app.py"


@pytest.fixture(scope="module")
def page() -> Iterator[AppTest]:
    with pytest.MonkeyPatch.context() as env:
        env.setenv("APP_LLM_PROVIDER", "none")
        app = AppTest.from_file(str(PAGE), default_timeout=600)
        app.run()
        yield app


def ask(page: AppTest, person: str, question: str) -> AppTest:
    page.sidebar.selectbox[0].set_value(person).run()
    page.text_input[0].set_value(question)
    page.button[0].click().run()
    assert not page.exception
    return page


def test_the_page_loads(page: AppTest) -> None:
    assert not page.exception
    assert page.title[0].value == "Permission-Aware Knowledge Assistant"
    # Without a model it says so, rather than failing.
    assert any("No language model is configured" in w.value for w in page.sidebar.warning)


def test_the_sidebar_shows_what_the_person_may_read(page: AppTest) -> None:
    page.sidebar.selectbox[0].set_value("david").run()

    shown = "\n".join(m.value for m in page.sidebar.markdown)
    assert "May read 4 of 17 documents" in shown
    assert "Discount Approval Policy" in shown
    assert "Salary Bands 2026" not in shown


def test_a_person_sees_passages_only_from_their_own_documents(page: AppTest) -> None:
    ask(page, "frank", "What is the hotel limit when travelling?")

    titles = [e.label for e in page.tabs[0].expander]
    assert any("Travel and Expense Policy" in t for t in titles)
    assert not any("Executive" in t or "Salary" in t for t in titles)


def test_a_forbidden_question_gets_no_information(page: AppTest) -> None:
    ask(page, "david", "How are executive bonuses calculated?")

    assert [i.value for i in page.tabs[0].info] == [
        "No information available to answer this question."
    ]
    assert list(page.tabs[0].expander) == []
