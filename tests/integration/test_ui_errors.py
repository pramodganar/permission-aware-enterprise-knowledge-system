"""What a browser is shown when a page fails with an error nothing on it caught.

Streamlit draws such an error itself, by default with its message and its
traceback. Both pages set it to the type only (``ui.text.ERROR_DETAILS``).
Each page is made to fail with a document's text in the error's message,
and the browser must get Streamlit's redacted message and no traceback.
With the setting put back to Streamlit's default, the same check sees the
text, so it is looking in the right place.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import NoReturn

import pytest
import streamlit as st
from streamlit import config
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[2]
# The page, and a function it calls before drawing anything else.
PAGES = {
    "ui": (ROOT / "ui" / "app.py", "ui.session.current_user"),
    "demo": (ROOT / "ui" / "streamlit_app.py", "ui.demo.build_demo"),
}
PASSAGE = "Senior engineers are paid between 68,000 and 92,000 a year."


def fail(*_: object, **__: object) -> NoReturn:
    raise ValueError(f"could not use {PASSAGE!r}")


@pytest.fixture(autouse=True)
def as_it_was() -> Iterator[None]:
    # The option is the process's, and every page test runs in this process.
    before = config.get_option("client.showErrorDetails")
    yield
    config.set_option("client.showErrorDetails", before)
    st.cache_resource.clear()


def failed(monkeypatch: pytest.MonkeyPatch, page: str) -> tuple[str, list[str]]:
    path, target = PAGES[page]
    monkeypatch.setenv("UI_API_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("APP_LLM_PROVIDER", "none")
    monkeypatch.setattr(target, fail)
    # The demo is built once per process and kept: build it again, so it fails.
    st.cache_resource.clear()
    app = AppTest.from_file(str(path), default_timeout=30).run()
    assert len(app.exception) == 1
    return app.exception[0].message, list(app.exception[0].stack_trace)


@pytest.mark.parametrize("page", sorted(PAGES))
def test_a_failing_page_shows_neither_the_message_nor_the_traceback(
    monkeypatch: pytest.MonkeyPatch, page: str
) -> None:
    message, traceback = failed(monkeypatch, page)

    assert PASSAGE not in message
    assert "redacted" in message
    assert traceback == []


@pytest.mark.parametrize("page", sorted(PAGES))
def test_with_streamlits_default_the_same_check_sees_the_text(
    monkeypatch: pytest.MonkeyPatch, page: str
) -> None:
    monkeypatch.setattr("ui.text.ERROR_DETAILS", "full")

    message, traceback = failed(monkeypatch, page)

    assert PASSAGE in message
    assert traceback
