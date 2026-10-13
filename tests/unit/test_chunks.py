"""Chunks: the table, how ingestion fills it, and who can read a chunk."""

import logging
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from acmecorp import NOW, PERSONAS, World
from app.authorization.document_access import GrantRequest, change_labels, revoke_access
from app.authorization.service import can_read, readable_chunks
from app.core.config import Settings
from app.core.database import utc_now
from app.core.exceptions import InvalidInputError, PermissionDeniedError
from app.ingestion.pipeline import (
    Upload,
    delete_document,
    ingest,
    rechunk_documents,
    replace_document,
)
from app.models import Chunk, Document, DocumentStatus
from logcheck import everything_logged

# Long enough for several chunks at the small sizes used here.
GUIDE = "\n".join(
    f"Step {number}. The on-call engineer checks item {number} before the release goes out."
    for number in range(1, 31)
).encode()
GUIDE_V2 = GUIDE + b"\nStep 31. The release channel is told when the release is done."
NOT_ADMINS = ["alice", "carol", "grace", "reviewer", "nobody"]


@pytest.fixture
def small(settings: Settings) -> Settings:
    """Settings that cut the guide into several chunks."""
    return settings.model_copy(update={"chunk_max_characters": 300, "chunk_overlap_characters": 80})


def upload(**changes: object) -> Upload:
    fields: dict[str, object] = {
        "filename": "release-guide.txt",
        "data": GUIDE,
        "title": "Release Guide",
        "department_code": "engineering",
        "classification": "departmental",
    }
    fields.update(changes)
    return Upload(**fields)  # type: ignore[arg-type]


@pytest.fixture
def guide(world: World, small: Settings) -> Document:
    return ingest(world.db, world.users["admin"], upload(), small)


def chunk_count(world: World) -> int:
    return world.db.scalar(select(func.count()).select_from(Chunk)) or 0


def add_chunks(world: World, per_document: int = 2) -> None:
    """Give every document in the world some chunks, for tests about access."""
    for document in world.documents.values():
        document.content = "x" * (10 * per_document)
        document.chunks = [
            Chunk(position=n, start_offset=10 * n, end_offset=10 * n + 10, text="x" * 10)
            for n in range(per_document)
        ]
    world.db.commit()


def readable_chunk_owners(world: World, user: str) -> set[str]:
    """The names of the documents whose chunks this user's query returns."""
    document_ids = {
        chunk.document_id for chunk in world.db.scalars(readable_chunks(world.users[user], NOW))
    }
    return {name for name, document in world.documents.items() if document.id in document_ids}


# --- the table -----------------------------------------------------------------------


def test_a_chunk_has_no_permission_columns() -> None:
    # Access is decided on the document. A department or classification here
    # would be a second copy that could disagree with it.
    columns = set(Chunk.__table__.columns.keys())
    assert columns == {
        "id",
        "document_id",
        "position",
        "start_offset",
        "end_offset",
        "text",
        "created_at",
    }


def test_chunks_come_back_in_reading_order(world: World) -> None:
    document = world.documents["runbook"]
    document.chunks = [
        Chunk(position=2, start_offset=20, end_offset=30, text="c"),
        Chunk(position=0, start_offset=0, end_offset=10, text="a"),
        Chunk(position=1, start_offset=10, end_offset=20, text="b"),
    ]
    world.db.commit()
    world.db.expire_all()

    assert [chunk.text for chunk in world.documents["runbook"].chunks] == ["a", "b", "c"]


def test_a_position_is_used_once_per_document(world: World) -> None:
    for document in (world.documents["runbook"], world.documents["leave_policy"]):
        document.chunks = [Chunk(position=0, start_offset=0, end_offset=5, text="a")]
    world.db.commit()

    world.db.add(
        Chunk(
            document=world.documents["runbook"], position=0, start_offset=5, end_offset=9, text="b"
        )
    )
    with pytest.raises(IntegrityError):
        world.db.commit()


