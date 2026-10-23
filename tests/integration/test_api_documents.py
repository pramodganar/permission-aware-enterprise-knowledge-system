"""Listing and opening documents through the API (FR-5).

Who may read what comes from the table in ``questions.py``, written from
docs/personas.md, and not from the authorization code these routes call.
"""

import logging
from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from apiclient import company_client, email, headers
from app.core.config import Settings
from app.core.database import utc_now
from app.models import Department, Document, DocumentGrant, DocumentStatus, Role, RoleCode, User
from questions import ALL_DEPARTMENTAL, DEPARTMENT_DOCUMENTS, GENERAL, MAY_READ

PEOPLE = sorted(MAY_READ)
# No document has this ID.
MISSING = 999_999


@pytest.fixture
def client(db: Session, engine: Engine, settings: Settings) -> Iterator[TestClient]:
    with company_client(db, engine, settings) as client:
        yield client


def titles(response: Response) -> set[str]:
    assert response.status_code == 200, response.text
    return {document["title"] for document in response.json()["documents"]}


def listed(client: TestClient, name: str, **params: str) -> set[str]:
    return titles(client.get("/documents", headers=headers(client, name), params=params))


def document_id(db: Session, title: str) -> int:
    return db.scalars(select(Document.id).where(Document.title == title)).one()


def user(db: Session, name: str) -> User:
    return db.scalars(select(User).where(User.email == email(name))).one()


def without_request_id(response: Response) -> dict[str, dict[str, object]]:
    body: dict[str, dict[str, object]] = response.json()
    body["error"].pop("request_id")
    return body


NOT_FOUND = {"error": {"code": "not_found", "message": "Not found."}}


# --- the list ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", PEOPLE)
def test_each_person_lists_exactly_what_they_may_read(client: TestClient, name: str) -> None:
    response = client.get("/documents", headers=headers(client, name))

    assert titles(response) == MAY_READ[name]
    assert response.json()["total"] == len(MAY_READ[name])


def test_the_list_is_in_title_order(client: TestClient) -> None:
    documents = client.get("/documents", headers=headers(client, "grace")).json()["documents"]

    names = [document["title"] for document in documents]
    assert names == sorted(names)


def test_a_list_entry_has_no_text_grantees_uploader_or_earlier_version(
    client: TestClient,
) -> None:
    documents = client.get("/documents", headers=headers(client, "carol")).json()["documents"]
    case_file = next(d for d in documents if d["title"] == "HR Case File 2026-014")

    assert set(case_file) == {
        "id",
        "title",
        "department",
        "classification",
        "file_type",
        "version",
        "updated_at",
    }
    assert case_file["department"] == {"code": "hr", "name": "Human Resources"}
    assert case_file["classification"] == "restricted"


def test_the_list_must_not_be_stored(client: TestClient) -> None:
    response = client.get("/documents", headers=headers(client, "david"))

    assert response.headers["Cache-Control"] == "no-store"


# --- opening one ------------------------------------------------------------------------


@pytest.mark.parametrize("name", PEOPLE)
def test_each_person_can_open_exactly_what_they_may_read(
    client: TestClient, db: Session, name: str
) -> None:
    auth = headers(client, name)
    missing = client.get(f"/documents/{MISSING}", headers=auth)
    every_document = db.scalars(select(Document)).all()
    assert len(every_document) == 17

    for document in every_document:
        response = client.get(f"/documents/{document.id}", headers=auth)
        if document.title in MAY_READ[name]:
            assert response.status_code == 200, document.title
            assert response.json()["title"] == document.title
        else:
            # Indistinguishable from a document that doesn't exist.
            assert response.status_code == 404, document.title
            assert without_request_id(response) == without_request_id(missing) == NOT_FOUND
            assert sorted(response.headers) == sorted(missing.headers)


def test_an_opened_document_has_its_text_and_file_details_but_no_grantees(
    client: TestClient, db: Session
) -> None:
    case_file = document_id(db, "HR Case File 2026-014")

    response = client.get(f"/documents/{case_file}", headers=headers(client, "carol"))

    assert response.status_code == 200
    body = response.json()
    assert "E-2291" in body["content"]
    assert set(body) == {
        "id",
        "title",
        "department",
        "classification",
        "file_type",
        "version",
        "updated_at",
        "source_filename",
        "size_bytes",
        "created_at",
        "content",
    }
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("path", ["abc", "0", "-1", "1.5", str(2**63), "1e3"])
def test_an_id_that_is_not_a_valid_document_id_is_invalid_input_not_a_crash(
    client: TestClient, path: str
) -> None:
    response = client.get(f"/documents/{path}", headers=headers(client, "david"))

    assert response.status_code == 422
    assert response.json()["error"]["fields"] == ["path.document_id"]


def test_the_largest_possible_id_is_simply_not_found(client: TestClient) -> None:
    response = client.get(f"/documents/{2**63 - 1}", headers=headers(client, "david"))

    assert without_request_id(response) == NOT_FOUND


@pytest.mark.parametrize("path", ["/documents", "/documents/1"])
@pytest.mark.parametrize("auth", [{}, {"Authorization": "Bearer not-a-token"}], ids=["none", "bad"])
def test_both_routes_need_a_valid_token(
    client: TestClient, path: str, auth: dict[str, str]
) -> None:
    response = client.get(path, headers=auth)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_failed"


# --- changes take effect on the next request --------------------------------------------


