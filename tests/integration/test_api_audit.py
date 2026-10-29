"""The audit trail through the API (FR-13, FR-14, SR-15, SR-16, decision record 0006).

Every question, search, document opened, failed login and change is
recorded with who did it; only Irene, the security reviewer, can read the
records; and when a record can't be stored, the request fails rather than
going unrecorded.
"""

import json
import logging
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, inspect, select, text
from sqlalchemy.orm import Session

from apiclient import MISSING, PASSWORD, company_client, email, headers
from app.api.components import Components
from app.core.config import Settings
from app.main import create_app
from app.models import AuditRecord, Chunk, Document, User
from app.rag.llm import LLMProvider, LLMUnavailableError
from app.retrieval.index import build_index
from fakes import FakeEmbedder, FakeLLM
from logcheck import everything_logged
from questions import MAY_READ

LEAVE = "How many days of annual leave do I get?"
MakeClient = Callable[..., TestClient]


@pytest.fixture
def make_client(db: Session, engine: Engine, settings: Settings) -> Iterator[MakeClient]:
    with ExitStack() as stack:

        def make(provider: LLMProvider | None = None) -> TestClient:
            changed = settings.model_copy(
                update={"retrieval_min_score": -1.0, "llm_max_retries": 0}
            )
            return stack.enter_context(company_client(db, engine, changed, provider))

        yield make


@pytest.fixture
def client(make_client: MakeClient) -> TestClient:
    return make_client(FakeLLM(["Twenty-five days [S1]."]))


def trail(client: TestClient, **params: str | int) -> list[dict[str, Any]]:
    response = client.get("/audit", headers=headers(client, "irene"), params=params)
    assert response.status_code == 200, response.text
    records: list[dict[str, Any]] = response.json()["records"]
    return records


def latest(client: TestClient, action: str) -> dict[str, Any]:
    return trail(client, action=action, limit=1)[0]


def user_id(db: Session, name: str) -> int:
    return db.scalars(select(User.id).where(User.email == email(name))).one()


def document_id(db: Session, title: str) -> UUID:
    """The ID the service gives the document: its public one."""
    return db.scalars(select(Document).where(Document.title == title)).one().public_id


def titles_of_chunks(db: Session, chunk_ids: list[int]) -> set[str]:
    return set(
        db.scalars(
            select(Document.title)
            .join(Chunk, Chunk.document_id == Document.id)
            .where(Chunk.id.in_(chunk_ids))
        )
    )


def count(db: Session) -> int:
    db.expire_all()
    return db.scalar(select(func.count()).select_from(AuditRecord)) or 0


def break_the_audit_store(engine: Engine) -> None:
    with engine.begin() as connection:
        # The seals first: they refer to the records.
        connection.execute(text("DROP TABLE audit_seals"))
        connection.execute(text("DROP TABLE audit_records"))


# --- who may read it --------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(set(MAY_READ) - {"irene"}))
def test_only_the_security_reviewer_can_read_the_trail(client: TestClient, name: str) -> None:
    response = client.get("/audit", headers=headers(client, name))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


def test_without_a_token_the_trail_is_401(client: TestClient) -> None:
    assert client.get("/audit").status_code == 401


def test_reading_the_trail_is_itself_recorded(client: TestClient, db: Session) -> None:
    client.get("/audit", headers=headers(client, "irene"), params={"actor": email("david")})

    read = latest(client, "audit.read")
    assert read["actor"] == {"id": user_id(db, "irene"), "email": email("irene")}
    assert read["details"]["filters"]["actor_email"] == email("david")


@pytest.mark.parametrize("title", ["Leave Policy", None])
def test_a_read_for_one_document_records_the_id_as_it_was_asked_for(
    client: TestClient, db: Session, title: str | None
) -> None:
    # The ID the reviewer gave, whether or not a document has it.
    asked = str(MISSING if title is None else document_id(db, title))
    client.get("/audit", headers=headers(client, "irene"), params={"document_id": asked})

    read = latest(client, "audit.read")
    assert read["details"]["filters"]["document_id"] == asked


def test_the_trail_must_not_be_stored(client: TestClient) -> None:
    response = client.get("/audit", headers=headers(client, "irene"))

    assert response.headers["Cache-Control"] == "no-store"


# --- questions and searches -------------------------------------------------------------