@pytest.mark.parametrize(
    "fields",
    [
        {"position": -1, "start_offset": 0, "end_offset": 5},
        {"position": 0, "start_offset": -1, "end_offset": 5},
        {"position": 0, "start_offset": 5, "end_offset": 5},
        {"position": 0, "start_offset": 9, "end_offset": 5},
    ],
)
def test_offsets_that_make_no_sense_are_refused(world: World, fields: dict[str, int]) -> None:
    world.db.add(Chunk(document=world.documents["runbook"], text="a", **fields))

    with pytest.raises(IntegrityError):
        world.db.commit()


def test_a_chunk_must_belong_to_a_real_document(world: World) -> None:
    world.db.add(Chunk(document_id=9999, position=0, start_offset=0, end_offset=5, text="a"))

    with pytest.raises(IntegrityError):
        world.db.commit()


def test_removing_a_document_row_removes_its_chunks(world: World) -> None:
    add_chunks(world)
    before = chunk_count(world)

    world.db.delete(world.documents["runbook"])
    world.db.commit()

    assert chunk_count(world) == before - 2


# --- ingestion ------------------------------------------------------------------------


def test_ingesting_stores_the_chunks_with_the_document(guide: Document, world: World) -> None:
    world.db.expire_all()
    stored = world.db.get(Document, guide.id)

    assert stored is not None
    assert len(stored.chunks) > 3
    assert [chunk.position for chunk in stored.chunks] == list(range(len(stored.chunks)))


def test_every_stored_chunk_is_a_slice_of_its_document(guide: Document) -> None:
    for chunk in guide.chunks:
        assert chunk.text == guide.content[chunk.start_offset : chunk.end_offset]


def test_stored_chunks_respect_the_configured_size(guide: Document) -> None:
    assert all(len(chunk.text) <= 300 for chunk in guide.chunks)


def test_the_default_size_keeps_a_short_document_in_one_chunk(
    world: World, settings: Settings
) -> None:
    document = ingest(
        world.db, world.users["admin"], upload(data=b"Rollbacks need approval."), settings
    )

    assert [chunk.text for chunk in document.chunks] == ["Rollbacks need approval."]


def test_chunks_are_cut_from_the_cleaned_text(world: World, small: Settings) -> None:
    # A zero-width space and a tag character, which cleaning removes.
    hidden = "Rollbacks​ need\U000e0041 approval."
    document = ingest(world.db, world.users["admin"], upload(data=hidden.encode()), small)

    assert [chunk.text for chunk in document.chunks] == ["Rollbacks need approval."]


@pytest.mark.parametrize(
    "changes",
    [
        {"data": b""},
        {"filename": "guide.exe"},
        {"classification": "secret"},
        {"department_code": "marketing"},
        {"classification": "restricted"},
    ],
)
def test_a_refused_upload_stores_no_chunks(
    world: World, small: Settings, changes: dict[str, object]
) -> None:
    with pytest.raises(InvalidInputError):
        ingest(world.db, world.users["admin"], upload(**changes), small)

    assert chunk_count(world) == 0


