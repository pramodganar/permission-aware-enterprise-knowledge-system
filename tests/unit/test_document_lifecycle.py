"""Duplicates, new versions and deletion."""

import logging
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text

from acmecorp import World
from app.authorization.checks import get_readable_document
from app.authorization.document_access import GrantRequest, change_labels, grant_access
from app.authorization.service import can_read, readable_documents
from app.core.config import Settings
from app.core.database import utc_now
from app.core.exceptions import InvalidInputError, NotFoundError, PermissionDeniedError
from app.ingestion.pipeline import Upload, delete_document, ingest, replace_document
from app.models import Document, DocumentStatus

V1 = b"Rollbacks are approved by the on-call engineer."
V2 = b"Rollbacks are approved by the on-call engineer and logged in the tracker."
V3 = b"Rollbacks need two approvals."
NOT_ADMINS = ["alice", "carol", "grace", "reviewer", "nobody"]


def upload(**changes: object) -> Upload:
    fields: dict[str, object] = {
        "filename": "rollback-guide.txt",
        "data": V1,
        "title": "Rollback Guide",
        "department_code": "engineering",
        "classification": "departmental",
    }
    fields.update(changes)
    return Upload(**fields)  # type: ignore[arg-type]


@pytest.fixture
def guide(world: World, settings: Settings) -> Document:
    return ingest(world.db, world.users["admin"], upload(), settings)


def readers(world: World, document: Document) -> set[str]:
    return {name for name, user in world.users.items() if can_read(user, document)}


def listed_ids(world: World, user: str) -> set[int]:
    return {document.id for document in world.db.scalars(readable_documents(world.users[user]))}


def document_count(world: World) -> int:
    return world.db.scalar(select(func.count()).select_from(Document)) or 0


def replace(world: World, settings: Settings, document: Document, data: bytes) -> Document:
    return replace_document(
        world.db,
        world.users["admin"],
        document.id,
        filename="rollback-guide-new.txt",
        data=data,
        settings=settings,
    )


# Duplicates


def test_same_file_cannot_be_ingested_twice(
    world: World, settings: Settings, guide: Document
) -> None:
    before = document_count(world)

    with pytest.raises(InvalidInputError, match=f"already stored as document {guide.public_id}"):
        ingest(
            world.db,
            world.users["admin"],
            upload(filename="copy.txt", title="A Copy", department_code="hr"),
            settings,
        )

    assert document_count(world) == before


