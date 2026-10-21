"""The UI's client against the service running over real HTTP.

What the UI will see, as it will see it: every call it makes, as each kind
of person, and every way a call can fail.
"""

from collections.abc import Callable, Iterator
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from apiclient import PASSWORD, company_app, email
from app.core.config import Settings
from app.models import Document
from app.rag.llm import LLMProvider
from fakes import FakeLLM
from liveserver import live_server
from questions import FACTS, MAY_READ
from ui.client import (
    Api,
    Filters,
    InvalidInput,
    NotAllowed,
    NotFound,
    NotSignedIn,
    TooManyAttempts,
    Unavailable,
)

LEAVE = "How many days of annual leave do I get?"
NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
StartService = Callable[..., str]


@pytest.fixture
def start(db: Session, engine: Engine, settings: Settings) -> Iterator[StartService]:
    """Start the service once per test, with the model the test needs."""
    with ExitStack() as stack:

        def run(provider: LLMProvider | None = None) -> str:
            changed = settings.model_copy(update={"retrieval_min_score": -1.0})
            return stack.enter_context(live_server(company_app(db, engine, changed, provider)))

        yield run


@pytest.fixture
def api(start: StartService) -> Iterator[Api]:
    client = Api(start(FakeLLM(["Twenty-five days [S1]."])), clock=lambda: NOW)
    yield client
    client.close()


def sign_in(api: Api, name: str) -> str:
    return api.login(email(name), PASSWORD).token


def document_id(db: Session, title: str) -> int:
    return db.scalars(select(Document.id).where(Document.title == title)).one()


# --- signing in -------------------------------------------------------------------------


def test_signing_in_gives_a_token_and_when_it_stops_working(api: Api) -> None:
    login = api.login(email("david"), PASSWORD)

    assert login.token
    assert login.expires_at == NOW + timedelta(minutes=30)
    assert not login.expired(NOW + timedelta(minutes=29))
    assert login.expired(NOW + timedelta(minutes=30))


@pytest.mark.parametrize(
    ("address", "password"),
    [(email("david"), "wrong"), ("nobody@acmecorp.example", PASSWORD)],
    ids=["wrong password", "unknown email"],
)
def test_a_failed_sign_in_is_not_signed_in_with_the_services_message(
    api: Api, address: str, password: str
) -> None:
    with pytest.raises(NotSignedIn) as raised:
        api.login(address, password)

    assert (raised.value.status, raised.value.code) == (401, "authentication_failed")
    assert raised.value.message == "Authentication failed."
    assert raised.value.request_id


def test_too_many_failed_sign_ins_say_how_long_to_wait(api: Api) -> None:
    for _ in range(5):
        with pytest.raises(NotSignedIn):
            api.login(email("frank"), "wrong")

    with pytest.raises(TooManyAttempts) as raised:
        api.login(email("frank"), PASSWORD)

    assert raised.value.retry_after == 900


def test_who_am_i_says_what_the_ui_may_offer(api: Api) -> None:
    david, henry = api.me(sign_in(api, "david")), api.me(sign_in(api, "henry"))

    assert (david.full_name, david.department.code, david.roles) == (
        "David Okafor",
        "sales",
        ["employee"],
    )
    assert not david.can("documents:manage")
    assert henry.can("documents:manage")
    assert not henry.can("documents:read:general")


@pytest.mark.parametrize("token", ["", "  ", "not-a-token"], ids=["empty", "blank", "garbage"])
def test_a_bad_token_is_not_signed_in(api: Api, token: str) -> None:
    with pytest.raises(NotSignedIn):
        api.me(token)


# --- reading ----------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(MAY_READ))
def test_each_person_lists_exactly_what_they_may_read(api: Api, name: str) -> None:
    listed = api.documents(sign_in(api, name))

    assert {d.title for d in listed.documents} == MAY_READ[name]
    assert listed.total == len(MAY_READ[name])


def test_the_list_can_be_filtered_and_paged(api: Api) -> None:
    grace = sign_in(api, "grace")

    hr = api.documents(grace, department="hr", classification="departmental")
    page = api.documents(grace, limit=5, offset=15)

    assert {d.title for d in hr.documents} == {
        "Performance Improvement Plan Process",
        "Salary Bands 2026",
    }
    assert (page.total, len(page.documents)) == (16, 1)


def test_a_document_opens_with_its_text_or_is_not_found(api: Api, db: Session) -> None:
    carol, david = sign_in(api, "carol"), sign_in(api, "david")
    case_file = document_id(db, "HR Case File 2026-014")

    opened = api.document(carol, case_file)
    with pytest.raises(NotFound) as forbidden:
        api.document(david, case_file)
    with pytest.raises(NotFound) as missing:
        api.document(david, 999_999)

    assert "E-2291" in opened.content
    assert forbidden.value.message == missing.value.message == "Not found."


def test_search_and_ask_answer_from_what_the_person_may_read(api: Api, db: Session) -> None:
    david = sign_in(api, "david")

    found = api.search(david, LEAVE)
    answer = api.ask(david, LEAVE)

    assert found.message is None
    assert {p.document.title for p in found.results} <= MAY_READ["david"]
    assert answer.answered
    assert answer.answer == "Twenty-five days [S1]."
    assert [c.label for c in answer.citations] == ["S1"]
    assert answer.citations[0].document.title in MAY_READ["david"]
    hidden = [fact for fact, title in FACTS.items() if title not in MAY_READ["david"]]
    received = " ".join([*(p.text for p in found.results), *(c.text for c in answer.citations)])
    assert not [fact for fact in hidden if fact in received]