@pytest.mark.parametrize("status", [DocumentStatus.SUPERSEDED, DocumentStatus.DELETED])
def test_a_replaced_or_deleted_document_disappears_and_looks_missing(
    client: TestClient, db: Session, status: DocumentStatus
) -> None:
    auth = headers(client, "david")
    leave = db.scalars(select(Document).where(Document.title == "Leave Policy")).one()
    leave.status = status
    db.commit()

    assert "Leave Policy" not in titles(client.get("/documents", headers=auth))
    response = client.get(f"/documents/{leave.id}", headers=auth)
    assert without_request_id(response) == NOT_FOUND


def test_an_expired_grant_stops_working(client: TestClient, db: Session) -> None:
    auth = headers(client, "emma")
    falcon = document_id(db, "Project Falcon Acquisition Paper")
    assert client.get(f"/documents/{falcon}", headers=auth).status_code == 200

    grant = db.scalars(
        select(DocumentGrant).where(
            DocumentGrant.document_id == falcon, DocumentGrant.user_id == user(db, "emma").id
        )
    ).one()
    grant.expires_at = utc_now() - timedelta(seconds=1)
    db.commit()

    assert titles(client.get("/documents", headers=auth)) == MAY_READ["emma"] - {
        "Project Falcon Acquisition Paper"
    }
    assert without_request_id(client.get(f"/documents/{falcon}", headers=auth)) == NOT_FOUND


def test_a_promotion_applies_to_the_same_token_but_not_to_restricted_documents(
    client: TestClient, db: Session
) -> None:
    auth = headers(client, "david")
    david = user(db, "david")
    david.roles.append(db.scalars(select(Role).where(Role.code == RoleCode.EXECUTIVE)).one())
    db.commit()

    assert titles(client.get("/documents", headers=auth)) == GENERAL | ALL_DEPARTMENTAL


def test_moving_department_moves_the_list_with_the_same_token(
    client: TestClient, db: Session
) -> None:
    auth = headers(client, "alice")
    alice = user(db, "alice")
    alice.department = db.scalars(select(Department).where(Department.code == "finance")).one()
    db.commit()

    expected = GENERAL | DEPARTMENT_DOCUMENTS["finance"]
    assert titles(client.get("/documents", headers=auth)) == expected


# --- filters only narrow ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "params", "expected"),
    [
        ("grace", {"department": "hr"}, {"Leave Policy"} | DEPARTMENT_DOCUMENTS["hr"]),
        ("grace", {"department": " HR "}, {"Leave Policy"} | DEPARTMENT_DOCUMENTS["hr"]),
        ("david", {"department": "hr"}, {"Leave Policy"}),
        ("david", {"department": "no-such-department"}, set()),
        ("emma", {"department": "executive"}, {"Project Falcon Acquisition Paper"}),
        ("david", {"classification": "restricted"}, set()),
        ("carol", {"classification": "restricted"}, {"HR Case File 2026-014"}),
        ("grace", {"classification": "restricted"}, {"Project Falcon Acquisition Paper"}),
        ("bob", {"classification": "departmental"}, DEPARTMENT_DOCUMENTS["finance"]),
        ("carol", {"department": "hr", "classification": "general"}, {"Leave Policy"}),
        ("henry", {"department": "hr"}, set()),
    ],
)
def test_a_filter_only_removes_documents_from_what_the_person_may_read(
    client: TestClient, name: str, params: dict[str, str], expected: set[str]
) -> None:
    found = listed(client, name, **params)

    assert found == expected
    assert found <= MAY_READ[name]


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"classification": "secret"}, "query.classification"),
        ({"department": "x" * 51}, "query.department"),
        ({"limit": 0}, "query.limit"),
        ({"limit": 101}, "query.limit"),
        ({"offset": -1}, "query.offset"),
        ({"offset": 2**63}, "query.offset"),
    ],
)
def test_a_bad_filter_or_page_is_invalid_input(
    client: TestClient, params: dict[str, str | int], field: str
) -> None:
    response = client.get("/documents", headers=headers(client, "david"), params=params)

    assert response.status_code == 422
    assert response.json()["error"]["fields"] == [field]


def test_pages_split_the_list_and_the_total_counts_every_page(client: TestClient) -> None:
    auth = headers(client, "grace")
    everything = client.get("/documents", headers=auth).json()["documents"]

    pages = [
        client.get("/documents", headers=auth, params={"limit": 5, "offset": offset}).json()
        for offset in range(0, 25, 5)
    ]

    assert [page["total"] for page in pages] == [len(MAY_READ["grace"])] * 5
    assert [d for page in pages for d in page["documents"]] == everything
    assert [len(page["documents"]) for page in pages] == [5, 5, 5, 1, 0]


# --- what is logged ---------------------------------------------------------------------


def test_a_denied_lookup_and_a_missing_one_are_each_logged_once_and_no_title_is_logged(
    client: TestClient, db: Session, caplog: pytest.LogCaptureFixture
) -> None:
    auth = headers(client, "david")
    case_file = document_id(db, "HR Case File 2026-014")

    with caplog.at_level(logging.INFO):
        client.get("/documents", headers=auth)
        client.get(f"/documents/{case_file}", headers=auth)
        client.get(f"/documents/{MISSING}", headers=auth)

    refused = [
        (r.document_id, r.exists)  # type: ignore[attr-defined]
        for r in caplog.records
        if r.message == "document not returned"
    ]
    assert refused == [(case_file, True), (MISSING, False)]
    service = "\n".join(str(vars(r)) for r in caplog.records if r.name.startswith("app."))
    every_title = {title for readable in MAY_READ.values() for title in readable}
    assert every_title, "nothing to look for"
    assert not [title for title in every_title if title in service]
