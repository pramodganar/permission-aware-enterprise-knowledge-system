from pathlib import Path

from sqlalchemy import Engine, text

from app.core.database import create_db_engine, create_session_factory


def test_sqlite_enforces_foreign_keys(engine: Engine) -> None:
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_in_memory_database_is_shared_between_sessions(engine: Engine) -> None:
    sessions = create_session_factory(engine)

    with sessions() as first:
        first.execute(text("CREATE TABLE notes (body TEXT)"))
        first.execute(text("INSERT INTO notes VALUES ('kept')"))
        first.commit()

    with sessions() as second:
        assert second.execute(text("SELECT body FROM notes")).scalar() == "kept"


def test_file_database_enforces_foreign_keys(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")

    try:
        with engine.connect() as connection:
            assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
    finally:
        engine.dispose()
