"""The UI's managing page, run headless against the service over real HTTP.

Henry manages documents without being able to read them, so besides every
action working, the page must never show him a title, a file name or text.
"""

from collections.abc import Iterator
from datetime import UTC, date, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from streamlit.testing.v1 import AppTest

from apiclient import MISSING, PASSWORD, company_app, email
from app.core.config import Settings
from app.models import Document, DocumentGrant, User
from liveserver import live_server
from questions import FACTS
from shown import everything_shown, open_ui, unescape
from ui import managing
from ui.client import Api
from ui.session import AUDIT, DOCUMENTS, HOME, MANAGE

BANDS = "Salary Bands 2026"
CASE_FILE = "HR Case File 2026-014"


@pytest.fixture
def service(db: Session, engine: Engine, settings: Settings) -> Iterator[str]:
    with live_server(company_app(db, engine, settings)) as url:
        yield url


def signed_in(url: str, monkeypatch: pytest.MonkeyPatch, name: str) -> AppTest:
    page = open_ui(url, monkeypatch)
    page.text_input[0].input(email(name))
    page.text_input[1].input(PASSWORD)
    page.button[0].click().run()
    assert not page.exception
    return page


@pytest.fixture
def henry(service: str, monkeypatch: pytest.MonkeyPatch) -> AppTest:
    page = signed_in(service, monkeypatch, "henry")
    page.sidebar.radio[0].set_value(MANAGE).run()
    assert not page.exception
    return page


def document(db: Session, title: str) -> Document:
    db.expire_all()
    return db.scalars(select(Document).where(Document.title == title)).one()


def look_up(page: AppTest, document_id: UUID) -> AppTest:
    # Picked from the list of every document, as an admin does.
    page.selectbox(key="manage_id").set_value(document_id).run()
    assert not page.exception
    return page


def type_into(page: AppTest, label: str, value: str, nth: int = 0) -> None:
    fields = [w for w in [*page.text_input, *page.text_area] if w.label == label]
    fields[nth].input(value)


def press(page: AppTest, label: str) -> AppTest:
    next(b for b in page.button if b.label == label).click().run()
    assert not page.exception
    return page


def facts_shown(page: AppTest) -> dict[str, str]:
    table = page.dataframe[0].value
    return dict(zip(table[""], table["Value"], strict=True))


def success(page: AppTest) -> list[str]:
    return [unescape(s.value) for s in page.success]


def errors(page: AppTest) -> list[str]:
    return [unescape(e.value) for e in page.error]


# --- who is offered it ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "pages"),
    [
        ("henry", [HOME, MANAGE]),
        ("irene", [HOME, AUDIT]),
        ("david", [HOME, "Ask and search", DOCUMENTS]),
        ("grace", [HOME, "Ask and search", DOCUMENTS]),
    ],
)
def test_each_page_is_offered_only_to_whoever_has_its_permission(
    service: str, monkeypatch: pytest.MonkeyPatch, name: str, pages: list[str]
) -> None:
    page = signed_in(service, monkeypatch, name)

    assert page.sidebar.radio[0].options == pages


# --- looking a document up --------------------------------------------------------------


def test_a_document_is_shown_as_it_is_managed_and_never_what_it_says(
    henry: AppTest, db: Session
) -> None:
    case = document(db, CASE_FILE)

    look_up(henry, case.public_id)

    facts = facts_shown(henry)
    assert facts["Classification"] == "restricted"
    assert facts["Department"] == "hr"
    assert facts["Search"] == "finds it"
    # Carol's grant is how she reads it; who holds a grant is the admin's business.
    assert any(t.value == f"{email('carol')}, until revoked" for t in henry.main.text)
    shown = everything_shown(henry)
    assert CASE_FILE not in shown
    assert case.source_filename not in shown
    assert case.content[:40] not in shown
    assert not [fact for fact in FACTS if fact in shown]


