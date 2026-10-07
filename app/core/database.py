"""Database engine, session factory and the base class for models."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

IN_MEMORY_SQLITE = ("sqlite://", "sqlite:///:memory:")


class Base(DeclarativeBase):
    pass


def utc_now() -> datetime:
    """Default for timestamp columns."""
    return datetime.now(UTC)


def create_db_engine(database_url: str) -> Engine:
    """Create an engine for the given URL.

    SQLite needs two adjustments. Foreign keys are not enforced unless they are
    switched on per connection, and an in-memory database only exists on one
    connection, so that connection has to be shared.
    """
    if not database_url.startswith("sqlite"):
        return create_engine(database_url)

    options: dict[str, Any] = {"connect_args": {"check_same_thread": False}}
    if database_url in IN_MEMORY_SQLITE:
        options["poolclass"] = StaticPool
    engine = create_engine(database_url, **options)

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection: Any, _: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