def test_filters_reach_the_service(api: Api, db: Session) -> None:
    emma = sign_in(api, "emma")
    falcon = document_id(db, "Project Falcon Acquisition Paper")

    found = api.search(
        emma, "Which company is the acquisition target?", Filters(document_ids=(falcon,))
    )
    nothing = api.search(
        sign_in(api, "david"), LEAVE, Filters(departments=("hr",), classifications=("restricted",))
    )

    assert {p.document.title for p in found.results} == {"Project Falcon Acquisition Paper"}
    assert nothing.results == []
    assert nothing.message == "No information available to answer this question."


@pytest.mark.parametrize(
    ("question", "message"),
    [("", None), ("x " * 501, "the question is longer than 1000 characters")],
    ids=["empty", "too long"],
)
def test_a_question_the_service_refuses_says_why(
    api: Api, question: str, message: str | None
) -> None:
    with pytest.raises(InvalidInput) as raised:
        api.search(sign_in(api, "david"), question)

    if message is None:
        assert raised.value.fields == ("body.question",)
    else:
        assert raised.value.message == message


def test_without_a_model_asking_is_unavailable_and_searching_works(start: StartService) -> None:
    with Api(start(None)) as api:
        david = sign_in(api, "david")

        with pytest.raises(Unavailable) as raised:
            api.ask(david, LEAVE)

        assert raised.value.message == (
            "Answers are switched off on this service. Search still works."
        )
        assert api.search(david, LEAVE).results


def test_a_service_that_is_not_running_is_unavailable() -> None:
    with Api("http://127.0.0.1:9") as api, pytest.raises(Unavailable) as raised:
        api.login(email("david"), PASSWORD)

    assert raised.value.code == "unreachable"


# --- managing and reviewing -------------------------------------------------------------


def test_the_admin_can_do_every_managing_call(api: Api, db: Session) -> None:
    henry = sign_in(api, "henry")
    bands = document_id(db, "Salary Bands 2026")
    soon = datetime.now(UTC) + timedelta(days=7)

    new = api.upload(
        henry,
        filename="fair.txt",
        data=b"Booth crates ship from dock 4.",
        title="Fair Note",
        department="sales",
        classification="restricted",
        readers=(email("david"),),
    )
    replaced = api.new_version(
        henry, new.id, filename="fair.txt", data=b"Dock 5 now.", title="Fair Note v2"
    )
    relabelled = api.relabel(henry, replaced.id, classification="departmental")
    granted = api.grant(henry, bands, email("david"), expires_at=soon)
    api.revoke(henry, bands, email("david"))
    api.delete(henry, relabelled.id)

    assert ([g.email for g in new.grants], new.in_index) == ([email("david")], False)
    assert (replaced.version, replaced.supersedes_id) == (2, new.id)
    assert relabelled.classification == "departmental"
    assert granted.grants[0].expires_at == soon
    assert api.managed(henry, bands).grants == []
    assert api.managed(henry, relabelled.id).status == "deleted"


@pytest.mark.parametrize("name", ["david", "grace", "irene"])
def test_managing_is_not_allowed_for_anyone_else(api: Api, name: str) -> None:
    with pytest.raises(NotAllowed):
        api.upload(
            sign_in(api, name),
            filename="x.txt",
            data=b"x",
            title="X",
            department="sales",
            classification="general",
        )


def test_a_refused_upload_says_which_rule(api: Api) -> None:
    with pytest.raises(InvalidInput) as raised:
        api.upload(
            sign_in(api, "henry"),
            filename="x.txt",
            data=b"A note.",
            title="X",
            department="sales",
            classification="restricted",
        )

    assert raised.value.message == "a restricted document needs at least one named reader"


def test_the_reviewer_reads_the_trail_and_nobody_else_can(api: Api) -> None:
    david, irene = sign_in(api, "david"), sign_in(api, "irene")
    api.ask(david, LEAVE)

    page = api.audit(irene, actor=email("david"), action="question.asked")

    assert page.total == 1
    entry = page.records[0]
    assert (entry.actor and entry.actor.email, entry.question, entry.outcome) == (
        email("david"),
        LEAVE,
        "answered",
    )
    with pytest.raises(NotAllowed):
        api.audit(david)


# --- the token --------------------------------------------------------------------------


def test_the_token_only_ever_travels_in_the_authorization_header(start: StartService) -> None:
    sent: list[httpx.Request] = []
    base_url = start(FakeLLM(["Twenty-five days [S1]."]))
    with Api(http=httpx.Client(base_url=base_url, event_hooks={"request": [sent.append]})) as api:
        token = api.login(email("henry"), PASSWORD).token
        api.me(token)
        api.documents(token, department="sales")
        api.managed(token, 1)
    calls = [str(request.url) for request in sent]
    bodies = [request.content for request in sent[1:]]

    assert not [url for url in calls if token in url]
    assert not [body for body in bodies if token.encode() in body]
    assert all(r.headers["Authorization"] == f"Bearer {token}" for r in sent[1:])
