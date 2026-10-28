"""Nothing a person gets back depends on what they may not read (SR-10).

The other tests each look for one way a hidden document could show: its
text, its title, a citation, an error message. This one doesn't list the
ways. It builds the company twice, differing only in what one person may not
read, and checks that every response that person can get is the same in
both, byte for byte, headers included.

In the second company every document the person may not read has another
title, other text, another file name and size, and a different number of
chunks, and three more documents they may not read are added, written to
match every question better than anything they may read does. And every
document's own ID is another, with gaps between them, as if other documents
had been stored in between. So any channel that depended on those documents
would show up as a difference: a count, a page, a score, an order, the
relevance gate's decision, an error, a header, the length of a body, or the
IDs themselves (threat model T36).

The service knows each document outside by a public ID, which is random. So
the two companies can be compared byte for byte, each is given the same
public ID for a document with the same title and version; that it is random
in the service, and taken from nothing else, is checked by
``tests/unit/test_public_ids.py``.

What it can't see, and where each is dealt with instead:

- Time. A response the same in both can still take longer in one. Opening a
  hidden document and a missing one do the same work, which
  ``test_a_refused_lookup_never_reads_the_document`` checks.
- The knowledge admin and the security reviewer are shown things about every
  document on purpose: its file details and chunk count, and the audit
  trail. Their own pages aren't compared; what they get as readers is.
"""

import hashlib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, select, text, update
from sqlalchemy.orm import Session

from acmecorp import DEPARTMENTS
from apiclient import MISSING, PASSWORD, email, headers
from app.api.components import Components
from app.authorization.document_access import GrantRequest
from app.core.config import Settings
from app.core.database import Base, create_db_engine, create_session_factory
from app.ingestion.pipeline import Upload, ingest, rechunk_documents
from app.main import create_app
from app.models import Classification, Document, DocumentStatus, User
from app.rag.prompt import Prompt
from app.retrieval.index import build_index
from fakes import FakeEmbedder, FakeLLM
from questions import ANSWERABLE, MAY_READ, OFF_TOPIC
from scripts.seed import seed

PEOPLE = sorted(MAY_READ)
QUESTIONS = [*ANSWERABLE, *OFF_TOPIC[:3]]
# Every question at once: a passage of this matches each of them closely.
EVERY_QUESTION = " ".join(QUESTIONS)
ADDED = 3
# Seeding takes the clock's time; both companies are given this one instead.
STORED_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
# Where the public IDs both companies are given come from.
NAMESPACE = uuid.UUID("6f1d3c2a-5b4e-4f7a-9c8d-0e1f2a3b4c5d")


def repeat_everything(prompt: Prompt) -> str:
    return f"{prompt.user}\n" + " ".join(f"[S{n}]" for n in range(1, 21))


@dataclass
class Company:
    client: TestClient
    db: Session
    # The public ID of every document stored, active or not.
    ids: list[uuid.UUID] = field(default_factory=list)


def person(db: Session, name: str) -> User:
    return db.scalars(select(User).where(User.email == email(name))).one()


def change_what_is_hidden(db: Session, settings: Settings, name: str) -> None:
    """Give every document this person may not read other labels and text, and add more."""
    henry = person(db, "henry")
    active = db.scalars(select(Document).where(Document.status == DocumentStatus.ACTIVE)).all()
    hidden = [d for d in active if d.title not in MAY_READ[name]]
    assert hidden, "nothing to change"
    for n, document in enumerate(hidden):
        text = f"Other text, version {n}. " + EVERY_QUESTION * (n % 3 + 1)
        document.title = f"Another title {n} " + "x" * n
        document.content = text
        document.source_filename = f"another-{n}.txt"
        document.size_bytes = len(text)
        document.sha256 = hashlib.sha256(text.encode()).hexdigest()
    db.commit()
    assert rechunk_documents(db, henry, settings) == len(hidden)

    # Readable by someone else only, so never by this person.
    other = "bob" if name == "alice" else "alice"
    for n in range(ADDED):
        ingest(
            db,
            henry,
            Upload(
                filename=f"added-{n}.txt",
                data=(EVERY_QUESTION * (n + 1)).encode(),
                title=f"Added {n}",
                department_code=DEPARTMENTS[n],
                classification="restricted",
                grants=(GrantRequest(email(other)),),
            ),
            settings,
        )


