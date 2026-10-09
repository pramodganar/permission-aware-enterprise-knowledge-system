from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from acmecorp import file_details
from app.models import Classification, Department, Document, DocumentGrant, User

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def alice(db: Session, engineering: Department) -> User:
    user = User(
        email="alice@acmecorp.example",
        full_name="Alice Example",
        hashed_password="x",
        department=engineering,
    )
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def runbook(db: Session, engineering: Department, alice: User) -> Document:
    document = Document(
        title="Deployment Runbook",
        department=engineering,
        classification=Classification.DEPARTMENTAL,
        **file_details(alice),
    )
    db.add(document)
    db.commit()
    return document


def test_document_is_saved_and_read_back(db: Session, runbook: Document) -> None:
    db.expire_all()

    document = db.scalars(select(Document)).one()

    assert document.title == "Deployment Runbook"
    assert document.department.code == "engineering"
    assert document.classification == "departmental"
    assert document.grants == []


@pytest.mark.parametrize("classification", list(Classification))
def test_every_classification_can_be_stored(
    db: Session, engineering: Department, alice: User, classification: Classification
) -> None:
    document = Document(
        title="Doc",
        department=engineering,
        classification=classification,
        **file_details(alice),
    )
    db.add(document)
    db.commit()

    assert document.classification == classification


def test_classification_has_no_default(db: Session, engineering: Department, alice: User) -> None:
    db.add(Document(title="Unlabelled", department=engineering, **file_details(alice)))

    with pytest.raises(IntegrityError):
        db.commit()


def test_owning_department_is_required(db: Session, alice: User) -> None:
    db.add(Document(title="Orphan", classification=Classification.GENERAL, **file_details(alice)))

    with pytest.raises(IntegrityError):
        db.commit()


def test_owning_department_must_exist(db: Session, alice: User) -> None:
    db.add(
        Document(
            title="Ghost",
            department_id=999,
            classification=Classification.GENERAL,
            **file_details(alice),
        )
    )

    with pytest.raises(IntegrityError):
        db.commit()


@pytest.mark.parametrize("value", ["public", "confidential", "secret", "", "all"])
def test_unknown_classification_is_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="is not a valid Classification"):
        Document(title="Doc", classification=value)


def test_classification_is_stored_lowercase() -> None:
    assert Document(title="Doc", classification=" Restricted ").classification == "restricted"


def test_grant_is_saved_with_who_gave_it(db: Session, runbook: Document, alice: User) -> None:
    db.add(DocumentGrant(document=runbook, user_id=alice.id, granted_by_id=alice.id))
    db.commit()
    db.expire_all()

    grant = db.scalars(select(DocumentGrant)).one()

    assert grant.document_id == runbook.id
    assert grant.user_id == alice.id
    assert grant.granted_by_id == alice.id
    assert grant.expires_at is None
    assert runbook.grants == [grant]


def test_a_user_has_at_most_one_grant_per_document(
    db: Session, runbook: Document, alice: User
) -> None:
    db.add(DocumentGrant(document=runbook, user_id=alice.id, granted_by_id=alice.id))
    db.commit()

    db.add(DocumentGrant(document=runbook, user_id=alice.id, granted_by_id=alice.id))
    with pytest.raises(IntegrityError):
        db.commit()


@pytest.mark.parametrize("field", ["user_id", "granted_by_id", "document_id"])
def test_grant_must_point_at_real_rows(
    db: Session, runbook: Document, alice: User, field: str
) -> None:
    fields = {"document_id": runbook.id, "user_id": alice.id, "granted_by_id": alice.id}
    fields[field] = 999

    db.add(DocumentGrant(**fields))
    with pytest.raises(IntegrityError):
        db.commit()


def test_grant_without_expiry_is_always_valid() -> None:
    assert DocumentGrant().is_valid_at(NOW) is True


