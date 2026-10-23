"""Trying to get round the access checks rather than through them (threats T4, T18, T38).

The other tests ask each route the questions it was built for. These ask it
in ways it wasn't: an admin who is also a reader using every manage route to
open a document to themselves, a path or a method written another way, and
a body in a shape the route doesn't expect. Whatever comes back, nobody may
end up reading more than they could before.

The first of these found a real way round. Nobody could grant access to
themselves, but an admin who was also a reader could relabel a restricted
document "general", or move it into their own department, and read it.
"""

import json
from collections.abc import Iterator
from itertools import product
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from acmecorp import DEPARTMENTS
from apiclient import company_client, email, headers
from app.core.config import Settings
from app.models import Classification, Document, DocumentGrant, Role, RoleCode, User
from app.rag.prompt import Prompt
from fakes import FakeLLM
from questions import ALL_DEPARTMENTAL, DEPARTMENT_DOCUMENTS, FACTS, GENERAL, MAY_READ

# What Henry, the knowledge admin, may read once he is given a reading role
# as well. He is in engineering. From docs/personas.md, not from the code.
HENRY_READS = {
    RoleCode.EMPLOYEE: GENERAL | DEPARTMENT_DOCUMENTS["engineering"],
    RoleCode.EXECUTIVE: GENERAL | ALL_DEPARTMENTAL,
}
CLASSIFICATIONS = [c.value for c in Classification]
# Every label change one request can ask for.
CHANGES: list[dict[str, str]] = [
    *({"classification": c} for c in CLASSIFICATIONS),
    *({"department": d} for d in DEPARTMENTS),
    *({"classification": c, "department": d} for c, d in product(CLASSIFICATIONS, DEPARTMENTS)),
]


def repeat_everything(prompt: Prompt) -> str:
    return f"{prompt.user}\n" + " ".join(f"[S{n}]" for n in range(1, 21))


@pytest.fixture
def client(db: Session, engine: Engine, settings: Settings) -> Iterator[TestClient]:
    changed = settings.model_copy(
        update={"retrieval_min_score": -1.0, "llm_max_output_tokens": 4000}
    )
    with company_client(db, engine, changed, FakeLLM(repeat_everything)) as client:
        yield client


def person(db: Session, name: str) -> User:
    return db.scalars(select(User).where(User.email == email(name))).one()


def give_role(db: Session, name: str, code: RoleCode) -> None:
    user = person(db, name)
    user.roles.append(db.scalars(select(Role).where(Role.code == code)).one())
    db.commit()


def titles_for(client: TestClient, auth: dict[str, str]) -> set[str]:
    found = client.get("/documents", headers=auth).json()["documents"]
    return {document["title"] for document in found}


def labels(db: Session) -> list[tuple[Any, ...]]:
    """Everything that decides who may read what."""
    db.expire_all()
    return [
        (d.id, d.status, d.classification, d.department_id, sorted(g.user_id for g in d.grants))
        for d in db.scalars(select(Document).order_by(Document.id))
    ]


def ids_by_title(db: Session) -> dict[str, int]:
    return {d.title: d.id for d in db.scalars(select(Document))}


# --- an admin who can also read ---------------------------------------------------------


@pytest.mark.parametrize("role", list(HENRY_READS))
def test_no_manage_route_lets_an_admin_who_can_also_read_open_a_document_to_themselves(
    client: TestClient, db: Session, role: RoleCode
) -> None:
    give_role(db, "henry", role)
    henry = headers(client, "henry")
    assert titles_for(client, henry) == HENRY_READS[role]
    hidden = {t: n for t, n in ids_by_title(db).items() if t not in HENRY_READS[role]}
    allowed = 0

    for (title, document), change in product(sorted(hidden.items()), CHANGES):
        response = client.patch(f"/admin/documents/{document}", headers=henry, json=change)
        assert response.status_code in (200, 403), (title, change, response.text)
        allowed += response.status_code == 200
        assert titles_for(client, henry) == HENRY_READS[role], (title, change)

    for title, document in sorted(hidden.items()):
        response = client.put(
            f"/admin/documents/{document}/grants", headers=henry, json={"email": email("henry")}
        )
        assert response.status_code == 403, title
    before = labels(db)
    response = client.post(
        "/admin/documents",
        headers=henry,
        files={"file": ("note.txt", b"Booth crates ship from dock 4.", "text/plain")},
        data={"title": "Note", "department": "hr", "classification": "restricted"}
        | {"readers": [email("henry")]},
    )
    assert response.status_code == 403
    assert labels(db) == before

    assert titles_for(client, henry) == HENRY_READS[role]
    assert (
        db.scalars(
            select(DocumentGrant).where(DocumentGrant.user_id == person(db, "henry").id)
        ).all()
        == []
    )
    # Not everything was refused: a change that leaves the document closed to him is made.
    assert allowed > len(hidden)