def test_a_question_is_recorded_with_who_asked_what_they_were_given_and_what_came_of_it(
    client: TestClient, db: Session
) -> None:
    response = client.post("/ask", headers=headers(client, "david"), json={"question": LEAVE})

    entry = latest(client, "question.asked")
    assert entry["actor"]["email"] == email("david")
    assert entry["question"] == LEAVE
    assert entry["outcome"] == "answered"
    assert entry["request_id"] == response.headers["X-Request-ID"]
    assert entry["chunk_ids"]
    assert titles_of_chunks(db, entry["chunk_ids"]) <= MAY_READ["david"]
    cited = response.json()["citations"][0]["document"]["id"]
    assert entry["details"]["cited_documents"] == [cited]
    assert entry["details"]["model"] == "fake-model"


def test_a_question_with_nothing_readable_is_recorded_with_its_reason(
    client: TestClient,
) -> None:
    client.post("/ask", headers=headers(client, "henry"), json={"question": LEAVE})

    entry = latest(client, "question.asked")
    assert (entry["outcome"], entry["details"]["reason"]) == ("no_information", "nothing_relevant")
    assert entry["chunk_ids"] == []


def test_a_question_the_model_failed_on_is_recorded_with_what_was_sent(
    make_client: MakeClient, db: Session
) -> None:
    client = make_client(FakeLLM([LLMUnavailableError("timed out")]))

    response = client.post("/ask", headers=headers(client, "carol"), json={"question": LEAVE})

    assert response.status_code == 503
    entry = latest(client, "question.asked")
    assert entry["outcome"] == "failed"
    assert entry["details"]["error"] == "LLMUnavailableError"
    assert entry["chunk_ids"]
    assert titles_of_chunks(db, entry["chunk_ids"]) <= MAY_READ["carol"]


def test_a_search_is_recorded_with_the_chunks_it_returned(client: TestClient, db: Session) -> None:
    hr = {"departments": ["hr"]}
    response = client.post(
        "/search", headers=headers(client, "carol"), json={"question": LEAVE, "filters": hr}
    )

    entry = latest(client, "search")
    results = response.json()["results"]
    assert entry["outcome"] == "found"
    assert len(entry["chunk_ids"]) == len(results)
    assert titles_of_chunks(db, entry["chunk_ids"]) == {r["document"]["title"] for r in results}
    assert entry["details"]["filters"] == {"departments": ["hr"]}


def test_a_question_that_can_not_be_searched_with_is_not_recorded(
    client: TestClient, db: Session
) -> None:
    auth = headers(client, "david")
    before = count(db)

    client.post("/ask", headers=auth, json={"question": "?!?"})
    client.post("/search", headers=auth, json={"question": ""})

    assert count(db) == before


