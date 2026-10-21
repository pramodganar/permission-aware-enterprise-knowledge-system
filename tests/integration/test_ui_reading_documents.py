"""The UI's documents page, run headless against the service over real HTTP."""

from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from streamlit.testing.v1 import AppTest

from apiclient import PASSWORD, company_app, email
from app.core.config import Settings
from app.models import AuditAction, AuditRecord, Department, Document, User
from liveserver import live_server
from questions import FACTS, MAY_READ
from shown import everything_shown, open_ui, unescape
from ui import documents
from ui.client import Api
from ui.documents import UNAVAILABLE
from ui.session import ASK, DOCUMENTS, HOME

FALCON = "Project Falcon Acquisition Paper"


@pytest.fixture
def service(db: Session, engine: Engine, settings: Settings) -> Iterator[str]:
    with live_server(company_app(db, engine, settings)) as url:
        yield url


Open = Callable[[str], AppTest]


@pytest.fixture
def open_as(service: str, monkeypatch: pytest.MonkeyPatch) -> Open:
    def open_page(name: str) -> AppTest:
        page = open_ui(service, monkeypatch)
        page.text_input[0].input(email(name))
        page.text_input[1].input(PASSWORD)
        page.button[0].click().run()
        if DOCUMENTS in page.sidebar.radio[0].options:
            page.sidebar.radio[0].set_value(DOCUMENTS).run()
        assert not page.exception
        return page

    return open_page


def listed(page: AppTest) -> list[str]:
    if not page.dataframe:
        return []
    return list(page.dataframe[0].value["Title"])


def all_pages(page: AppTest) -> list[str]:
    titles = listed(page)
    pager = [s for s in page.selectbox if s.label == "Page"]
    if pager:
        for number in pager[0].options[1:]:
            page.selectbox(key="documents_page").set_value(int(number)).run()
            titles += listed(page)
    return titles


def choose(page: AppTest, key: str, value: object) -> AppTest:
    page.selectbox(key=key).set_value(value).run()
    assert not page.exception
    return page


def document_id(db: Session, title: str) -> int:
    return db.scalars(select(Document.id).where(Document.title == title)).one()


def open_document(page: AppTest, db: Session, title: str) -> AppTest:
    return choose(page, "documents_open", document_id(db, title))


# --- the list ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(set(MAY_READ) - {"henry", "irene"}))
def test_the_list_is_exactly_what_each_person_may_read(open_as: Open, name: str) -> None:
    page = open_as(name)

    titles = listed(page)

    assert sorted(titles) == sorted(MAY_READ[name])
    assert titles == sorted(titles)


@pytest.mark.parametrize("name", ["henry", "irene"])
def test_the_page_is_not_offered_to_someone_who_may_read_nothing(open_as: Open, name: str) -> None:
    page = open_as(name)

    assert page.sidebar.radio[0].options == [HOME]