def renumber_documents(db: Session) -> None:
    """Give every document another ID of its own, with gaps, as if others were stored between."""
    db.commit()
    # SQLite can't update a key that others refer to while it checks them.
    db.execute(text("PRAGMA foreign_keys=OFF"))
    for table, name in [
        ("documents", "id"),
        ("documents", "supersedes_id"),
        ("chunks", "document_id"),
        ("document_grants", "document_id"),
        ("document_public_ids", "document_id"),
    ]:
        column = Base.metadata.tables[table].c[name]
        db.execute(
            update(column.table).where(column.is_not(None)).values({column: 1000 + 7 * column})
        )
    db.commit()
    db.execute(text("PRAGMA foreign_keys=ON"))
    db.expire_all()


def same_public_ids(db: Session) -> None:
    """The same public ID in both companies for a document with the same title and version."""
    for document in db.scalars(select(Document)):
        document.public.public_id = uuid.uuid5(NAMESPACE, f"{document.title} {document.version}")
    db.commit()


@contextmanager
def open_company(settings: Settings, hidden_from: str | None = None) -> Iterator[Company]:
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with create_session_factory(engine)() as db:
            seed(db, settings, PASSWORD)
            if hidden_from is not None:
                change_what_is_hidden(db, settings, hidden_from)
                renumber_documents(db)
            same_public_ids(db)
            db.execute(update(Document).values(created_at=STORED_AT, updated_at=STORED_AT))
            db.commit()
            embedder = FakeEmbedder()
            components = Components(
                embedder=embedder,
                index=build_index(db, embedder),
                provider=FakeLLM(repeat_everything),
            )
            app = create_app(settings, engine=engine, components=components)
            with TestClient(app, raise_server_exceptions=False) as client:
                ids = db.scalars(select(Document)).all()
                yield Company(client, db, sorted(d.public_id for d in ids))
    finally:
        engine.dispose()


@pytest.fixture
def settings(settings: Settings) -> Settings:
    # Long enough for an answer that repeats everything it was given. The
    # relevance gate stays at its default: whether it lets a question through
    # must not depend on hidden documents either.
    return settings.model_copy(update={"llm_max_output_tokens": 4000})


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    json: Any = None


ASKING = [("POST", "/search"), ("POST", "/ask")]


def requests_for(ids: list[uuid.UUID]) -> list[Request]:
    """Everything a reader can ask the service, with every ID either company has and more."""
    every_id = [*ids, MISSING]
    found = [
        Request("GET", "/health"),
        Request("GET", "/auth/me"),
        Request("GET", "/documents"),
        *(Request("GET", f"/documents?department={code}") for code in (*DEPARTMENTS, "nowhere")),
        *(Request("GET", f"/documents?classification={c.value}") for c in Classification),
        *(Request("GET", f"/documents?limit=2&offset={n}") for n in range(0, 20, 2)),
        *(Request("GET", f"/documents/{n}") for n in every_id),
    ]
    for question in QUESTIONS:
        found += [Request(m, path, {"question": question}) for m, path in ASKING]
    filters: list[dict[str, list[Any]]] = [
        *({"departments": [code]} for code in (*DEPARTMENTS, "nowhere")),
        *({"classifications": [c.value]} for c in Classification),
        *({"document_ids": [str(n)]} for n in every_id),
    ]
    for question in QUESTIONS[:2]:
        for narrowed in filters:
            found += [
                Request(m, path, {"question": question, "filters": narrowed}) for m, path in ASKING
            ]
    return found


def everything_seen(company: Company, auth: dict[str, str], walk: list[Request]) -> list[Any]:
    """Each response in full: status, every header and the body's bytes."""
    seen = []
    for n, request in enumerate(walk):
        # The same request ID in both, so it is the same in every body and header.
        sent = {**auth, "X-Request-ID": f"walk-{n}"}
        response = company.client.request(
            request.method, request.path, headers=sent, json=request.json
        )
        headers = sorted(response.headers.items())
        seen.append((request, response.status_code, headers, response.content))
    return seen


