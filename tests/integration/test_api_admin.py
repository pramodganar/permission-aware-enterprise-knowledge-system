"""Managing documents through the API (FR-2, FR-4).

Henry is the knowledge admin. He can upload, replace, relabel, delete and
grant, and he can't read any of it. Everyone else is refused every route,
and the company is checked to be unchanged afterwards.
"""

import itertools
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from apiclient import company_client, email, headers
from app.core.config import Settings
from app.models import Document, DocumentGrant, Role, RoleCode, User
from files import make_docx
from logcheck import everything_logged
from questions import MAY_READ

# Above the largest company file (39 KB), small enough to test cheaply.
UPLOAD_LIMIT = 50_000
_numbers = itertools.count(1)


@pytest.fixture
def client(db: Session, engine: Engine, settings: Settings) -> Iterator[TestClient]:
    limited = settings.model_copy(update={"max_upload_bytes": UPLOAD_LIMIT})
    with company_client(db, engine, limited) as client:
        yield client


@pytest.fixture
def henry(client: TestClient) -> dict[str, str]:
    return headers(client, "henry")


def note(words: str = "") -> bytes:
    """A text file no other test has uploaded."""
    return (
        f"Sales note {next(_numbers)}. Booth crates for the Lisbon trade fair ship from dock 4. "
        f"{words}"
    ).encode()


def upload(
    client: TestClient,
    auth: dict[str, str],
    *,
    data: bytes | None = None,
    filename: str = "fair-logistics.txt",
    title: str = "Trade Fair Logistics",
    department: str = "sales",
    classification: str = "departmental",
    readers: list[str] | None = None,
) -> Response:
    form: dict[str, Any] = {
        "title": title,
        "department": department,
        "classification": classification,
    }
    if readers is not None:
        form["readers"] = readers
    return client.post(
        "/admin/documents",
        headers=auth,
        files={"file": (filename, data if data is not None else note(), "text/plain")},
        data=form,
    )


def uploaded(client: TestClient, auth: dict[str, str], **kwargs: Any) -> dict[str, Any]:
    response = upload(client, auth, **kwargs)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def titles_for(client: TestClient, name: str) -> set[str]:
    documents = client.get("/documents", headers=headers(client, name)).json()["documents"]
    return {document["title"] for document in documents}


def document_id(db: Session, title: str) -> int:
    return db.scalars(select(Document.id).where(Document.title == title)).one()


def state(db: Session) -> list[tuple[Any, ...]]:
    """Everything a manage route could change."""
    db.expire_all()
    documents = db.scalars(select(Document).order_by(Document.id)).all()
    return [
        (
            d.id,
            d.status,
            d.version,
            d.classification,
            d.department_id,
            sorted((g.user_id, g.expires_at) for g in d.grants),
        )
        for d in documents
    ]


# --- who may manage ---------------------------------------------------------------------


def calls(client: TestClient) -> list[tuple[str, str, dict[str, Any]]]:
    form = {
        "files": {"file": ("x.txt", note(), "text/plain")},
        "data": {"title": "T", "department": "sales", "classification": "general"},
    }
    return [
        ("POST", "/admin/documents", form),
        ("GET", "/admin/documents/1", {}),
        ("POST", "/admin/documents/1/versions", {"files": form["files"]}),
        ("PATCH", "/admin/documents/1", {"json": {"classification": "restricted"}}),
        ("DELETE", "/admin/documents/1", {}),
        ("PUT", "/admin/documents/1/grants", {"json": {"email": email("david")}}),
        ("DELETE", f"/admin/documents/16/grants/{email('carol')}", {}),
    ]


@pytest.mark.parametrize("name", sorted(set(MAY_READ) - {"henry"}))
def test_nobody_but_the_knowledge_admin_can_manage_and_nothing_changes(
    client: TestClient, db: Session, name: str
) -> None:
    auth = headers(client, name)
    before = state(db)

    for method, path, extra in calls(client):
        response = client.request(method, path, headers=auth, **extra)
        assert response.status_code == 403, (method, path, response.text)
        assert response.json()["error"]["code"] == "permission_denied"

    assert state(db) == before


def test_without_a_token_every_route_is_401_and_nothing_changes(
    client: TestClient, db: Session
) -> None:
    before = state(db)

    for method, path, extra in calls(client):
        assert client.request(method, path, **extra).status_code == 401, (method, path)

    assert state(db) == before


@pytest.mark.parametrize(("name", "status"), [("david", 403), (None, 401)])
def test_refusal_comes_before_the_body_is_checked(
    client: TestClient, name: str | None, status: int
) -> None:
    auth = headers(client, name) if name else {}

    response = client.post("/admin/documents", headers=auth, data={"nonsense": "1"})

    assert response.status_code == status