def test_duplicate_is_noticed_before_the_file_is_parsed(
    world: World, settings: Settings, guide: Document, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_if_called(filename: str, data: bytes) -> str:
        raise AssertionError("a file that is already stored must not be parsed again")

    monkeypatch.setattr("app.ingestion.pipeline.extract_text", fail_if_called)

    with pytest.raises(InvalidInputError, match="already stored"):
        ingest(world.db, world.users["admin"], upload(), settings)


def test_same_words_in_a_different_file_are_not_a_duplicate(
    world: World, settings: Settings, guide: Document
) -> None:
    other = ingest(world.db, world.users["admin"], upload(data=V1 + b"\n"), settings)

    assert other.content == guide.content
    assert other.sha256 != guide.sha256


def test_file_can_be_ingested_again_after_the_first_copy_is_deleted(
    world: World, settings: Settings, guide: Document
) -> None:
    delete_document(world.db, world.users["admin"], guide.id)

    again = ingest(world.db, world.users["admin"], upload(), settings)

    assert again.id != guide.id
    assert again.status == "active"


# New versions


def test_replacing_stores_a_new_version_and_retires_the_old_one(
    world: World, settings: Settings, guide: Document
) -> None:
    new = replace(world, settings, guide, V2)
    world.db.expire_all()

    assert new.id != guide.id
    assert new.version == 2
    assert new.supersedes_id == guide.id
    assert new.status == "active"
    assert new.content == V2.decode()
    assert new.source_filename == "rollback-guide-new.txt"
    assert new.uploaded_by.id == world.users["admin"].id

    assert guide.status == "superseded"
    assert guide.version == 1
    assert guide.content == V1.decode()


def test_new_version_keeps_the_title_department_and_classification(
    world: World, settings: Settings, guide: Document
) -> None:
    new = replace(world, settings, guide, V2)

    assert new.title == "Rollback Guide"
    assert new.department.code == "engineering"
    assert new.classification == "departmental"


def test_new_version_can_be_given_a_new_title(
    world: World, settings: Settings, guide: Document
) -> None:
    new = replace_document(
        world.db,
        world.users["admin"],
        guide.id,
        filename="guide.txt",
        data=V2,
        settings=settings,
        title="  Rollback and Recovery Guide ",
    )

    assert new.title == "Rollback and Recovery Guide"


def test_readers_of_the_old_version_read_the_new_one_and_only_the_new_one(
    world: World, settings: Settings, guide: Document
) -> None:
    assert readers(world, guide) == {"alice", "grace"}

    new = replace(world, settings, guide, V2)

    assert readers(world, new) == {"alice", "grace"}
    assert readers(world, guide) == set()
    assert guide.id not in listed_ids(world, "alice")
    assert new.id in listed_ids(world, "alice")
    assert guide.id not in listed_ids(world, "grace")


def test_old_version_cannot_be_fetched_by_id(
    world: World, settings: Settings, guide: Document
) -> None:
    new = replace(world, settings, guide, V2)

    with pytest.raises(NotFoundError):
        get_readable_document(world.db, world.users["alice"], guide.public_id)
    assert get_readable_document(world.db, world.users["alice"], new.public_id).id == new.id


def test_grants_carry_over_to_the_new_version(
    world: World, settings: Settings, guide: Document
) -> None:
    grant_access(world.db, world.users["admin"], guide.id, GrantRequest("carol@acmecorp.example"))

    new = replace(world, settings, guide, V2)

    assert readers(world, new) == {"alice", "carol", "grace"}
    assert readers(world, guide) == set()
    assert new.grants[0].user_id == world.users["carol"].id
    assert new.grants[0].granted_by_id == world.users["admin"].id


def test_expired_grants_do_not_carry_over(
    world: World, settings: Settings, guide: Document
) -> None:
    grant_access(world.db, world.users["admin"], guide.id, GrantRequest("carol@acmecorp.example"))
    guide.grants[0].expires_at = utc_now() - timedelta(minutes=1)
    world.db.commit()

    new = replace(world, settings, guide, V2)

    assert new.grants == []
    assert "carol" not in readers(world, new)


def test_grant_expiry_is_kept_on_the_new_version(
    world: World, settings: Settings, guide: Document
) -> None:
    expires = utc_now() + timedelta(days=7)
    grant_access(
        world.db, world.users["admin"], guide.id, GrantRequest("carol@acmecorp.example", expires)
    )

    new = replace(world, settings, guide, V2)

    assert new.grants[0].expires_at == expires


def test_restricted_document_stays_restricted_to_the_same_readers(
    world: World, settings: Settings
) -> None:
    case = ingest(
        world.db,
        world.users["admin"],
        upload(
            title="Case 2026-021",
            department_code="hr",
            classification="restricted",
            grants=(GrantRequest("carol@acmecorp.example"),),
        ),
        settings,
    )

    new = replace(world, settings, case, V2)

    assert new.classification == "restricted"
    assert readers(world, new) == {"carol"}
    assert readers(world, case) == set()


def test_versions_form_a_chain(world: World, settings: Settings, guide: Document) -> None:
    second = replace(world, settings, guide, V2)
    third = replace(world, settings, second, V3)

    assert [guide.version, second.version, third.version] == [1, 2, 3]
    assert third.supersedes_id == second.id
    assert second.supersedes_id == guide.id
    assert [guide.status, second.status, third.status] == ["superseded", "superseded", "active"]
    assert listed_ids(world, "alice") >= {third.id}
    assert listed_ids(world, "alice").isdisjoint({guide.id, second.id})


def test_only_the_current_version_can_be_replaced(
    world: World, settings: Settings, guide: Document
) -> None:
    replace(world, settings, guide, V2)

    with pytest.raises(InvalidInputError, match="replaced or deleted"):
        replace(world, settings, guide, V3)


def test_replacing_with_the_identical_file_is_rejected(
    world: World, settings: Settings, guide: Document
) -> None:
    with pytest.raises(InvalidInputError, match="already stored"):
        replace(world, settings, guide, V1)

    assert guide.status == "active"


@pytest.mark.parametrize(
    ("filename", "data", "message"),
    [
        ("guide.exe", V2, "unsupported file type"),
        ("guide.txt", b"", "empty"),
        ("guide.txt", b"   \n", "no text"),
        ("guide.txt", b"\x00\x01binary", "not a readable text file"),
    ],
    ids=["wrong type", "empty", "no text", "binary"],
)
def test_bad_replacement_file_leaves_the_current_version_in_place(
    world: World, settings: Settings, guide: Document, filename: str, data: bytes, message: str
) -> None:
    before = document_count(world)

    with pytest.raises(InvalidInputError, match=message):
        replace_document(
            world.db,
            world.users["admin"],
            guide.id,
            filename=filename,
            data=data,
            settings=settings,
        )

    world.db.rollback()
    assert document_count(world) == before
    assert guide.status == "active"
    assert readers(world, guide) == {"alice", "grace"}


@pytest.mark.parametrize("user", NOT_ADMINS)
def test_only_an_admin_can_replace(
    world: World, settings: Settings, guide: Document, user: str
) -> None:
    with pytest.raises(PermissionDeniedError):
        replace_document(
            world.db, world.users[user], guide.id, filename="g.txt", data=V2, settings=settings
        )

    assert guide.status == "active"


def test_replacing_a_document_that_does_not_exist_is_not_found(
    world: World, settings: Settings
) -> None:
    with pytest.raises(NotFoundError):
        replace_document(
            world.db, world.users["admin"], 999999, filename="g.txt", data=V2, settings=settings
        )


# Deleting


def test_deleted_document_is_readable_by_nobody(
    world: World, settings: Settings, guide: Document
) -> None:
    assert readers(world, guide) == {"alice", "grace"}

    delete_document(world.db, world.users["admin"], guide.id)

    assert guide.status == "deleted"
    assert readers(world, guide) == set()
    assert guide.id not in listed_ids(world, "alice")
    with pytest.raises(NotFoundError):
        get_readable_document(world.db, world.users["grace"], guide.public_id)


def test_grant_does_not_keep_a_deleted_document_readable(
    world: World, settings: Settings, guide: Document
) -> None:
    grant_access(world.db, world.users["admin"], guide.id, GrantRequest("carol@acmecorp.example"))

    delete_document(world.db, world.users["admin"], guide.id)

    assert not can_read(world.users["carol"], guide)
    assert guide.id not in listed_ids(world, "carol")


def test_deleting_keeps_the_record(world: World, settings: Settings, guide: Document) -> None:
    before = document_count(world)

    delete_document(world.db, world.users["admin"], guide.id)
    world.db.expire_all()

    stored = world.db.get(Document, guide.id)
    assert document_count(world) == before
    assert stored is not None
    assert stored.content == V1.decode()
    assert stored.uploaded_by.id == world.users["admin"].id


def test_document_cannot_be_deleted_twice(
    world: World, settings: Settings, guide: Document
) -> None:
    delete_document(world.db, world.users["admin"], guide.id)

    with pytest.raises(InvalidInputError, match="replaced or deleted"):
        delete_document(world.db, world.users["admin"], guide.id)


@pytest.mark.parametrize("user", NOT_ADMINS)
def test_only_an_admin_can_delete(
    world: World, settings: Settings, guide: Document, user: str
) -> None:
    with pytest.raises(PermissionDeniedError):
        delete_document(world.db, world.users[user], guide.id)

    assert guide.status == "active"


def test_deleting_a_document_that_does_not_exist_is_not_found(world: World) -> None:
    with pytest.raises(NotFoundError):
        delete_document(world.db, world.users["admin"], 999999)


# Managing a document that is no longer current


def test_retired_documents_cannot_be_granted_or_relabelled(
    world: World, settings: Settings, guide: Document
) -> None:
    new = replace(world, settings, guide, V2)
    delete_document(world.db, world.users["admin"], new.id)

    for document in (guide, new):
        with pytest.raises(InvalidInputError, match="replaced or deleted"):
            grant_access(
                world.db,
                world.users["admin"],
                document.id,
                GrantRequest("carol@acmecorp.example"),
            )
        with pytest.raises(InvalidInputError, match="replaced or deleted"):
            change_labels(world.db, world.users["admin"], document.id, classification="general")

    assert readers(world, guide) == readers(world, new) == set()


# Status in the access decision


@pytest.mark.parametrize("status", [DocumentStatus.SUPERSEDED, DocumentStatus.DELETED])
def test_only_active_documents_are_readable(world: World, status: DocumentStatus) -> None:
    world.grant("runbook", "carol")
    world.documents["runbook"].status = status
    world.db.commit()

    assert readers(world, world.documents["runbook"]) == set()
    for user in world.users:
        assert world.documents["runbook"].id not in listed_ids(world, user)


def test_unknown_status_is_readable_by_nobody(world: World) -> None:
    # Written with SQL, as it would be if someone edited the database by hand.
    world.db.execute(
        text("UPDATE documents SET status = 'archived' WHERE id = :id"),
        {"id": world.documents["leave_policy"].id},
    )
    world.db.commit()
    world.db.expire_all()

    assert readers(world, world.documents["leave_policy"]) == set()
    for user in world.users:
        assert world.documents["leave_policy"].id not in listed_ids(world, user)


def test_status_cannot_be_set_to_an_unknown_value(world: World) -> None:
    with pytest.raises(ValueError, match="is not a valid DocumentStatus"):
        world.documents["runbook"].status = "archived"


# Logging


def test_replace_and_delete_are_logged_with_ids_only(
    world: World, settings: Settings, guide: Document, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="app.ingestion.pipeline")

    new = replace(world, settings, guide, V2)
    delete_document(world.db, world.users["admin"], new.id)

    replaced, deleted = caplog.records[-2:]
    assert replaced.getMessage() == "document replaced"
    assert replaced.__dict__["document_id"] == new.id
    assert replaced.__dict__["replaced_document_id"] == guide.id
    assert replaced.__dict__["version"] == 2
    assert deleted.getMessage() == "document deleted"
    assert deleted.__dict__["document_id"] == new.id
    logged = str(vars(replaced)) + str(vars(deleted))
    assert "Rollback" not in logged
    assert "rollback-guide" not in logged
