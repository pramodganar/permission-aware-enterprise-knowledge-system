"""Every route records what it should, and only that (FR-13, SR-15, SR-16, threat T39).

``RECORDED`` says, for every route the service serves, what one call to it
must leave in the audit trail, or why it leaves nothing. A route missing
from it fails the tests, so a new route can't be added without deciding.

Each route is then called three ways, and the records that call left are
found by its request ID:

- by someone allowed to: exactly the records the table says, by them;
- by someone refused with 403: one ``action.denied``, naming the route;
- with no token: nothing. There is nobody to record, and anyone could fill
  the trail with them. They are in the application log.

Every record any of this writes is sealed, and the trail checks as intact.
"""

import itertools
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from routes import INTERACTIVE_DOCS, routes_of
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from apiclient import PASSWORD, company_client, email, headers
from app.audit.seals import check_trail, seal_key
from app.core.config import Settings
from app.models import AuditRecord, Document, User
from fakes import FakeLLM

QUESTION = {"question": "How many days of annual leave do I get?"}
_numbers = itertools.count(1)


@dataclass(frozen=True)
class Call:
    method: str
    # "{Title}" stands for that document's ID.
    path: str
    json: Any = None
    upload: bool = False


@dataclass(frozen=True)
class Recorded:
    # Who makes the call; None for the call that logs in.
    who: str | None
    call: Call
    # What the call must record, in order. Empty only with a reason.
    actions: tuple[str, ...] = ()
    reason: str = ""
    # A call made first, so this one has something to work on.
    before: Call | None = None
    # The permission a refused person lacks, for routes that need one.
    permission: str | None = None


GRANT = Call("PUT", "/admin/documents/{HR Case File 2026-014}/grants", {"email": email("david")})

RECORDED: dict[tuple[str, str], Recorded] = {
    ("GET", "/health"): Recorded(
        None, Call("GET", "/health"), reason="Needs no token, and says only that the service is up."
    ),
    ("POST", "/auth/login"): Recorded(
        None,
        Call("POST", "/auth/login", {"email": email("david"), "password": PASSWORD}),
        ("login.succeeded",),
    ),
    ("GET", "/auth/me"): Recorded(
        "david", Call("GET", "/auth/me"), reason="The caller's own account, and nothing else."
    ),
    ("GET", "/documents"): Recorded(
        "david",
        Call("GET", "/documents"),
        reason="Titles the person may read. Opening one, searching and asking are recorded.",
    ),
    ("GET", "/documents/{document_id}"): Recorded(
        "david", Call("GET", "/documents/{Leave Policy}"), ("document.read",)
    ),
    ("POST", "/search"): Recorded("david", Call("POST", "/search", QUESTION), ("search",)),
    ("POST", "/ask"): Recorded(
        "david", Call("POST", "/ask", QUESTION), ("question.sent", "question.asked")
    ),
    ("POST", "/admin/documents"): Recorded(
        "henry",
        Call("POST", "/admin/documents", upload=True),
        ("document.ingested",),
        permission="documents:manage",
    ),
    ("GET", "/admin/documents/{document_id}"): Recorded(
        "henry",
        Call("GET", "/admin/documents/{Leave Policy}"),
        reason="Labels and grants, never content. Every change to them is recorded.",
        permission="documents:manage",
    ),
    ("POST", "/admin/documents/{document_id}/versions"): Recorded(
        "henry",
        Call("POST", "/admin/documents/{Code of Conduct}/versions", upload=True),
        ("document.replaced",),
        permission="documents:manage",
    ),
    ("PATCH", "/admin/documents/{document_id}"): Recorded(
        "henry",
        Call("PATCH", "/admin/documents/{Salary Bands 2026}", {"classification": "restricted"}),
        ("document.relabelled",),
        permission="documents:manage",
    ),
    ("DELETE", "/admin/documents/{document_id}"): Recorded(
        "henry",
        Call("DELETE", "/admin/documents/{Discount Approval Policy}"),
        ("document.deleted",),
        permission="documents:manage",
    ),
    ("PUT", "/admin/documents/{document_id}/grants"): Recorded(
        "henry", GRANT, ("grant.given",), permission="documents:manage"
    ),
    ("DELETE", "/admin/documents/{document_id}/grants/{email}"): Recorded(
        "henry",
        Call("DELETE", "/admin/documents/{HR Case File 2026-014}/grants/" + email("david")),
        ("grant.revoked",),
        before=GRANT,
        permission="documents:manage",
    ),
    ("GET", "/audit"): Recorded(
        "irene", Call("GET", "/audit"), ("audit.read",), permission="audit:read"
    ),
}
PROTECTED = [route for route, r in RECORDED.items() if r.who is not None]
NEED_PERMISSION = [route for route, r in RECORDED.items() if r.permission is not None]


@pytest.fixture
def client(db: Session, engine: Engine, settings: Settings) -> Iterator[TestClient]:
    with company_client(db, engine, settings, FakeLLM(["Twenty-five days [S1]."])) as client:
        yield client