def test_the_chunk_count_is_logged_without_the_text(
    world: World, small: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        document = ingest(world.db, world.users["admin"], upload(), small)

    record = next(r for r in caplog.records if r.message == "document ingested")
    assert record.chunks == len(document.chunks)  # type: ignore[attr-defined]
    assert "on-call engineer" not in everything_logged(caplog)


# --- versions and deletion ----------------------------------------------------------------


def test_a_new_version_gets_its_own_chunks(world: World, small: Settings, guide: Document) -> None:
    old_ids = {chunk.id for chunk in guide.chunks}

    new_version = replace_document(
        world.db,
        world.users["admin"],
        guide.id,
        filename="guide.txt",
        data=GUIDE_V2,
        settings=small,
    )

    assert new_version.chunks
    assert old_ids.isdisjoint(chunk.id for chunk in new_version.chunks)
    assert "Step 31." in new_version.chunks[-1].text
    for chunk in new_version.chunks:
        assert chunk.text == new_version.content[chunk.start_offset : chunk.end_offset]


def test_the_old_version_keeps_its_chunks_but_nobody_can_read_them(
    world: World, small: Settings, guide: Document
) -> None:
    old_ids = {chunk.id for chunk in guide.chunks}
    new_version = replace_document(
        world.db,
        world.users["admin"],
        guide.id,
        filename="guide.txt",
        data=GUIDE_V2,
        settings=small,
    )

    kept = set(world.db.scalars(select(Chunk.id).where(Chunk.document_id == guide.id)))
    readable = set(
        world.db.scalars(readable_chunks(world.users["alice"]).with_only_columns(Chunk.id))
    )

    assert kept == old_ids
    assert readable.isdisjoint(old_ids)
    assert readable == {chunk.id for chunk in new_version.chunks}


def test_a_bad_replacement_leaves_the_chunks_alone(
    world: World, small: Settings, guide: Document
) -> None:
    before = {chunk.id for chunk in guide.chunks}

    with pytest.raises(InvalidInputError):
        replace_document(
            world.db, world.users["admin"], guide.id, filename="guide.txt", data=b"", settings=small
        )

    assert set(world.db.scalars(select(Chunk.id))) == before
    assert (
        set(world.db.scalars(readable_chunks(world.users["alice"]).with_only_columns(Chunk.id)))
        == before
    )


def test_deleting_a_document_keeps_its_chunks_but_nobody_can_read_them(
    world: World, guide: Document
) -> None:
    count = len(guide.chunks)

    delete_document(world.db, world.users["admin"], guide.id)

    assert chunk_count(world) == count
    for user in world.users.values():
        assert world.db.scalars(readable_chunks(user)).all() == []


# --- who can read a chunk -------------------------------------------------------------------


@pytest.mark.parametrize("user", [*PERSONAS, "admin", "reviewer", "nobody"])
def test_a_chunk_is_readable_exactly_when_its_document_is(company: World, user: str) -> None:
    add_chunks(company)
    company.grant("case_file", "carol")
    company.grant("acquisition", "grace")
    company.grant("runbook", "carol", expires_at=NOW + timedelta(days=30))
    company.grant("postmortem", "bob", expires_at=NOW - timedelta(days=1))

    assert readable_chunk_owners(company, user) == company.readable(user)


@pytest.mark.parametrize("user", [*PERSONAS, "admin", "reviewer", "nobody"])
def test_every_chunk_of_a_readable_document_is_returned(company: World, user: str) -> None:
    add_chunks(company, per_document=3)

    found = company.db.scalars(readable_chunks(company.users[user], NOW)).all()

    assert len(found) == 3 * len(company.readable(user))
    for chunk in found:
        assert can_read(company.users[user], chunk.document, NOW)


def test_no_reading_role_means_no_chunks(company: World) -> None:
    add_chunks(company)

    for user in ("admin", "reviewer", "nobody"):
        assert company.db.scalars(readable_chunks(company.users[user], NOW)).all() == []


def test_an_inactive_user_gets_no_chunks(company: World) -> None:
    add_chunks(company)
    company.users["grace"].is_active = False
    company.db.commit()

    assert company.db.scalars(readable_chunks(company.users["grace"], NOW)).all() == []


@pytest.mark.parametrize("status", [DocumentStatus.SUPERSEDED, DocumentStatus.DELETED])
def test_chunks_of_a_retired_document_are_readable_by_nobody(
    company: World, status: DocumentStatus
) -> None:
    add_chunks(company)
    company.grant("runbook", "carol")
    company.documents["runbook"].status = status
    company.db.commit()

    for user in company.users:
        assert "runbook" not in readable_chunk_owners(company, user)


def test_chunks_of_a_document_with_a_broken_label_are_readable_by_nobody(company: World) -> None:
    add_chunks(company)
    company.grant("runbook", "carol")
    company.db.execute(
        text("UPDATE documents SET classification = 'public' WHERE id = :id"),
        {"id": company.documents["runbook"].id},
    )
    company.db.commit()
    company.db.expire_all()

    for user in company.users:
        assert "runbook" not in readable_chunk_owners(company, user)


def test_a_grant_that_has_expired_no_longer_reaches_the_chunks(company: World) -> None:
    add_chunks(company)
    company.grant("case_file", "carol", expires_at=NOW + timedelta(days=1))
    carol = company.users["carol"]

    def reads_case_file(moment_offset: timedelta) -> bool:
        chunks = company.db.scalars(readable_chunks(carol, NOW + moment_offset))
        return any(chunk.document_id == company.documents["case_file"].id for chunk in chunks)

    assert reads_case_file(timedelta(hours=1))
    assert not reads_case_file(timedelta(days=2))


def test_a_label_change_reaches_the_chunks_at_once(
    world: World, small: Settings, guide: Document
) -> None:
    carol, admin = world.users["carol"], world.users["admin"]
    assert world.db.scalars(readable_chunks(carol)).all() == []

    change_labels(world.db, admin, guide.id, classification="general")

    assert len(world.db.scalars(readable_chunks(carol)).all()) == len(guide.chunks)
    assert world.db.scalars(select(Chunk.id)).all() == [chunk.id for chunk in guide.chunks]


def test_revoking_a_grant_reaches_the_chunks_at_once(world: World, small: Settings) -> None:
    carol, admin = world.users["carol"], world.users["admin"]
    document = ingest(
        world.db,
        admin,
        upload(classification="restricted", grants=(GrantRequest(email=carol.email),)),
        small,
    )
    assert len(world.db.scalars(readable_chunks(carol)).all()) == len(document.chunks)

    revoke_access(world.db, admin, document.id, carol.email)

    assert world.db.scalars(readable_chunks(carol)).all() == []


# --- re-chunking -----------------------------------------------------------------------


def test_rechunking_changes_nothing_when_the_chunks_are_current(
    world: World, small: Settings, guide: Document
) -> None:
    before = [(chunk.id, chunk.text) for chunk in guide.chunks]

    assert rechunk_documents(world.db, world.users["admin"], small) == 0

    world.db.expire_all()
    after = world.db.scalars(select(Chunk).order_by(Chunk.position)).all()
    assert [(chunk.id, chunk.text) for chunk in after] == before


def test_rechunking_follows_changed_settings(
    world: World, small: Settings, settings: Settings, guide: Document
) -> None:
    several = len(guide.chunks)

    assert rechunk_documents(world.db, world.users["admin"], settings) == 1

    world.db.expire_all()
    chunks = world.db.scalars(select(Chunk).order_by(Chunk.position)).all()
    assert len(chunks) < several
    assert all(len(chunk.text) <= settings.chunk_max_characters for chunk in chunks)
    for chunk in chunks:
        assert chunk.document_id == guide.id
        assert chunk.text == guide.content[chunk.start_offset : chunk.end_offset]


def test_rechunking_fills_in_a_document_that_has_no_chunks(
    world: World, small: Settings, guide: Document
) -> None:
    world.db.execute(text("DELETE FROM chunks"))
    world.db.commit()
    world.db.expire_all()

    assert rechunk_documents(world.db, world.users["admin"], small) == 1
    assert chunk_count(world) > 3


def test_rechunking_repairs_a_chunk_that_does_not_match_its_document(
    world: World, small: Settings, guide: Document
) -> None:
    world.db.execute(text("UPDATE chunks SET text = 'something else' WHERE position = 1"))
    world.db.commit()
    world.db.expire_all()

    assert rechunk_documents(world.db, world.users["admin"], small) == 1

    world.db.expire_all()
    stored = world.db.get(Document, guide.id)
    assert stored is not None
    for chunk in stored.chunks:
        assert chunk.text == stored.content[chunk.start_offset : chunk.end_offset]


def test_rechunking_leaves_retired_documents_alone(
    world: World, small: Settings, settings: Settings, guide: Document
) -> None:
    before = [chunk.id for chunk in guide.chunks]
    delete_document(world.db, world.users["admin"], guide.id)

    assert rechunk_documents(world.db, world.users["admin"], settings) == 0
    assert world.db.scalars(select(Chunk.id).order_by(Chunk.position)).all() == before


@pytest.mark.parametrize("name", NOT_ADMINS)
def test_only_an_admin_can_rechunk(
    world: World, small: Settings, guide: Document, name: str
) -> None:
    before = chunk_count(world)

    with pytest.raises(PermissionDeniedError):
        rechunk_documents(world.db, world.users[name], small)

    assert chunk_count(world) == before


def test_chunks_carry_a_timestamp(guide: Document) -> None:
    assert all(utc_now() - chunk.created_at < timedelta(minutes=1) for chunk in guide.chunks)