# --- uploading --------------------------------------------------------------------------


def test_an_upload_is_readable_at_once_by_the_right_people_and_not_by_the_admin(
    client: TestClient, henry: dict[str, str]
) -> None:
    body = uploaded(client, henry)

    assert body["version"] == 1
    assert body["status"] == "active"
    assert (body["department"], body["classification"]) == ("sales", "departmental")
    assert body["chunks"] >= 1
    assert body["grants"] == []
    assert "Trade Fair Logistics" in titles_for(client, "david")
    assert "Trade Fair Logistics" in titles_for(client, "grace")
    assert "Trade Fair Logistics" not in titles_for(client, "alice")
    # Managing it is not reading it.
    assert titles_for(client, "henry") == set()
    assert client.get(f"/documents/{body['id']}", headers=henry).status_code == 404


def test_what_the_admin_sees_has_no_title_file_name_or_text(
    client: TestClient, henry: dict[str, str]
) -> None:
    response = upload(client, henry, data=note("Marker words zebra quartz."))

    assert set(response.json()) == {
        "id",
        "version",
        "status",
        "department",
        "classification",
        "file_type",
        "size_bytes",
        "chunks",
        "supersedes_id",
        "grants",
        "in_index",
    }
    for secret in ("Trade Fair Logistics", "fair-logistics", "zebra"):
        assert secret not in response.text


def test_a_new_upload_is_not_in_the_index_until_it_is_rebuilt(
    client: TestClient, db: Session, henry: dict[str, str]
) -> None:
    new = uploaded(client, henry)
    seeded = document_id(db, "Leave Policy")

    assert new["in_index"] is False
    assert client.get(f"/admin/documents/{seeded}", headers=henry).json()["in_index"] is True


def test_a_word_file_is_accepted(client: TestClient, henry: dict[str, str]) -> None:
    data = make_docx(["Booth crates for the Porto fair ship from dock 9."])

    body = uploaded(client, henry, data=data, filename="porto.docx", title="Porto Fair")

    assert body["file_type"] == ".docx"


def test_a_restricted_upload_is_read_only_by_its_named_readers(
    client: TestClient, henry: dict[str, str]
) -> None:
    body = uploaded(
        client,
        henry,
        title="Sales Dispute Note",
        classification="restricted",
        readers=[email("david"), email("bob")],
    )

    assert [grant["email"] for grant in body["grants"]] == [email("bob"), email("david")]
    for name in MAY_READ:
        assert ("Sales Dispute Note" in titles_for(client, name)) == (name in {"bob", "david"})


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"classification": "restricted"}, "a restricted document needs at least one named reader"),
        ({"classification": "secret"}, "classification must be one of"),
        ({"department": "marketing"}, "unknown department"),
        ({"title": "   "}, "title is required"),
        ({"filename": "notes.exe"}, "unsupported file type"),
        ({"data": b""}, "the file is empty"),
        ({"data": b"x" * (UPLOAD_LIMIT + 1)}, f"larger than the limit of {UPLOAD_LIMIT} bytes"),
        ({"readers": ["nobody@acmecorp.example"]}, "no active user with the email"),
        ({"readers": [email("henry")]}, "does not have a role that can read documents"),
        ({"readers": [email("david"), email("david")]}, "listed more than once"),
    ],
)
def test_an_upload_that_can_not_be_stored_says_why_and_stores_nothing(
    client: TestClient, db: Session, henry: dict[str, str], changes: dict[str, Any], message: str
) -> None:
    before = state(db)

    response = upload(client, henry, **changes)

    assert response.status_code == 422, response.text
    assert message in response.json()["error"]["message"]
    assert state(db) == before


def test_the_same_file_can_not_be_stored_twice(client: TestClient, henry: dict[str, str]) -> None:
    data = note()
    first = uploaded(client, henry, data=data)

    again = upload(client, henry, data=data, title="Another Title")

    assert again.status_code == 422
    expected = f"this file is already stored as document {first['id']}"
    assert again.json()["error"]["message"] == expected


@pytest.mark.parametrize(
    "missing", ["title", "department", "classification"], ids=lambda field: field
)
def test_an_upload_needs_every_label(
    client: TestClient, henry: dict[str, str], missing: str
) -> None:
    form = {"title": "T", "department": "sales", "classification": "general"}
    del form[missing]

    response = client.post(
        "/admin/documents",
        headers=henry,
        files={"file": ("x.txt", note(), "text/plain")},
        data=form,
    )

    assert response.status_code == 422
    assert response.json()["error"]["fields"] == [f"body.{missing}"]


# --- the size of a request --------------------------------------------------------------