def test_a_refused_relabel_changes_nothing(client: TestClient, db: Session) -> None:
    give_role(db, "henry", RoleCode.EMPLOYEE)
    case_file = ids_by_title(db)["HR Case File 2026-014"]
    before = labels(db)

    response = client.patch(
        f"/admin/documents/{case_file}",
        headers=headers(client, "henry"),
        json={"classification": "general"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"
    assert labels(db) == before


def test_relabelling_still_works_when_it_opens_nothing_to_the_admin(
    client: TestClient, db: Session
) -> None:
    documents = ids_by_title(db)
    # Henry with no reading role reads nothing, whatever the label.
    plain = headers(client, "henry")
    response = client.patch(
        f"/admin/documents/{documents['Salary Bands 2026']}",
        headers=plain,
        json={"classification": "general"},
    )
    assert response.status_code == 200
    assert "Salary Bands 2026" in titles_for(client, headers(client, "david"))

    give_role(db, "henry", RoleCode.EMPLOYEE)
    reader = headers(client, "henry")
    # One he already reads may be opened to everyone.
    response = client.patch(
        f"/admin/documents/{documents['Deployment Runbook']}",
        headers=reader,
        json={"classification": "general"},
    )
    assert response.status_code == 200
    # One he can't read may be moved somewhere he still can't.
    response = client.patch(
        f"/admin/documents/{documents['Headcount Budget 2027']}",
        headers=reader,
        json={"department": "legal"},
    )
    assert response.status_code == 200


# --- a path or a method written another way ---------------------------------------------


def not_served(response: Response) -> bool:
    return response.status_code >= 300


def test_an_admin_route_written_another_way_is_still_refused(
    client: TestClient, db: Session
) -> None:
    david = headers(client, "david")
    document = ids_by_title(db)["HR Case File 2026-014"]
    before = labels(db)
    paths = [
        f"/admin/documents/{document}/",
        f"//admin/documents/{document}",
        f"/admin//documents/{document}",
        f"/ADMIN/documents/{document}",
        f"/Admin/Documents/{document}",
        f"/admin/documents/{document}%2F",
        f"/admin/documents/./{document}",
        f"/documents/../admin/documents/{document}",
        f"/documents/%2E%2E/admin/documents/{document}",
        f"/admin/documents/{document};x",
        f"/admin/documents/0{document}",
        f"/admin%2Fdocuments/{document}",
    ]
    for path, method in product(paths, ["GET", "PATCH", "DELETE", "HEAD", "OPTIONS"]):
        response = client.request(
            method, path, headers=david, json={"classification": "general"}, follow_redirects=False
        )
        assert not_served(response), (method, path, response.status_code)
    assert labels(db) == before


def test_a_hidden_document_asked_for_another_way_is_still_not_found(
    client: TestClient, db: Session
) -> None:
    david = headers(client, "david")
    hidden = [n for t, n in ids_by_title(db).items() if t not in MAY_READ["david"]]
    for document in hidden:
        for path in [
            f"/documents/{document}/",
            f"//documents/{document}",
            f"/DOCUMENTS/{document}",
            f"/documents/0{document}",
            f"/documents/+{document}",
            f"/documents/%2B{document}",
            f"/documents/{document}%20",
            f"/documents/{document}.0",
            f"/documents/{document}?id=1",
            f"/documents/1/../{document}",
        ]:
            for method in ("GET", "HEAD"):
                response = client.request(method, path, headers=david, follow_redirects=True)
                assert not_served(response), (method, path)
                assert not [
                    f for f, t in FACTS.items() if t not in MAY_READ["david"] and f in response.text
                ]


def test_a_method_override_header_is_ignored(client: TestClient, db: Session) -> None:
    david = headers(client, "david")
    document = ids_by_title(db)["Discount Approval Policy"]
    before = labels(db)
    for header in ("X-HTTP-Method-Override", "X-HTTP-Method", "X-Method-Override"):
        for method, override in [("GET", "DELETE"), ("POST", "PATCH"), ("POST", "DELETE")]:
            client.request(
                method,
                f"/admin/documents/{document}",
                headers={**david, header: override},
                json={"classification": "restricted"},
            )
    assert labels(db) == before
    assert "Discount Approval Policy" in titles_for(client, david)


# --- a body in another shape ------------------------------------------------------------


def test_a_search_written_in_another_shape_never_reaches_further(
    client: TestClient, db: Session
) -> None:
    david = headers(client, "david")
    question = "What are the salary bands for senior engineers?"
    hidden = [n for t, n in ids_by_title(db).items() if t not in MAY_READ["david"]]
    grace = person(db, "grace").id
    bodies: list[Any] = [
        {"question": question, "filters": {"document_ids": [True]}},
        {"question": question, "filters": {"document_ids": [str(hidden[0])]}},
        {"question": question, "filters": {"document_ids": [float(hidden[0])]}},
        {"question": question, "filters": {"document_ids": [-1, 0]}},
        {"question": question, "filters": {"document_ids": [2**70]}},
        {"question": question, "filters": {"document_ids": hidden}},
        {"question": question, "filters": {"departments": ["HR "]}},
        {"question": question, "filters": {"departments": ["%", "*", "hr' OR '1'='1"]}},
        {"question": question, "filters": {"classifications": ["Restricted"]}},
        {"question": question, "filters": {"document_ids": [hidden[0]], "user_id": grace}},
        {"question": question, "user_id": grace},
        {"question": question, "as": "grace", "filters": None},
        {"question": [question, "and the case file"]},
        {"question": {"text": question}},
    ]
    # The same key twice: whichever one is read, a filter only narrows David's own.
    asked = json.dumps(question)
    narrowed = json.dumps({"document_ids": [hidden[0]]})
    raw = [
        f'{{"question": {asked}, "filters": {narrowed}, "filters": null}}',
        f'{{"question": {asked}, "filters": null, "filters": {narrowed}}}',
    ]
    responses = [
        client.post(route, headers=david, json=body)
        for route in ("/search", "/ask")
        for body in bodies
    ]
    for route in ("/search", "/ask"):
        for text in raw:
            responses.append(
                client.post(
                    route, headers={**david, "Content-Type": "application/json"}, content=text
                )
            )
        responses.append(client.post(route, headers=david, data={"question": question}))
        responses.append(
            client.post(
                route,
                headers={**david, "Content-Type": "text/plain"},
                content=json.dumps({"question": question}),
            )
        )

    answered = 0
    for response in responses:
        assert response.status_code in (200, 422), response.text
        found = response.json()
        cited = [
            r["document"]["title"] for r in found.get("results", []) + found.get("citations", [])
        ]
        assert set(cited) <= MAY_READ["david"]
        assert not [
            f for f, t in FACTS.items() if t not in MAY_READ["david"] and f in response.text
        ]
        answered += bool(cited)
    # Some of these were searches David may make, and found his own documents.
    assert answered


def test_a_relabel_with_fields_it_does_not_take_changes_nothing(
    client: TestClient, db: Session
) -> None:
    henry = headers(client, "henry")
    document = ids_by_title(db)["HR Case File 2026-014"]
    before = labels(db)
    for body in [
        {"classification": "general", "status": "deleted"},
        {"department": "hr", "grants": [{"email": email("david")}]},
        {"classification": "general", "readers": [email("david")]},
        {"classification": "general", "id": 1},
        {"classification": ["general", "restricted"]},
        {"department": {"code": "hr"}},
    ]:
        response = client.patch(f"/admin/documents/{document}", headers=henry, json=body)
        assert response.status_code == 422, body
    assert labels(db) == before