@pytest.mark.parametrize(
    ("expires_in", "valid"),
    [
        (timedelta(days=30), True),
        (timedelta(seconds=1), True),
        (timedelta(0), False),
        (timedelta(seconds=-1), False),
        (timedelta(days=-30), False),
    ],
)
def test_grant_is_valid_only_before_it_expires(expires_in: timedelta, valid: bool) -> None:
    assert DocumentGrant(expires_at=NOW + expires_in).is_valid_at(NOW) is valid


def test_expiry_survives_the_database_as_a_utc_timestamp(
    db: Session, runbook: Document, alice: User
) -> None:
    expires = NOW + timedelta(days=7)
    db.add(
        DocumentGrant(
            document=runbook, user_id=alice.id, granted_by_id=alice.id, expires_at=expires
        )
    )
    db.commit()
    db.expire_all()

    grant = db.scalars(select(DocumentGrant)).one()

    assert grant.expires_at == expires
    assert grant.expires_at is not None
    assert grant.expires_at.tzinfo is not None
    assert grant.is_valid_at(NOW) is True
    assert grant.is_valid_at(expires) is False


def test_expiry_in_another_timezone_is_converted_not_shifted(
    db: Session, runbook: Document, alice: User
) -> None:
    india = timezone(timedelta(hours=5, minutes=30))
    expires = datetime(2026, 10, 8, 17, 30, tzinfo=india)
    db.add(
        DocumentGrant(
            document=runbook, user_id=alice.id, granted_by_id=alice.id, expires_at=expires
        )
    )
    db.commit()
    db.expire_all()

    grant = db.scalars(select(DocumentGrant)).one()

    assert grant.expires_at == datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def test_expiry_without_a_timezone_is_refused(db: Session, runbook: Document, alice: User) -> None:
    db.add(
        DocumentGrant(
            document=runbook,
            user_id=alice.id,
            granted_by_id=alice.id,
            expires_at=datetime(2026, 10, 8, 12, 0),
        )
    )

    with pytest.raises(StatementError, match="timezone-aware"):
        db.commit()


def test_deleting_a_document_removes_its_grants(
    db: Session, runbook: Document, alice: User
) -> None:
    db.add(DocumentGrant(document=runbook, user_id=alice.id, granted_by_id=alice.id))
    db.commit()

    db.delete(runbook)
    db.commit()

    assert db.scalars(select(DocumentGrant)).all() == []


def test_model_timestamps_are_timezone_aware(db: Session, runbook: Document, alice: User) -> None:
    db.expire_all()

    assert db.scalars(select(Document)).one().created_at.tzinfo is not None
    assert db.scalars(select(User)).one().created_at.tzinfo is not None
    assert db.scalars(select(Department)).one().created_at.tzinfo is not None


# File details


def test_file_details_are_saved(db: Session, runbook: Document, alice: User) -> None:
    db.expire_all()

    document = db.scalars(select(Document)).one()

    assert document.source_filename == "document.txt"
    assert document.file_type == ".txt"
    assert document.size_bytes == 1
    assert document.sha256 == "0" * 64
    assert document.uploaded_by.id == alice.id
    assert document.version == 1
    assert document.updated_at.tzinfo is not None


@pytest.mark.parametrize(
    "field", ["source_filename", "file_type", "size_bytes", "sha256", "uploaded_by"]
)
def test_file_details_are_required(
    db: Session, engineering: Department, alice: User, field: str
) -> None:
    fields = file_details(alice)
    del fields[field]

    db.add(
        Document(
            title="Doc",
            department=engineering,
            classification=Classification.GENERAL,
            **fields,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


def test_uploader_must_be_a_real_user(db: Session, engineering: Department, alice: User) -> None:
    fields = file_details(alice)
    del fields["uploaded_by"]

    db.add(
        Document(
            title="Doc",
            department=engineering,
            classification=Classification.GENERAL,
            uploaded_by_id=999,
            **fields,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