def test_a_long_list_comes_a_page_at_a_time(open_as: Open, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(documents, "PAGE_SIZE", 5)
    page = open_as("grace")

    assert len(listed(page)) == 5
    assert page.selectbox(key="documents_page").options == ["1", "2", "3", "4"]
    titles = all_pages(page)
    assert titles == sorted(MAY_READ["grace"])


def test_a_filter_that_leaves_fewer_pages_goes_to_the_last_one(
    open_as: Open, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(documents, "PAGE_SIZE", 2)
    page = open_as("grace")
    choose(page, "documents_page", 8)

    choose(page, "documents_department", "finance")

    assert listed(page)
    assert not page.exception


def test_filters_narrow_the_list(open_as: Open, db: Session) -> None:
    page = open_as("grace")
    finance = set(
        db.scalars(
            select(Document.title)
            .join(Department, Document.department_id == Department.id)
            .where(Department.code == "finance")
        )
    )

    choose(page, "documents_department", "finance")
    assert set(listed(page)) == finance & MAY_READ["grace"]

    choose(page, "documents_classification", "general")
    assert set(listed(page)) == {"Travel and Expense Policy"}


def test_the_filter_choices_come_from_what_the_person_may_read(open_as: Open) -> None:
    page = open_as("david")

    departments = page.selectbox(key="documents_department").options

    # David reads Sales documents and the company-wide ones, nothing else.
    assert "Executive" not in departments
    assert departments[0] == "All"


# --- opening a document -----------------------------------------------------------------


def test_opening_a_document_shows_its_whole_text_as_plain_text(open_as: Open, db: Session) -> None:
    page = open_as("david")
    leave = db.scalars(select(Document).where(Document.title == "Leave Policy")).one()

    open_document(page, db, "Leave Policy")

    assert unescape(page.subheader[0].value) == "Leave Policy"
    assert page.text[0].value == leave.content
    assert leave.source_filename in unescape(page.main.caption[-1].value)
    assert all(leave.content not in m.value for m in page.markdown)


def test_the_text_is_fetched_each_time_and_never_kept(open_as: Open, db: Session) -> None:
    page = open_as("david")
    leave = db.scalars(select(Document).where(Document.title == "Leave Policy")).one()
    open_document(page, db, "Leave Policy")

    kept = repr(page.session_state.to_dict())
    assert leave.content[:60] not in kept

    leave.content = "Changed since it was opened."
    db.commit()
    page.run()
    assert page.text[0].value == "Changed since it was opened."


def test_opening_a_document_is_recorded(open_as: Open, db: Session) -> None:
    page = open_as("david")

    open_document(page, db, "Leave Policy")

    david = db.scalars(select(User.id).where(User.email == email("david"))).one()
    reads = db.scalars(
        select(AuditRecord).where(
            AuditRecord.actor_id == david, AuditRecord.action == AuditAction.DOCUMENT_READ
        )
    ).all()
    assert [(r.document_id, r.outcome) for r in reads] == [
        (document_id(db, "Leave Policy"), "allowed")
    ]


@pytest.mark.parametrize("name", sorted(set(MAY_READ) - {"henry", "irene"}))
def test_nothing_the_person_may_not_read_is_shown_on_the_page(
    open_as: Open, db: Session, name: str
) -> None:
    page = open_as(name)

    for title in sorted(MAY_READ[name]):
        open_document(page, db, title)
        shown = everything_shown(page)
        for fact, source in FACTS.items():
            if source not in MAY_READ[name]:
                assert fact not in shown, (title, fact)


def test_a_document_whose_grant_is_revoked_goes_on_the_next_click(
    open_as: Open, db: Session, service: str
) -> None:
    page = open_as("emma")
    open_document(page, db, FALCON)
    assert FACTS_IN[FALCON] in everything_shown(page)

    with Api(service) as api:
        henry = api.login(email("henry"), PASSWORD).token
        api.revoke(henry, document_id(db, FALCON), email("emma"))
    page.run()

    assert FACTS_IN[FALCON] not in everything_shown(page)
    assert FALCON not in listed(page)


def test_a_document_withdrawn_between_listing_and_opening_says_it_is_unavailable(
    open_as: Open, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = open_as("david")
    leave = document_id(db, "Leave Policy")
    # The list still offers it, as it would have a moment before.
    real = Api.document

    def withdrawn(self: Api, token: str, document_id: int) -> object:
        return real(self, token, 10**9 if document_id == leave else document_id)

    monkeypatch.setattr(Api, "document", withdrawn)

    open_document(page, db, "Leave Policy")

    assert [unescape(w.value) for w in page.warning] == [UNAVAILABLE]
    assert not page.text


def test_a_title_with_markdown_in_it_is_shown_as_typed(
    open_as: Open, db: Session, service: str
) -> None:
    planted = "![x](https://evil.example/collect.png) **Fair** Note"
    with Api(service) as api:
        henry = api.login(email("henry"), PASSWORD).token
        api.upload(
            henry,
            filename="fair.txt",
            data=b"Booth crates ship from dock 4.",
            title=planted,
            department="sales",
            classification="general",
        )
    page = open_as("david")

    open_document(page, db, planted)

    heading = page.subheader[0].value
    assert unescape(heading) == planted
    assert "![" not in heading
    assert planted in listed(page)


def test_asking_and_documents_are_offered_together(open_as: Open) -> None:
    page = open_as("david")

    assert page.sidebar.radio[0].options == [HOME, ASK, DOCUMENTS]


FACTS_IN = {source: fact for fact, source in FACTS.items()}