def test_a_request_far_over_the_upload_limit_is_refused_before_it_is_read(
    client: TestClient, db: Session, henry: dict[str, str]
) -> None:
    before = state(db)

    response = upload(client, henry, data=b"x" * (UPLOAD_LIMIT + 70 * 1024))

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert response.headers["X-Request-ID"] == response.json()["error"]["request_id"]
    assert state(db) == before


def test_a_body_without_a_declared_length_is_refused(
    client: TestClient, henry: dict[str, str]
) -> None:
    def chunks() -> Iterator[bytes]:
        yield b'{"question": '
        yield b'"leave?"}'

    response = client.post(
        "/search", headers={**henry, "Content-Type": "application/json"}, content=chunks()
    )

    assert response.status_code == 411
    assert response.json()["error"]["code"] == "length_required"


# --- replacing --------------------------------------------------------------------------


def test_a_new_version_replaces_the_old_one_for_readers_and_keeps_its_audience(
    client: TestClient, db: Session, henry: dict[str, str]
) -> None:
    case_file = document_id(db, "HR Case File 2026-014")
    carol = headers(client, "carol")

    response = client.post(
        f"/admin/documents/{case_file}/versions",
        headers=henry,
        files={"file": ("case.txt", b"HR case 2026-014, update: plan extended.", "text/plain")},
    )

    assert response.status_code == 201, response.text
    new = response.json()
    assert (new["version"], new["supersedes_id"]) == (2, case_file)
    assert (new["classification"], new["department"]) == ("restricted", "hr")
    assert [grant["email"] for grant in new["grants"]] == [email("carol")]
    assert client.get(f"/documents/{case_file}", headers=carol).status_code == 404
    opened = client.get(f"/documents/{new['id']}", headers=carol).json()
    assert opened["title"] == "HR Case File 2026-014"
    assert opened["content"] == "HR case 2026-014, update: plan extended."
    assert titles_for(client, "david") == MAY_READ["david"]
    old = client.get(f"/admin/documents/{case_file}", headers=henry).json()
    assert old["status"] == "superseded"


def test_a_new_version_can_carry_a_new_title(
    client: TestClient, db: Session, henry: dict[str, str]
) -> None:
    leave = document_id(db, "Leave Policy")

    client.post(
        f"/admin/documents/{leave}/versions",
        headers=henry,
        files={"file": ("leave.txt", b"Leave: 26 days from 2027.", "text/plain")},
        data={"title": "Leave Policy 2027"},
    )

    assert "Leave Policy 2027" in titles_for(client, "frank")
    assert "Leave Policy" not in titles_for(client, "frank")


@pytest.mark.parametrize(
    ("target", "status", "code"),
    [("deleted", 422, "invalid_input"), ("missing", 404, "not_found")],
)
def test_only_a_current_document_can_be_replaced(
    client: TestClient, db: Session, henry: dict[str, str], target: str, status: int, code: str
) -> None:
    leave = document_id(db, "Leave Policy")
    if target == "deleted":
        client.delete(f"/admin/documents/{leave}", headers=henry)
    path_id = leave if target == "deleted" else 999_999

    response = client.post(
        f"/admin/documents/{path_id}/versions",
        headers=henry,
        files={"file": ("leave.txt", b"Leave: 26 days.", "text/plain")},
    )

    assert response.status_code == status
    assert response.json()["error"]["code"] == code


# --- deleting ---------------------------------------------------------------------------


def test_a_deleted_document_is_gone_for_everyone_at_once(
    client: TestClient, db: Session, henry: dict[str, str]
) -> None:
    leave = document_id(db, "Leave Policy")
    david = headers(client, "david")

    response = client.delete(f"/admin/documents/{leave}", headers=henry)

    assert response.status_code == 204
    assert response.content == b""
    assert "Leave Policy" not in titles_for(client, "david")
    assert client.get(f"/documents/{leave}", headers=david).status_code == 404
    search = client.post(
        "/search", headers=david, json={"question": "How many days of annual leave?"}
    )
    assert "Leave Policy" not in {r["document"]["title"] for r in search.json()["results"]}
    assert client.get(f"/admin/documents/{leave}", headers=henry).json()["status"] == "deleted"
    assert client.delete(f"/admin/documents/{leave}", headers=henry).status_code == 422


# --- labels -----------------------------------------------------------------------------


