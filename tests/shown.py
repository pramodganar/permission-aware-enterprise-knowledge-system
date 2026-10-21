"""Reading back what a UI page drew, for the AppTest tests."""

import re
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

PAGE = Path(__file__).resolve().parents[1] / "ui" / "app.py"

_ESCAPED = re.compile(r"\\(.)")


def open_ui(url: str, monkeypatch: pytest.MonkeyPatch) -> AppTest:
    """The UI pointed at the service at ``url``, drawn once.

    The page shares one API client per URL between sessions. Tests run in one
    process and start a service per test, so the shared client is dropped
    first: each test talks only to its own service.
    """
    monkeypatch.setenv("UI_API_URL", url)
    st.cache_resource.clear()
    page = AppTest.from_file(str(PAGE), default_timeout=30)
    page.run()
    assert not page.exception
    return page


def unescape(markdown: str) -> str:
    """The text a label or heading escaped with ``ui.text.literal`` shows."""
    return _ESCAPED.sub(r"\1", markdown)


def everything_shown(page: AppTest) -> str:
    """Every value and label on the page, unescaped, as one string."""
    parts: list[str] = []
    for area in (page.main, page.sidebar):
        for element in area:
            for name in ("value", "label"):
                if hasattr(element, name):
                    parts.append(unescape(str(getattr(element, name))))
    return "\n".join(parts)