def walk_both(settings: Settings, viewer: str, hidden_from: str) -> tuple[list[Any], list[Any]]:
    with open_company(settings) as plain, open_company(settings, hidden_from) as changed:
        walk = requests_for(sorted({*plain.ids, *changed.ids}))
        auth = headers(plain.client, viewer)
        return everything_seen(plain, auth, walk), everything_seen(changed, auth, walk)


@pytest.mark.parametrize("name", PEOPLE)
def test_what_a_person_gets_back_is_the_same_whatever_they_may_not_read(
    settings: Settings, name: str
) -> None:
    plain, changed = walk_both(settings, name, hidden_from=name)

    differences = [
        (first[0], first[1], second[1])
        for first, second in zip(plain, changed, strict=True)
        if first != second
    ]
    assert differences == []
    # The same token works in both: the people are stored the same way.
    assert all(status != 401 for _, status, _, _ in plain)
    if MAY_READ[name]:
        # Something to compare: passages, answers and documents were shown.
        found = [body for request, status, _, body in plain if status == 200]
        assert sum(b'"results":[{' in body for body in found) >= 10
        assert sum(b'"answered":true' in body for body in found) >= 10
        assert sum(b'"content":' in body for body in found) == len(MAY_READ[name])


def test_the_same_walk_tells_the_companies_apart_for_someone_who_reads_what_changed(
    settings: Settings,
) -> None:
    # Grace reads Alice's hidden departmental documents, so for her the two differ.
    plain, changed = walk_both(settings, "grace", hidden_from="alice")

    pairs = zip(plain, changed, strict=True)
    differing = {first[0].path for first, second in pairs if first != second}
    assert "/documents" in differing
    assert "/search" in differing
    assert "/ask" in differing


# --- what the walk can't see ------------------------------------------------------------


def test_a_refused_lookup_never_reads_the_document(settings: Settings) -> None:
    """A hidden document and a missing ID cost the same: the same queries, and nothing loaded.

    Loading the hidden document to see that it exists made the 404 slower
    the bigger the document was.
    """
    with open_company(settings) as company:
        auth = headers(company.client, "david")
        case_file = company.db.scalars(
            select(Document).where(Document.title == "HR Case File 2026-014")
        ).one()
        engine = company.db.get_bind()
        assert isinstance(engine, Engine)

        def work(document_id: uuid.UUID, status: int) -> tuple[list[str], list[int]]:
            statements: list[str] = []
            loaded: list[int] = []

            def ran(*args: Any) -> None:
                statements.append(args[2])

            def load(document: Document, _: Any) -> None:
                loaded.append(document.id)

            event.listen(engine, "before_cursor_execute", ran)
            event.listen(Document, "load", load)
            try:
                response = company.client.get(f"/documents/{document_id}", headers=auth)
                assert response.status_code == status
            finally:
                event.remove(engine, "before_cursor_execute", ran)
                event.remove(Document, "load", load)
            return statements, loaded

        hidden, hidden_loaded = work(case_file.public_id, 404)
        missing, missing_loaded = work(MISSING, 404)
        leave = company.db.scalars(select(Document).where(Document.title == "Leave Policy")).one()
        _, opened = work(leave.public_id, 200)

    assert hidden == missing
    assert hidden_loaded == missing_loaded == []
    # The listener does see a document that is loaded.
    assert opened == [leave.id]


def test_in_the_second_company_every_document_has_another_id_of_its_own(
    settings: Settings,
) -> None:
    """What makes the walk see the IDs: the documents a person reads are numbered otherwise."""
    with open_company(settings) as plain, open_company(settings, "david") as changed:
        first, second = (
            {d.title: (d.id, d.public_id) for d in company.db.scalars(select(Document))}
            for company in (plain, changed)
        )

    readable = MAY_READ["david"]
    assert readable
    for title in readable:
        (number, public_id), (other_number, other_public_id) = first[title], second[title]
        assert number != other_number
        assert public_id == other_public_id
    numbers = sorted(number for number, _ in second.values())
    assert all(b - a > 1 for a, b in pairwise(numbers))
