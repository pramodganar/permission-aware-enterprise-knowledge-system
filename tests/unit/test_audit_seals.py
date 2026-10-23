"""Finding out that the audit trail was changed (threat T22, decision record 0013).

Each test plays someone who can do anything to the database, triggers and
foreign keys included, but doesn't have the service's key, and checks that
what they did is found. One thing isn't, and a test says so: the newest
records deleted, seals and all. The application log is what shows that.
"""

import logging
from collections.abc import Iterator

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from app.audit.seals import check_trail, seal_key
from app.audit.trail import record, write
from app.core.config import Settings
from app.core.database import Base, create_db_engine, create_session_factory
from app.models import AuditAction, AuditRecord, AuditSeal, seal_of
from scripts import audit as audit_command

QUESTIONS = ["How many days of annual leave do I get?", "Who approves a large payment?"]


@pytest.fixture
def trail(db: Session) -> list[AuditRecord]:
    """Five records of the kinds the service writes, sealed as it seals them."""
    written = [
        write(db, AuditAction.LOGIN_FAILED, "failed", actor_id=None, details={"reason": "x"}),
        *(
            write(
                db,
                AuditAction.QUESTION_ASKED,
                "answered",
                actor_id=None,
                question=question,
                chunk_ids=[3, 1, 2],
                details={"model": "fake", "filters": {"departments": ["hr"]}, "tokens": 12},
            )
            for question in QUESTIONS
        ),
        write(db, AuditAction.SEARCH, "found", actor_id=None, question="Wörter, ünïcode"),
        write(db, AuditAction.AUDIT_READ, "allowed", actor_id=None, details={"records": 4}),
    ]
    return written


Statement = str | tuple[str, dict[str, object]]


def as_the_database_owner(engine: Engine, *statements: Statement) -> None:
    """Run SQL with nothing in the way: no triggers, no foreign keys."""
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        for table in ("audit_records", "audit_seals"):
            for operation in ("update", "delete"):
                connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {table}_no_{operation}")
        for statement in statements:
            sql, values = (statement, {}) if isinstance(statement, str) else statement
            connection.execute(text(sql), values)
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def problems(db: Session, settings: Settings) -> list[str]:
    db.expire_all()
    return check_trail(db, seal_key(settings)).problems


def test_a_trail_as_written_is_intact(
    db: Session, settings: Settings, trail: list[AuditRecord]
) -> None:
    found = check_trail(db, seal_key(settings))

    assert found.intact
    assert (found.records, found.sealed, found.last_id) == (5, 5, 5)


def test_the_seal_key_is_not_the_signing_key_and_another_key_matches_nothing(
    db: Session, settings: Settings, trail: list[AuditRecord]
) -> None:
    other = Settings(_env_file=None, jwt_secret_key=SecretStr("another-key-" + "y" * 60))

    assert seal_key(settings) != settings.jwt_secret_key.get_secret_value().encode()
    assert len(check_trail(db, seal_key(other)).problems) == len(trail)


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE audit_records SET question = 'Something harmless' WHERE id = 2",
        "UPDATE audit_records SET outcome = 'no_information' WHERE id = 2",
        "UPDATE audit_records SET chunk_ids = '[3, 1]' WHERE id = 2",
        'UPDATE audit_records SET details = \'{"model": "fake"}\' WHERE id = 2',
        "UPDATE audit_records SET actor_id = 1 WHERE id = 2",
        "UPDATE audit_records SET created_at = '2026-01-01 00:00:00.000000' WHERE id = 2",
        "UPDATE audit_records SET request_id = 'another' WHERE id = 2",
        "UPDATE audit_records SET action = 'search' WHERE id = 2",
    ],
)
def test_a_record_changed_in_any_field_no_longer_matches_its_seal(
    db: Session, engine: Engine, settings: Settings, trail: list[AuditRecord], change: str
) -> None:
    as_the_database_owner(engine, change)

    assert problems(db, settings) == ["record 2 does not match its seal"]


def test_a_record_deleted_from_the_middle_leaves_a_gap(
    db: Session, engine: Engine, settings: Settings, trail: list[AuditRecord]
) -> None:
    as_the_database_owner(
        engine,
        "DELETE FROM audit_seals WHERE record_id IN (2, 3)",
        "DELETE FROM audit_records WHERE id IN (2, 3)",
    )

    assert problems(db, settings) == ["records 2 to 3 are missing"]


def test_two_records_swapped_match_neither_seal(
    db: Session, engine: Engine, settings: Settings, trail: list[AuditRecord]
) -> None:
    # Moving the later question to the earlier ID, and back.
    as_the_database_owner(
        engine,
        "UPDATE audit_records SET id = 100 WHERE id = 2",
        "UPDATE audit_records SET id = 2 WHERE id = 3",
        "UPDATE audit_records SET id = 3 WHERE id = 100",
    )

    assert problems(db, settings) == [
        "record 2 does not match its seal",
        "record 3 does not match its seal",
    ]


def test_a_record_moved_down_with_its_seal_into_a_deleted_ones_place_is_found(
    db: Session, engine: Engine, settings: Settings, trail: list[AuditRecord]
) -> None:
    # Record 4 deleted, and record 5 moved to 4 with its seal: no gap is left.
    as_the_database_owner(
        engine,
        "DELETE FROM audit_seals WHERE record_id = 4",
        "DELETE FROM audit_records WHERE id = 4",
        "UPDATE audit_records SET id = 4 WHERE id = 5",
        "UPDATE audit_seals SET record_id = 4 WHERE record_id = 5",
    )

    # Only the ID inside the seal shows it.
    assert problems(db, settings) == ["record 4 does not match its seal"]