def test_the_question_goes_to_the_trail_and_not_to_the_application_log(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    question = "What is the hotel limit when travelling to Zanzibar?"

    with caplog.at_level(logging.DEBUG, logger="app"):
        client.post("/ask", headers=headers(client, "david"), json={"question": question})

    assert "Zanzibar" not in everything_logged(caplog)
    assert latest(client, "question.asked")["question"] == question


# --- documents opened -------------------------------------------------------------------


def test_opening_a_document_is_recorded_and_so_is_every_refusal(
    client: TestClient, db: Session
) -> None:
    auth = headers(client, "david")
    leave, case_file = document_id(db, "Leave Policy"), document_id(db, "HR Case File 2026-014")

    responses = [client.get(f"/documents/{i}", headers=auth) for i in (leave, case_file, MISSING)]

    entries = trail(client, action="document.read")[:3][::-1]
    assert [(e["outcome"], e["document_id"]) for e in entries] == [
        ("allowed", str(leave)),
        ("denied", str(case_file)),
        ("not_found", None),
    ]
    # As asked for: no document has it, so there is nothing else to name.
    assert entries[2]["details"] == {"requested_id": str(MISSING)}
    # The responses to the last two were identical; only the trail tells them apart.
    assert responses[1].json()["error"]["message"] == responses[2].json()["error"]["message"]
    assert [e["request_id"] for e in entries] == [r.headers["X-Request-ID"] for r in responses]


# --- logins -----------------------------------------------------------------------------


def test_a_failed_login_is_recorded_without_what_was_typed(client: TestClient, db: Session) -> None:
    typed_password = "Tr0ub4dor&3-typed"
    client.post("/auth/login", json={"email": email("david"), "password": typed_password})
    client.post("/auth/login", json={"email": "typo.d4vid@acmecorp.example", "password": "x"})

    entries = trail(client, action="login.failed")[:2][::-1]
    assert [(e["actor"], e["details"]["reason"]) for e in entries] == [
        ({"id": user_id(db, "david"), "email": email("david")}, "wrong password"),
        (None, "unknown email"),
    ]
    everything = json.dumps(trail(client))
    assert typed_password not in everything
    assert "typo.d4vid" not in everything


def test_a_successful_login_is_recorded_as_one(client: TestClient, db: Session) -> None:
    before = count(db)

    client.post("/auth/login", json={"email": email("david"), "password": PASSWORD})

    assert count(db) == before + 1
    entry = db.scalars(select(AuditRecord).order_by(AuditRecord.id.desc())).first()
    assert entry is not None
    assert (entry.action, entry.outcome, entry.actor_id) == (
        "login.succeeded",
        "allowed",
        user_id(db, "david"),
    )


# --- changes ----------------------------------------------------------------------------


def test_every_change_an_admin_makes_is_recorded_with_its_document(
    client: TestClient, db: Session
) -> None:
    henry = headers(client, "henry")
    bands = document_id(db, "Salary Bands 2026")
    leave = document_id(db, "Leave Policy")

    uploaded = client.post(
        "/admin/documents",
        headers=henry,
        files={"file": ("note.txt", b"Booth crates ship from dock 4.", "text/plain")},
        data={"title": "Fair Note", "department": "sales", "classification": "general"},
    ).json()
    replaced = client.post(
        f"/admin/documents/{uploaded['id']}/versions",
        headers=henry,
        files={"file": ("note.txt", b"Booth crates ship from dock 5.", "text/plain")},
    ).json()
    client.patch(
        f"/admin/documents/{leave}", headers=henry, json={"classification": "departmental"}
    )
    client.put(f"/admin/documents/{bands}/grants", headers=henry, json={"email": email("david")})
    client.delete(f"/admin/documents/{bands}/grants/{email('david')}", headers=henry)
    client.delete(f"/admin/documents/{replaced['id']}", headers=henry)

    entries = trail(client, actor=email("henry"), limit=6)[::-1]
    assert [(e["action"], e["document_id"]) for e in entries] == [
        ("document.ingested", uploaded["id"]),
        ("document.replaced", replaced["id"]),
        ("document.relabelled", str(leave)),
        ("grant.given", str(bands)),
        ("grant.revoked", str(bands)),
        ("document.deleted", replaced["id"]),
    ]
    assert entries[1]["details"]["replaced_document_id"] == uploaded["id"]
    assert entries[2]["details"]["old_classification"] == "general"
    assert entries[3]["details"]["grantee_id"] == user_id(db, "david")


def test_loading_the_company_recorded_each_document(client: TestClient, db: Session) -> None:
    entries = trail(client, action="document.ingested", limit=100)

    assert len(entries) == 17
    assert {e["actor"]["email"] for e in entries} == {email("henry")}


# --- reading it -------------------------------------------------------------------------


def test_the_trail_can_be_narrowed_and_paged(client: TestClient, db: Session) -> None:
    david = headers(client, "david")
    for _ in range(3):
        client.post("/search", headers=david, json={"question": LEAVE})
    client.post("/search", headers=headers(client, "carol"), json={"question": LEAVE})
    leave = document_id(db, "Leave Policy")
    client.get(f"/documents/{leave}", headers=david)
    client.get(f"/documents/{document_id(db, 'Code of Conduct')}", headers=david)

    searches = trail(client, actor=email("david"), action="search")
    assert len(searches) == 3
    assert [e["id"] for e in searches] == sorted((e["id"] for e in searches), reverse=True)
    assert [
        e["document_id"] for e in trail(client, document_id=str(leave), action="document.read")
    ] == [str(leave)]
    assert trail(client, document_id=str(MISSING)) == []
    page = client.get(
        "/audit", headers=headers(client, "irene"), params={"action": "search", "limit": 2}
    ).json()
    assert (page["total"], len(page["records"])) == (4, 2)
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert trail(client, since=future) == []
    assert len(trail(client, action="search", until=future)) == 4
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    assert trail(client, action="search", until=past) == []


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"since": "2026-10-01T00:00:00"}, "query.since"),
        ({"action": "document.stolen"}, "query.action"),
        ({"limit": 501}, "query.limit"),
        ({"document_id": 0}, "query.document_id"),
    ],
)
def test_a_bad_query_is_invalid_input(
    client: TestClient, params: dict[str, str | int], field: str
) -> None:
    response = client.get("/audit", headers=headers(client, "irene"), params=params)

    assert response.status_code == 422
    assert response.json()["error"]["fields"] == [field]


