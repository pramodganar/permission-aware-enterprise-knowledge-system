"""The UI's sign-in, run headless against the service over real HTTP."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from streamlit.testing.v1 import AppTest

from apiclient import PASSWORD, company_app, email
from app.core.config import Settings
from app.models import User
from liveserver import live_server
from shown import everything_shown, open_ui, unescape
from ui.client import Login
from ui.session import LOGIN, SESSION_ENDED, SIGN_IN_FAILED, SIGNED_OUT, UNREACHABLE


@pytest.fixture
def service(db: Session, engine: Engine, settings: Settings) -> Iterator[str]:
    with live_server(company_app(db, engine, settings)) as url:
        yield url


def open_page(url: str, monkeypatch: pytest.MonkeyPatch) -> AppTest:
    return open_ui(url, monkeypatch)


@pytest.fixture
def page(service: str, monkeypatch: pytest.MonkeyPatch) -> AppTest:
    return open_page(service, monkeypatch)


def sign_in(page: AppTest, address: str, password: str = PASSWORD) -> AppTest:
    page.text_input[0].input(address)
    page.text_input[1].input(password)
    page.button[0].click().run()
    assert not page.exception
    return page


def test_the_page_starts_at_sign_in_and_says_the_company_is_made_up(page: AppTest) -> None:
    assert page.title[0].value == "AcmeCorp Knowledge Assistant"
    assert [t.label for t in page.text_input] == ["Email", "Password"]
    assert any("fictional" in c.value for c in page.caption)
    assert len(page.sidebar) == 0


def test_signing_in_shows_who_they_are_and_what_they_can_do(page: AppTest) -> None:
    sign_in(page, email("david"))

    assert page.sidebar.subheader[0].value == "David Okafor"
    assert page.sidebar.caption[0].value == "Sales · employee"
    shown = everything_shown(page)
    assert "Ask questions and search the documents you may read." in shown
    assert "Upload and manage" not in shown


def test_neither_the_password_nor_the_token_is_kept_or_shown(page: AppTest) -> None:
    sign_in(page, email("david"))
    login = page.session_state[LOGIN]

    # What the page keeps, and what the widgets themselves hold on to.
    kept = repr(page.session_state.to_dict())
    kept += repr(page.session_state._state.get_widget_states())
    assert PASSWORD not in kept
    assert PASSWORD not in everything_shown(page)
    assert login.token not in everything_shown(page)


@pytest.mark.parametrize(
    ("name", "line"),
    [
        ("henry", "Upload and manage documents. Managing a document doesn't let you read it."),
        ("irene", "Review the audit trail."),
    ],
)
def test_each_role_is_told_what_it_can_do(page: AppTest, name: str, line: str) -> None:
    sign_in(page, email(name))

    shown = everything_shown(page)
    assert line in shown
    assert "Ask questions" not in shown


def test_a_failed_sign_in_keeps_no_trace_of_the_password(page: AppTest) -> None:
    typed = "Typed-Pass-123"

    sign_in(page, email("david"), typed)

    # Blanked at once. The browser's message for this one redraw carried it,
    # as any form submission does; by the next interaction that is gone too.
    assert page.text_input[1].value == ""
    page.run()
    assert typed not in repr(page.session_state.to_dict())
    assert typed not in repr(page.session_state._state.get_widget_states())
    # The email stays, so a typo means typing the password again, not both.
    assert page.text_input[0].value == email("david")


@pytest.mark.parametrize(
    ("address", "password"),
    [(email("david"), "wrong"), ("nobody@acmecorp.example", PASSWORD)],
    ids=["wrong password", "unknown email"],
)
def test_a_failed_sign_in_says_the_same_whatever_was_wrong(
    page: AppTest, address: str, password: str
) -> None:
    sign_in(page, address, password)

    assert [e.value for e in page.error] == [SIGN_IN_FAILED]
    assert [t.label for t in page.text_input] == ["Email", "Password"]


def test_signing_out_goes_back_to_sign_in(page: AppTest) -> None:
    sign_in(page, email("david"))

    page.sidebar.button[0].click().run()

    assert [i.value for i in page.info] == [SIGNED_OUT]
    assert LOGIN not in page.session_state
    assert len(page.sidebar) == 0


def test_a_deactivated_account_is_signed_out_on_its_next_click(page: AppTest, db: Session) -> None:
    sign_in(page, email("david"))
    david = db.scalars(select(User).where(User.email == email("david"))).one()
    david.is_active = False
    db.commit()

    page.run()

    assert [i.value for i in page.info] == [SESSION_ENDED]
    assert LOGIN not in page.session_state


def test_an_expired_session_goes_back_to_sign_in(page: AppTest) -> None:
    sign_in(page, email("david"))
    old = page.session_state[LOGIN]
    page.session_state[LOGIN] = Login(old.token, datetime.now(UTC) - timedelta(seconds=1))

    page.run()

    assert [i.value for i in page.info] == [SESSION_ENDED]


def test_a_service_that_is_not_running_is_explained(monkeypatch: pytest.MonkeyPatch) -> None:
    page = open_page("http://127.0.0.1:9", monkeypatch)

    sign_in(page, email("david"))

    assert [e.value for e in page.error] == [UNREACHABLE]


def test_a_name_with_markdown_in_it_is_shown_as_typed(page: AppTest, db: Session) -> None:
    planted = "![x](https://evil.example/a.png) **David**"
    david = db.scalars(select(User).where(User.email == email("david"))).one()
    david.full_name = planted
    db.commit()

    sign_in(page, email("david"))

    heading = page.sidebar.subheader[0].value
    assert unescape(heading) == planted
    assert "![" not in heading