def test_a_document_the_service_no_longer_finds_says_so(
    henry: AppTest, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Listed a moment before, then not found when it is fetched.
    real = Api.managed

    def gone(self: Api, token: str, document_id: UUID) -> object:
        return real(self, token, MISSING)

    monkeypatch.setattr(Api, "managed", gone)
    look_up(henry, document(db, "Leave Policy").public_id)

    assert errors(henry) == [managing.NOT_FOUND]
    assert not henry.dataframe


# --- uploading --------------------------------------------------------------------------


def test_uploading_a_document_files_it_and_shows_how_it_is_managed(
    henry: AppTest, db: Session
) -> None:
    henry.file_uploader[0].upload("fair.txt", b"Booth crates ship from dock 4.", "text/plain")
    type_into(henry, "Title", "Trade Fair Logistics")
    type_into(henry, "Department", "sales")
    type_into(henry, "Readers", "david@acmecorp.example\nfrank@acmecorp.example")
    next(s for s in henry.selectbox if s.label == "Classification").set_value("restricted")

    press(henry, "Upload")

    new = document(db, "Trade Fair Logistics")
    assert success(henry) == [f"Uploaded as document {new.public_id}."]
    assert henry.selectbox(key="manage_id").value == new.public_id
    assert facts_shown(henry)["Search"] == "can't find it until the index is rebuilt"
    assert (new.classification, new.department.code) == ("restricted", "sales")
    granted = db.scalars(
        select(User.email)
        .join(DocumentGrant, DocumentGrant.user_id == User.id)
        .where(DocumentGrant.document_id == new.id)
    ).all()
    assert sorted(granted) == [email("david"), email("frank")]
    # The forms start again empty, so pressing Upload again sends nothing,
    # and the title Henry typed isn't shown back to him.
    assert not [u for u in henry.file_uploader if u.value]
    assert "Trade Fair Logistics" not in everything_shown(henry)


def test_an_upload_without_a_file_is_not_sent(henry: AppTest, db: Session) -> None:
    before = db.scalars(select(Document.id)).all()
    type_into(henry, "Title", "Nothing")
    type_into(henry, "Department", "sales")

    press(henry, "Upload")

    assert errors(henry) == [managing.NO_FILE]
    assert db.scalars(select(Document.id)).all() == before


def test_the_service_says_what_is_wrong_with_an_upload(henry: AppTest) -> None:
    henry.file_uploader[0].upload("fair.txt", b"Booth crates.", "text/plain")
    type_into(henry, "Title", "Fair")
    type_into(henry, "Department", "nowhere")

    press(henry, "Upload")

    assert errors(henry) == ["unknown department"]


# --- grants -----------------------------------------------------------------------------


def test_granting_and_revoking_access(henry: AppTest, db: Session) -> None:
    case = document(db, CASE_FILE)
    look_up(henry, case.public_id)
    type_into(henry, "Email", email("david"))
    last_day = date.today() + timedelta(days=7)
    henry.date_input[0].set_value(last_day)

    press(henry, "Grant")

    assert success(henry) == [f"Gave {email('david')} access to document {case.public_id}."]
    grant = db.scalars(
        select(DocumentGrant)
        .join(User, User.id == DocumentGrant.user_id)
        .where(DocumentGrant.document_id == case.id, User.email == email("david"))
    ).one()
    assert grant.expires_at is not None
    assert grant.expires_at.replace(tzinfo=UTC) == managing.until_end_of(last_day)
    assert any(email("david") in t.value for t in henry.main.text)

    henry.button(key=f"revoke {email('david')}").click().run()

    assert success(henry) == [f"Revoked access to document {case.public_id} for {email('david')}."]
    db.expire_all()
    assert (
        db.scalars(
            select(DocumentGrant)
            .join(User, User.id == DocumentGrant.user_id)
            .where(DocumentGrant.document_id == case.id, User.email == email("david"))
        ).first()
        is None
    )


def test_a_grant_the_service_refuses_shows_its_reason(henry: AppTest, db: Session) -> None:
    case = document(db, CASE_FILE)
    look_up(henry, case.public_id)
    # Henry himself: he has no role that reads documents, so a grant would
    # give him nothing, and the service says so.
    type_into(henry, "Email", email("henry"))

    press(henry, "Grant")

    assert errors(henry) == [f"{email('henry')} does not have a role that can read documents"]
    assert not success(henry)


# --- labels, versions, withdrawing ------------------------------------------------------


def test_changing_a_label(henry: AppTest, db: Session) -> None:
    bands = document(db, BANDS)
    look_up(henry, bands.public_id)
    next(s for s in henry.selectbox if s.label == "Classification").set_value("restricted")

    press(henry, "Save labels")

    assert success(henry) == [f"Changed the labels of document {bands.public_id}."]
    assert document(db, BANDS).classification == "restricted"
    assert facts_shown(henry)["Classification"] == "restricted"


def test_saving_unchanged_labels_sends_nothing(henry: AppTest, db: Session) -> None:
    bands = document(db, BANDS)
    look_up(henry, bands.public_id)

    press(henry, "Save labels")

    assert [i.value for i in henry.info] == ["Nothing changed."]
    assert document(db, BANDS).version == bands.version


def test_a_new_version_replaces_the_document_and_is_shown_next(henry: AppTest, db: Session) -> None:
    bands = document(db, BANDS)
    look_up(henry, bands.public_id)
    henry.file_uploader[0].upload("bands.txt", b"Senior engineers: new bands.", "text/plain")

    press(henry, "Upload new version")

    db.expire_all()
    new = db.scalars(select(Document).where(Document.supersedes_id == bands.id)).one()
    assert success(henry) == [
        f"Version {new.version} is document {new.public_id}; it replaces {bands.public_id}."
    ]
    assert henry.selectbox(key="manage_id").value == new.public_id
    assert facts_shown(henry)["Replaces"] == str(bands.public_id)


def test_withdrawing_needs_saying_so_first(henry: AppTest, db: Session) -> None:
    bands = document(db, BANDS)
    look_up(henry, bands.public_id)
    withdraw = next(b for b in henry.button if b.label == "Withdraw document")
    assert withdraw.disabled

    henry.checkbox(key=f"withdraw {bands.public_id}").check().run()
    press(henry, "Withdraw document")

    assert success(henry) == [f"Withdrew document {bands.public_id}."]
    assert document(db, BANDS).status == "deleted"
    assert facts_shown(henry)["Status"] == "deleted"
    assert [i.value for i in henry.info] == [
        "This document has been withdrawn. Nobody can read or find it."
    ]
    assert not [b for b in henry.button if b.label == "Withdraw document"]