# --- if a record can't be stored, the request fails -------------------------------------


def test_without_an_audit_store_nothing_is_answered_found_or_opened(
    make_client: MakeClient, engine: Engine, db: Session
) -> None:
    llm = FakeLLM(["Twenty-five days [S1]."])
    client = make_client(llm)
    david = headers(client, "david")
    leave = document_id(db, "Leave Policy")
    break_the_audit_store(engine)

    asked = client.post("/ask", headers=david, json={"question": LEAVE})
    searched = client.post("/search", headers=david, json={"question": LEAVE})
    opened = client.get(f"/documents/{leave}", headers=david)

    for response in (asked, searched, opened):
        assert response.status_code == 503, response.text
        assert response.json()["error"]["code"] == "service_unavailable"
    assert "Twenty-five" not in asked.text
    # Not even sent: the record of what the model would be sent came first.
    assert llm.prompts == []
    assert "Leave" not in searched.text
    assert "annual leave" not in opened.text.lower()


def test_without_an_audit_store_a_failed_login_is_refused_with_503(
    client: TestClient, engine: Engine
) -> None:
    break_the_audit_store(engine)

    response = client.post("/auth/login", json={"email": email("david"), "password": "wrong"})

    assert response.status_code == 503


def test_without_an_audit_store_a_good_login_gets_no_token(
    client: TestClient, engine: Engine
) -> None:
    break_the_audit_store(engine)

    response = client.post("/auth/login", json={"email": email("david"), "password": PASSWORD})

    assert response.status_code == 503
    assert "access_token" not in response.text


def test_without_an_audit_store_a_refusal_is_a_503(client: TestClient, engine: Engine) -> None:
    david = headers(client, "david")
    break_the_audit_store(engine)

    response = client.get("/audit", headers=david)

    assert response.status_code == 503


def test_without_an_audit_store_a_change_is_not_made(
    client: TestClient, engine: Engine, db: Session
) -> None:
    henry = headers(client, "henry")
    # Signed in first: a login is recorded too, so it would fail once the store is gone.
    frank = headers(client, "frank")
    leave = document_id(db, "Leave Policy")
    break_the_audit_store(engine)

    deleted = client.delete(f"/admin/documents/{leave}", headers=henry)

    assert deleted.status_code >= 500
    db.expire_all()
    leave_policy = db.scalars(select(Document).where(Document.title == "Leave Policy")).one()
    assert leave_policy.public_id == leave
    assert leave_policy.status == "active"
    # Lists aren't audited, so Frank's still works, and still has it.
    listed = client.get("/documents", headers=frank).json()["documents"]
    assert "Leave Policy" in {document["title"] for document in listed}


def test_startup_adds_the_audit_trail_to_a_database_seeded_before_it_existed(
    db: Session, engine: Engine, settings: Settings
) -> None:
    break_the_audit_store(engine)
    embedder = FakeEmbedder()
    components = Components(embedder=embedder, index=build_index(db, embedder), provider=None)

    with TestClient(create_app(settings, engine=engine, components=components)):
        pass

    assert {"audit_records", "audit_seals"} <= set(inspect(engine).get_table_names())
    # And with the triggers that keep both append-only.
    with engine.connect() as connection:
        triggers = connection.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        ).scalars()
        assert set(triggers) == {
            "audit_records_no_update",
            "audit_records_no_delete",
            "audit_seals_no_update",
            "audit_seals_no_delete",
        }


def test_signing_out_is_recorded_with_who_did_it(client: TestClient, db: Session) -> None:
    signed_out = client.post("/auth/logout", headers=headers(client, "david"))

    entry = latest(client, "signed.out")
    assert (entry["actor"]["email"], entry["outcome"]) == (email("david"), "done")
    assert entry["request_id"] == signed_out.headers["X-Request-ID"]


def test_without_an_audit_store_signing_out_withdraws_nothing(
    client: TestClient, engine: Engine
) -> None:
    david = headers(client, "david")
    break_the_audit_store(engine)

    refused = client.post("/auth/logout", headers=david)

    assert refused.status_code == 503
    # Not withdrawn, so not unrecorded: the token still works.
    assert client.get("/auth/me", headers=david).status_code == 200
