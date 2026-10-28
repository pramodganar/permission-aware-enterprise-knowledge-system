"""Three people using the UI at once against one running service.

Henry gives David access to the restricted HR case file and takes it away
again, while David has the documents page open and Irene watches the audit
trail. Each change shows on the other person's next click, and every step
is in the trail.
"""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from streamlit.testing.v1 import AppTest

from apiclient import PASSWORD, company_app, email
from app.core.config import Settings
from app.models import Document
from liveserver import live_server
from questions import FACTS
from shown import everything_shown, open_ui, unescape
from ui.session import AUDIT, DOCUMENTS, MANAGE

CASE_FILE = "HR Case File 2026-014"
IN_THE_CASE_FILE = next(fact for fact, title in FACTS.items() if title == CASE_FILE)


@pytest.fixture
def service(db: Session, engine: Engine, settings: Settings) -> Iterator[str]:
    with live_server(company_app(db, engine, settings)) as url:
        yield url


def on_page(url: str, monkeypatch: pytest.MonkeyPatch, name: str, page_name: str) -> AppTest:
    page = open_ui(url, monkeypatch)
    page.text_input[0].input(email(name))
    page.text_input[1].input(PASSWORD)
    page.button[0].click().run()
    page.sidebar.radio[0].set_value(page_name).run()
    assert not page.exception
    return page


def listed(page: AppTest) -> list[str]:
    return list(page.dataframe[0].value["Title"]) if page.dataframe else []


def trail(page: AppTest) -> list[tuple[object, ...]]:
    page.run()
    rows = page.dataframe[0].value.to_dict("records")
    return [(r["Who"], r["Action"], r["Outcome"], r["Document"]) for r in rows]


def test_access_given_and_taken_away_shows_on_the_next_click_and_in_the_trail(
    service: str, monkeypatch: pytest.MonkeyPatch, db: Session
) -> None:
    case = db.scalars(select(Document).where(Document.title == CASE_FILE)).one().public_id
    david = on_page(service, monkeypatch, "david", DOCUMENTS)
    henry = on_page(service, monkeypatch, "henry", MANAGE)
    irene = on_page(service, monkeypatch, "irene", AUDIT)

    # Before: David can't see it, and opening it by ID isn't on offer.
    assert CASE_FILE not in listed(david)

    # Henry gives David access.
    henry.selectbox(key="manage_id").set_value(case).run()
    next(t for t in henry.text_input if t.label == "Email").input(email("david"))
    next(b for b in henry.button if b.label == "Grant").click().run()
    assert [unescape(s.value) for s in henry.success] == [
        f"Gave {email('david')} access to document {case}."
    ]

    # David's next click: it is listed, and opens.
    david.run()
    assert CASE_FILE in listed(david)
    david.selectbox(key="documents_open").set_value(case).run()
    assert IN_THE_CASE_FILE in everything_shown(david)

    # Irene sees both.
    seen = trail(irene)
    assert (email("henry"), "grant.given", "done", str(case)) in seen
    assert (email("david"), "document.read", "allowed", str(case)) in seen

    # Henry takes it away.
    henry.button(key=f"revoke {email('david')}").click().run()

    # David's next click: gone from the list and the page.
    david.run()
    assert CASE_FILE not in listed(david)
    assert IN_THE_CASE_FILE not in everything_shown(david)

    seen = trail(irene)
    assert (email("henry"), "grant.revoked", "done", str(case)) in seen
    # Henry managed it throughout without ever reading it.
    assert not [row for row in seen if row[0] == email("henry") and row[1] == "document.read"]
    assert IN_THE_CASE_FILE not in everything_shown(henry)
