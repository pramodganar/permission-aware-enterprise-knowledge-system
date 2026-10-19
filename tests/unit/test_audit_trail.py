"""The audit trail: append-only, written with the change it records, readable by reviewers only."""

import logging

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session

from app.audit.trail import AuditQuery, AuditWriteError, read_trail, record, write
from app.core.config import Settings
from app.core.exceptions import PermissionDeniedError
from app.core.logging_setup import bind_request_id, clear_request_id
from app.models import AuditAction, AuditRecord, AuditRecordChangedError, User
from app.models.audit import _add_triggers
from logcheck import everything_logged
from scripts.seed import seed


@pytest.fixture
def people(db: Session, settings: Settings) -> dict[str, User]:
    seed(db, settings, "audit-test-password")
    return {user.email.split("@")[0]: user for user in db.scalars(select(User))}


@pytest.fixture
def entry(db: Session, people: dict[str, User]) -> AuditRecord:
    return write(
        db,
        AuditAction.SEARCH,
        "found",
        actor_id=people["david"].id,
        question="How many days of leave?",
        chunk_ids=[3, 1, 2],
    )


def count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(AuditRecord)) or 0


# --- append-only ------------------------------------------------------------------------


def test_a_record_keeps_what_it_was_given(entry: AuditRecord) -> None:
    assert entry.question == "How many days of leave?"
    # In the order they were given, not sorted.
    assert entry.chunk_ids == [3, 1, 2]
    assert entry.created_at.tzinfo is not None


def test_a_record_can_not_be_changed_through_the_orm(db: Session, entry: AuditRecord) -> None:
    entry.outcome = "nothing to see"

    with pytest.raises(AuditRecordChangedError):
        db.commit()


def test_a_record_can_not_be_deleted_through_the_orm(db: Session, entry: AuditRecord) -> None:
    db.delete(entry)

    with pytest.raises(AuditRecordChangedError):
        db.commit()


@pytest.mark.parametrize(
    "statement",
    ["UPDATE audit_records SET outcome = 'nothing'", "DELETE FROM audit_records"],
    ids=["update", "delete"],
)
def test_the_database_refuses_changes_that_skip_the_orm(
    engine: Engine, entry: AuditRecord, statement: str
) -> None:
    with pytest.raises(DatabaseError, match="append-only"), engine.begin() as connection:
        connection.execute(text(statement))


def test_an_unknown_action_can_not_be_recorded(db: Session, people: dict[str, User]) -> None:
    with pytest.raises(ValueError):
        record(db, "document.stolen", "done", actor_id=people["henry"].id)  # type: ignore[arg-type]


# --- writing ----------------------------------------------------------------------------


def test_record_waits_for_the_callers_commit(db: Session, people: dict[str, User]) -> None:
    before = count(db)

    record(db, AuditAction.DOCUMENT_DELETED, "done", actor_id=people["henry"].id)
    db.rollback()

    assert count(db) == before


def test_a_record_carries_the_request_id(db: Session, people: dict[str, User]) -> None:
    request_id = bind_request_id("trace-123")
    try:
        entry = write(db, AuditAction.SEARCH, "found", actor_id=people["david"].id)
    finally:
        clear_request_id()

    assert request_id == "trace-123"
    assert entry.request_id == "trace-123"


def test_when_a_record_can_not_be_stored_write_says_so_and_logs_no_values(
    db: Session, engine: Engine, people: dict[str, User], caplog: pytest.LogCaptureFixture
) -> None:
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE audit_records"))

    with caplog.at_level(logging.ERROR), pytest.raises(AuditWriteError) as raised:
        write(
            db,
            AuditAction.QUESTION_ASKED,
            "answered",
            actor_id=people["david"].id,
            question="Who is on a performance plan?",
        )

    assert raised.value.status_code == 503
    assert "audit record not written" in caplog.text
    assert "performance plan" not in everything_logged(caplog)
    # The session is usable again.
    assert db.get(User, people["david"].id) is not None


# --- reading ----------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["david", "grace", "henry"])
def test_only_a_reviewer_may_read_the_trail(
    db: Session, people: dict[str, User], entry: AuditRecord, name: str
) -> None:
    before = count(db)

    with pytest.raises(PermissionDeniedError):
        read_trail(db, people[name], AuditQuery())

    assert count(db) == before


def test_a_reviewer_reads_newest_first_and_the_read_is_recorded(
    db: Session, people: dict[str, User], entry: AuditRecord
) -> None:
    later = write(db, AuditAction.SEARCH, "no_information", actor_id=people["alice"].id)

    page = read_trail(db, people["irene"], AuditQuery(action=AuditAction.SEARCH))

    assert [r.id for r in page.records] == [later.id, entry.id]
    assert page.emails == {
        people["alice"].id: "alice@acmecorp.example",
        people["david"].id: "david@acmecorp.example",
    }
    newest = db.scalars(select(AuditRecord).order_by(AuditRecord.id.desc())).first()
    assert newest is not None
    assert (newest.action, newest.actor_id) == ("audit.read", people["irene"].id)
    assert newest.details["records"] == 2


def test_a_record_shows_its_action_and_outcome_but_never_the_question(entry: AuditRecord) -> None:
    shown = repr(entry)

    assert "search" in shown
    assert "found" in shown
    assert "leave" not in shown


def test_the_triggers_are_only_added_on_sqlite() -> None:
    executed: list[object] = []

    class OtherDatabase:
        dialect = type("Dialect", (), {"name": "postgresql"})()

        def execute(self, statement: object) -> None:
            executed.append(statement)

    _add_triggers(AuditRecord.__table__, OtherDatabase())

    assert executed == []
