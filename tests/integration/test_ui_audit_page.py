"""The UI's audit page, run headless against the service over real HTTP."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session
from streamlit.testing.v1 import AppTest

from apiclient import PASSWORD, company_app, email
from app.core.config import Settings
from app.models import AuditAction, AuditRecord, Document, User
from liveserver import live_server
from shown import open_ui, unescape
from ui import reviewing
from ui.client import Api, NotFound, NotSignedIn
from ui.session import AUDIT


@pytest.fixture
def service(db: Session, engine: Engine, settings: Settings) -> Iterator[str]:
    with live_server(company_app(db, engine, settings)) as url:
        # Some activity to review: a document opened, one refused, a failed login.
        with Api(url) as api:
            david = api.login(email("david"), PASSWORD).token
            leave = db.scalars(select(Document).where(Document.title == "Leave Policy")).one()
            case = db.scalars(
                select(Document).where(Document.title == "HR Case File 2026-014")
            ).one()
            api.document(david, leave.public_id)
            with pytest.raises(NotFound):
                api.document(david, case.public_id)
            with pytest.raises(NotSignedIn):
                api.login(email("frank"), "wrong password")
        yield url


@pytest.fixture
def irene(service: str, monkeypatch: pytest.MonkeyPatch) -> AppTest:
    page = open_ui(service, monkeypatch)
    page.text_input[0].input(email("irene"))
    page.text_input[1].input(PASSWORD)
    page.button[0].click().run()
    page.sidebar.radio[0].set_value(AUDIT).run()
    assert not page.exception
    return page


def table(page: AppTest) -> list[dict[str, object]]:
    if not page.dataframe:
        return []
    records: list[dict[str, object]] = page.dataframe[0].value.to_dict("records")
    return records


def show(page: AppTest, **fields: object) -> AppTest:
    for label, value in fields.items():
        widgets = [
            w
            for w in [*page.text_input, *page.selectbox, *page.number_input, *page.date_input]
            if w.label == label
        ]
        widgets[0].set_value(value)
    next(b for b in page.button if b.label == "Show").click().run()
    assert not page.exception
    return page


def reads_by(db: Session, name: str) -> int:
    db.expire_all()
    who = db.scalars(select(User.id).where(User.email == email(name))).one()
    return (
        db.scalar(
            select(func.count())
            .select_from(AuditRecord)
            .where(AuditRecord.actor_id == who, AuditRecord.action == AuditAction.AUDIT_READ)
        )
        or 0
    )


def test_the_trail_is_shown_newest_first(irene: AppTest, db: Session) -> None:
    rows = table(irene)

    stored = db.scalars(select(AuditRecord).order_by(AuditRecord.id.desc())).all()
    # The newest record is this look at the page, written after reading the rest.
    assert stored[0].action == AuditAction.AUDIT_READ
    assert [r["Action"] for r in rows] == [r.action for r in stored[1:]]
    assert {"document.read", "login.failed", "document.ingested"} <= {r["Action"] for r in rows}
    assert rows == sorted(rows, key=lambda r: str(r["When (UTC)"]), reverse=True)


def test_a_refused_opening_and_a_failed_login_are_there_as_the_service_recorded(
    irene: AppTest,
) -> None:
    rows = table(irene)

    reads = [(r["Who"], r["Outcome"]) for r in rows if r["Action"] == "document.read"]
    assert sorted(reads) == [(email("david"), "allowed"), (email("david"), "denied")]
    failed = [r for r in rows if r["Action"] == "login.failed"]
    assert [r["Who"] for r in failed] == [email("frank")]


def test_filtering_by_who_and_what(irene: AppTest) -> None:
    show(irene, **{"Who (email)": email("david"), "Action": "document.read"})

    rows = table(irene)
    assert len(rows) == 2
    assert {(r["Who"], r["Action"]) for r in rows} == {(email("david"), "document.read")}


def test_filtering_by_who_alone(irene: AppTest) -> None:
    show(irene, **{"Who (email)": email("frank")})

    assert [(r["Who"], r["Action"]) for r in table(irene)] == [(email("frank"), "login.failed")]


def test_filtering_by_document(irene: AppTest, db: Session) -> None:
    leave = db.scalars(select(Document).where(Document.title == "Leave Policy")).one()

    show(irene, **{"Document ID": f"  {leave.public_id}  "})

    rows = table(irene)
    assert [(r["Action"], r["Outcome"]) for r in rows] == [
        ("document.read", "allowed"),
        ("document.ingested", "done"),
    ]
    assert {r["Document"] for r in rows} == {str(leave.public_id)}


def test_a_document_id_that_is_not_one_is_explained_and_nothing_is_fetched(
    irene: AppTest, db: Session
) -> None:
    before = reads_by(db, "irene")
    widgets = [w for w in irene.text_input if w.label == "Document ID"]
    widgets[0].set_value("16")
    next(b for b in irene.button if b.label == "Show").click().run()

    assert [unescape(e.value) for e in irene.error] == [reviewing.NOT_AN_ID]
    assert reads_by(db, "irene") == before


def test_a_window_of_days_in_the_past_shows_nothing(irene: AppTest) -> None:
    today = datetime.now(UTC).date()

    show(irene, From=today - timedelta(days=9), To=today - timedelta(days=2))

    assert not table(irene)
    assert [i.value for i in irene.info] == ["No records match."]


def test_a_window_ending_before_it_starts_is_not_sent(irene: AppTest, db: Session) -> None:
    today = datetime.now(UTC).date()
    before = reads_by(db, "irene")

    show(irene, From=today, To=today - timedelta(days=1))

    assert [unescape(e.value) for e in irene.error] == [reviewing.BACKWARDS]
    assert reads_by(db, "irene") == before


def test_every_look_at_the_page_is_itself_recorded(irene: AppTest, db: Session) -> None:
    before = reads_by(db, "irene")

    irene.run()
    irene.run()

    assert reads_by(db, "irene") == before + 2
    assert table(irene)[0]["Action"] == "audit.read"


def test_a_long_trail_comes_a_page_at_a_time(
    irene: AppTest, monkeypatch: pytest.MonkeyPatch, db: Session
) -> None:
    monkeypatch.setattr(reviewing, "PAGE_SIZE", 2)
    irene.run()

    assert len(table(irene)) == 2
    pager = irene.selectbox(key="audit_page")
    pager.set_value(len(pager.options)).run()

    last = table(irene)
    assert 1 <= len(last) <= 2
    # The oldest record is the first thing that happened: loading the company.
    assert last[-1]["Action"] == "document.ingested"


def test_a_question_is_shown_exactly_as_it_was_asked(
    irene: AppTest, service: str, db: Session
) -> None:
    asked = "What does ![x](https://evil.example/a.png) **mean**?"
    with Api(service) as api:
        api.search(api.login(email("david"), PASSWORD).token, asked)

    irene.run()

    searches = [r for r in table(irene) if r["Action"] == "search"]
    assert [r["Question"] for r in searches] == [asked]