def send(client: TestClient, db: Session, call: Call, auth: dict[str, str]) -> tuple[Response, str]:
    """Make the call with a request ID of its own, and return both."""
    request_id = f"audit-walk-{next(_numbers)}"
    path = re.sub(
        r"\{([^}]+)\}",
        lambda found: str(db.scalars(select(Document.id).where(Document.title == found[1])).one()),
        call.path,
    )
    sent = {**auth, "X-Request-ID": request_id}
    if call.upload:
        response = client.request(
            call.method,
            path,
            headers=sent,
            files={"file": ("note.txt", f"Booth note {request_id}.".encode(), "text/plain")},
            data={"title": "Booth Note", "department": "sales", "classification": "general"},
        )
    else:
        response = client.request(call.method, path, headers=sent, json=call.json)
    return response, request_id


def left_by(db: Session, request_id: str) -> list[AuditRecord]:
    db.expire_all()
    found = select(AuditRecord).where(AuditRecord.request_id == request_id)
    return list(db.scalars(found.order_by(AuditRecord.id)))


def person(db: Session, name: str) -> int:
    return db.scalars(select(User.id).where(User.email == email(name))).one()


def test_every_route_has_a_decided_record(client: TestClient) -> None:
    served = set(routes_of(client.app))  # type: ignore[arg-type]

    assert served - INTERACTIVE_DOCS == set(RECORDED), "a route was added or removed"
    for route, recorded in RECORDED.items():
        assert bool(recorded.actions) != bool(recorded.reason), route


@pytest.mark.parametrize("route", sorted(RECORDED), ids=" ".join)
def test_a_call_records_what_the_table_says_by_whoever_made_it(
    client: TestClient, db: Session, settings: Settings, route: tuple[str, str]
) -> None:
    recorded = RECORDED[route]
    auth = {} if recorded.who is None else headers(client, recorded.who)
    if recorded.before is not None:
        assert send(client, db, recorded.before, auth)[0].status_code < 300

    response, request_id = send(client, db, recorded.call, auth)

    assert response.status_code < 300, response.text
    records = left_by(db, request_id)
    assert tuple(r.action for r in records) == recorded.actions
    actor = person(db, recorded.who or "david")
    assert all(r.actor_id == actor for r in records)
    trail = check_trail(db, seal_key(settings))
    assert trail.intact, trail.problems
    assert trail.sealed == trail.records


@pytest.mark.parametrize("route", NEED_PERMISSION, ids=" ".join)
def test_a_refused_call_records_one_denial_and_nothing_else(
    client: TestClient, db: Session, route: tuple[str, str]
) -> None:
    recorded = RECORDED[route]

    response, request_id = send(client, db, recorded.call, headers(client, "david"))

    assert response.status_code == 403
    [denied] = left_by(db, request_id)
    assert (denied.action, denied.outcome, denied.actor_id) == (
        "action.denied",
        "denied",
        person(db, "david"),
    )
    assert denied.details["route"] == " ".join(route)
    assert denied.details["permission"] == recorded.permission


@pytest.mark.parametrize("route", PROTECTED, ids=" ".join)
def test_a_call_without_a_token_records_nothing(
    client: TestClient, db: Session, route: tuple[str, str]
) -> None:
    response, request_id = send(client, db, RECORDED[route].call, {})

    assert response.status_code == 401
    assert left_by(db, request_id) == []


def test_giving_yourself_access_is_recorded_as_a_denial_with_the_document(
    client: TestClient, db: Session
) -> None:
    henry = db.scalars(select(User).where(User.email == email("henry"))).one()
    employee = db.scalars(select(User).where(User.email == email("alice"))).one().roles[0]
    henry.roles.append(employee)
    db.commit()
    auth = headers(client, "henry")
    relabel = Call(
        "PATCH", "/admin/documents/{HR Case File 2026-014}", {"classification": "general"}
    )
    grant = Call("PUT", "/admin/documents/{Salary Bands 2026}/grants", {"email": email("henry")})

    for call, title in ((relabel, "HR Case File 2026-014"), (grant, "Salary Bands 2026")):
        response, request_id = send(client, db, call, auth)
        assert response.status_code == 403
        [denied] = left_by(db, request_id)
        assert (denied.action, denied.actor_id) == ("action.denied", henry.id)
        assert "themselves" in denied.details["reason"] or "could read" in denied.details["reason"]
        if call is relabel:
            assert (
                denied.document_id
                == db.scalars(select(Document.id).where(Document.title == title)).one()
            )


def test_a_throttled_login_is_recorded_with_its_account(
    client: TestClient, db: Session, settings: Settings
) -> None:
    wrong = {"email": email("david"), "password": "not the password"}
    for _ in range(settings.login_max_failures):
        client.post("/auth/login", json=wrong)

    response, request_id = send(client, db, Call("POST", "/auth/login", wrong), {})

    assert response.status_code == 429
    [refused] = left_by(db, request_id)
    assert (refused.action, refused.actor_id, refused.details) == (
        "login.failed",
        person(db, "david"),
        {"reason": "too many attempts"},
    )