def test_relabelling_moves_a_document_to_another_audience_at_once(
    client: TestClient, db: Session, henry: dict[str, str]
) -> None:
    leave = document_id(db, "Leave Policy")

    response = client.patch(
        f"/admin/documents/{leave}",
        headers=henry,
        json={"classification": "departmental", "department": "sales"},
    )

    assert response.status_code == 200
    assert (response.json()["classification"], response.json()["department"]) == (
        "departmental",
        "sales",
    )
    assert "Leave Policy" in titles_for(client, "david")
    assert "Leave Policy" not in titles_for(client, "carol")


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({}, 422),
        ({"classification": "secret"}, 422),
        ({"department": "marketing"}, 422),
        ({"owner": "grace"}, 422),
    ],
)
def test_a_relabel_that_can_not_be_done_changes_nothing(
    client: TestClient, db: Session, henry: dict[str, str], body: dict[str, str], status: int
) -> None:
    before = state(db)

    response = client.patch("/admin/documents/1", headers=henry, json=body)

    assert response.status_code == status
    assert state(db) == before


# --- grants -----------------------------------------------------------------------------


def test_a_grant_lets_one_person_in_and_revoking_it_takes_them_out(
    client: TestClient, db: Session, henry: dict[str, str]
) -> None:
    bands = document_id(db, "Salary Bands 2026")

    granted = client.put(
        f"/admin/documents/{bands}/grants", headers=henry, json={"email": email("david")}
    )

    assert granted.status_code == 200
    assert [g["email"] for g in granted.json()["grants"]] == [email("david")]
    assert "Salary Bands 2026" in titles_for(client, "david")
    assert "Salary Bands 2026" not in titles_for(client, "alice")

    revoked = client.delete(f"/admin/documents/{bands}/grants/{email('david')}", headers=henry)

    assert revoked.status_code == 204
    assert "Salary Bands 2026" not in titles_for(client, "david")
    again = client.delete(f"/admin/documents/{bands}/grants/{email('david')}", headers=henry)
    assert again.status_code == 404


def test_a_grant_can_expire_and_granting_again_replaces_the_expiry(
    client: TestClient, db: Session, henry: dict[str, str]
) -> None:
    bands = document_id(db, "Salary Bands 2026")
    soon = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    later = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    path = f"/admin/documents/{bands}/grants"

    client.put(path, headers=henry, json={"email": email("david"), "expires_at": soon})
    response = client.put(path, headers=henry, json={"email": email("david"), "expires_at": later})

    grants = response.json()["grants"]
    assert len(grants) == 1
    assert datetime.fromisoformat(grants[0]["expires_at"]) == datetime.fromisoformat(later)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"email": "nobody@acmecorp.example"}, "no active user with the email"),
        ({"email": email("irene")}, "does not have a role that can read documents"),
        (
            {"email": email("david"), "expires_at": "2020-01-01T00:00:00+00:00"},
            "the expiry time must be in the future",
        ),
        (
            {"email": email("david"), "expires_at": "2030-01-01T00:00:00"},
            "the expiry time must include a timezone",
        ),
    ],
)
def test_a_grant_that_can_not_be_given_says_why_and_changes_nothing(
    client: TestClient, db: Session, henry: dict[str, str], body: dict[str, str], message: str
) -> None:
    before = state(db)

    response = client.put("/admin/documents/1/grants", headers=henry, json=body)

    assert response.status_code == 422
    assert message in response.json()["error"]["message"]
    assert state(db) == before


def test_an_admin_who_can_also_read_can_not_grant_themselves(
    client: TestClient, db: Session, henry: dict[str, str]
) -> None:
    user = db.scalars(select(User).where(User.email == email("henry"))).one()
    user.roles.append(db.scalars(select(Role).where(Role.code == RoleCode.EMPLOYEE)).one())
    db.commit()
    bands = document_id(db, "Salary Bands 2026")
    before = state(db)

    response = client.put(
        f"/admin/documents/{bands}/grants", headers=henry, json={"email": email("henry")}
    )

    assert response.status_code == 403
    assert state(db) == before
    assert db.scalars(select(DocumentGrant).where(DocumentGrant.user_id == user.id)).all() == []


# --- what is logged ---------------------------------------------------------------------


def test_managing_logs_ids_and_labels_never_titles_file_names_or_text(
    client: TestClient, db: Session, henry: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app"):
        new = uploaded(client, henry, data=note("Marker words zebra quartz."))
        client.post(
            f"/admin/documents/{new['id']}/versions",
            headers=henry,
            files={"file": ("fair-v2.txt", note("Second marker walrus."), "text/plain")},
        )

    logged = everything_logged(caplog)
    assert "document ingested" in logged
    assert "document replaced" in logged
    for secret in ("Trade Fair Logistics", "fair-logistics", "fair-v2", "zebra", "walrus"):
        assert secret not in logged


def test_the_admin_view_of_a_missing_document_is_not_found(
    client: TestClient, henry: dict[str, str]
) -> None:
    response = client.get("/admin/documents/999999", headers=henry)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