def test_a_record_added_with_a_made_up_seal_is_found(
    db: Session, engine: Engine, settings: Settings, trail: list[AuditRecord]
) -> None:
    forged = AuditRecord(
        id=6,
        created_at=trail[-1].created_at,
        action="search",
        outcome="found",
        question="Nothing to see",
        chunk_ids=[],
        details={},
    )
    made_up = seal_of(forged, b"a key that is not the service's" * 2)
    as_the_database_owner(
        engine,
        (
            "INSERT INTO audit_records (id, created_at, action, outcome, question, chunk_ids,"
            " details) VALUES (6, :at, 'search', 'found', 'Nothing to see', '[]', '{}')",
            {"at": f"{trail[-1].created_at:%Y-%m-%d %H:%M:%S.%f}"},
        ),
        ("INSERT INTO audit_seals (record_id, mac) VALUES (6, :mac)", {"mac": made_up}),
    )

    assert problems(db, settings) == ["record 6 does not match its seal"]


def test_a_seal_removed_or_left_without_its_record_is_found(
    db: Session, engine: Engine, settings: Settings, trail: list[AuditRecord]
) -> None:
    as_the_database_owner(engine, "DELETE FROM audit_seals WHERE record_id = 3")
    assert problems(db, settings) == ["record 3 has no seal"]

    as_the_database_owner(engine, "DELETE FROM audit_records WHERE id = 3")
    assert problems(db, settings) == ["record 3 is missing"]


def test_a_seal_whose_record_was_deleted_is_found(
    db: Session, engine: Engine, settings: Settings, trail: list[AuditRecord]
) -> None:
    as_the_database_owner(engine, "DELETE FROM audit_records WHERE id = 5")

    assert problems(db, settings) == ["seal 5 has no record"]


def test_the_newest_records_deleted_with_their_seals_show_only_in_the_log(
    db: Session, engine: Engine, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app.models.audit"):
        for question in QUESTIONS:
            write(db, AuditAction.SEARCH, "found", actor_id=None, question=question)
    logged = [r.audit_id for r in caplog.records if r.message == "audit record sealed"]  # type: ignore[attr-defined]

    as_the_database_owner(
        engine,
        "DELETE FROM audit_seals WHERE record_id = 2",
        "DELETE FROM audit_records WHERE id = 2",
    )

    found = check_trail(db, seal_key(settings))
    # The check can't see it: the trail is short, not changed.
    assert found.intact
    # The log, kept apart from the database, names a record the trail doesn't have.
    assert max(logged) == 2
    assert found.last_id == 1


def test_a_record_rolled_back_is_neither_sealed_nor_logged(
    db: Session, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app.models.audit"):
        record(db, AuditAction.SEARCH, "found", actor_id=None, question=QUESTIONS[0])
        db.flush()
        db.rollback()

    assert db.scalars(select(AuditSeal)).all() == []
    assert not [r for r in caplog.records if r.message == "audit record sealed"]
    assert check_trail(db, seal_key(settings)).records == 0

    # The next commit logs its own record only, never the one rolled back.
    with caplog.at_level(logging.INFO, logger="app.models.audit"):
        kept = write(db, AuditAction.SEARCH, "found", actor_id=None, question=QUESTIONS[1])
    sealed = [r for r in caplog.records if r.message == "audit record sealed"]
    assert [r.audit_id for r in sealed] == [kept.id]  # type: ignore[attr-defined]


def test_records_from_before_sealing_are_counted_and_only_they_may_be_unsealed(
    engine: Engine, settings: Settings
) -> None:
    unsealed = create_session_factory(engine)
    sealed = create_session_factory(engine, seal_key(settings))
    with unsealed() as db:
        write(db, AuditAction.SEARCH, "found", actor_id=None, question=QUESTIONS[0])
    with sealed() as db:
        write(db, AuditAction.SEARCH, "found", actor_id=None, question=QUESTIONS[1])
        found = check_trail(db, seal_key(settings))
        assert (found.intact, found.before_sealing, found.sealed) == (True, 1, 1)
    with unsealed() as db:
        write(db, AuditAction.SEARCH, "found", actor_id=None, question=QUESTIONS[0])
        assert check_trail(db, seal_key(settings)).problems == ["record 3 has no seal"]


# --- the command ------------------------------------------------------------------------


@pytest.fixture
def on_disk(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    url = f"sqlite:///{tmp_path}/trail.db"
    monkeypatch.setenv("APP_DATABASE_URL", url)
    monkeypatch.setenv("APP_JWT_SECRET_KEY", "command-test-" + "z" * 60)
    monkeypatch.setattr(audit_command, "configure_logging", lambda level: None)
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def test_the_command_says_intact_or_lists_each_problem(
    on_disk: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    key = seal_key(Settings(_env_file=None, jwt_secret_key=SecretStr("command-test-" + "z" * 60)))
    with create_session_factory(on_disk, key)() as db:
        for question in QUESTIONS:
            write(db, AuditAction.SEARCH, "found", actor_id=None, question=question)

    assert audit_command.main(["check"]) == 0
    assert "highest ID: 2" in capsys.readouterr().out

    as_the_database_owner(on_disk, "UPDATE audit_records SET question = 'x' WHERE id = 1")

    assert audit_command.main(["check"]) == audit_command.CHANGED
    out = capsys.readouterr().out
    assert "PROBLEM: record 1 does not match its seal" in out
    # What the records say is never printed.
    assert QUESTIONS[1] not in out
