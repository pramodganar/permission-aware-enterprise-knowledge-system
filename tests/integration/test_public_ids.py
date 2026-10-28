"""The IDs documents are known by outside the service (threat model T36).

A document's own ID is numbered in the order documents are stored. It is
never shown: every response names a document by its public ID, which is
random. That nothing outside the service sees the numbering is shown by the
non-interference test, which numbers every document otherwise and gets the
same responses. This is what that test takes as given: each public ID is
random, comes from nothing else, and every document has one, those stored
before public IDs existed included.
"""

import logging
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.orm import Session

from apiclient import PASSWORD, headers
from app.api.components import Components
from app.core.config import Settings
from app.core.database import Base, create_db_engine, create_session_factory
from app.main import create_app
from app.models import Document, document_number, give_public_ids, public_ids
from app.retrieval.index import build_index
from fakes import FakeEmbedder
from scripts.seed import seed


def stored(settings: Settings) -> dict[str, UUID]:
    """The company seeded into a database of its own: each document's public ID, by title."""
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with create_session_factory(engine)() as db:
            seed(db, settings, PASSWORD)
            return {d.title: d.public_id for d in db.scalars(select(Document))}
    finally:
        engine.dispose()


def test_every_document_is_given_a_random_public_id(db: Session, settings: Settings) -> None:
    seed(db, settings, PASSWORD)
    documents = db.scalars(select(Document)).all()

    assert len(documents) == 17
    assert all(d.public_id.version == 4 for d in documents)
    assert len({d.public_id for d in documents}) == len(documents)
    assert all(d.public_id != UUID(int=d.id) for d in documents)


def test_the_same_company_stored_twice_shares_no_public_id(settings: Settings) -> None:
    # Taken from nothing about the document, its place or the time: the same
    # documents, stored in the same order, get other IDs.
    first, second = stored(settings), stored(settings)

    assert first.keys() == second.keys()
    assert not set(first.values()) & set(second.values())


def test_a_public_id_finds_its_document_and_nothing_else(db: Session, settings: Settings) -> None:
    seed(db, settings, PASSWORD)
    leave = db.scalars(select(Document).where(Document.title == "Leave Policy")).one()

    assert document_number(db, leave.public_id) == leave.id
    assert document_number(db, UUID(int=leave.id)) is None
    assert public_ids(db, [leave.id, None]) == {leave.id: leave.public_id}
    assert public_ids(db, []) == {}


def test_a_database_made_before_public_ids_gets_them_when_the_service_starts(
    db: Session, engine: Engine, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    seed(db, settings, PASSWORD)
    embedder = FakeEmbedder()
    components = Components(embedder=embedder, index=build_index(db, embedder), provider=None)
    titles = set(db.scalars(select(Document.title)))
    # As a database from before this version: no table for them at all.
    db.execute(text("DROP TABLE document_public_ids"))
    db.commit()
    db.expire_all()
    assert "document_public_ids" not in inspect(engine).get_table_names()

    caplog.set_level(logging.INFO, logger="app.models.document")
    app = create_app(settings, engine=engine, components=components)
    with TestClient(app, raise_server_exceptions=False) as client:
        david = headers(client, "david")
        listed = client.get("/documents", headers=david).json()["documents"]
        opened = [client.get(f"/documents/{d['id']}", headers=david) for d in listed]

    given = [r for r in caplog.records if r.getMessage() == "public document IDs given"]
    assert [r.__dict__["documents"] for r in given] == [17]
    assert listed
    assert all(UUID(d["id"]).version == 4 for d in listed)
    assert [r.status_code for r in opened] == [200] * len(listed)
    assert {r.json()["title"] for r in opened} <= titles
    # Once given, they are kept.
    assert give_public_ids(db) == 0
