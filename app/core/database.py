"""Database engine, session factory and the base class for models."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Dialect, Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

IN_MEMORY_SQLITE = ("sqlite://", "sqlite:///:memory:")

# The largest ID the database can hold. Every ID a client sends is checked
# against it, so a bigger number is refused as invalid input rather than
# reaching the database and failing there with a 500.
MAX_ID = 2**63 - 1

# The name of the key that seals audit records, in each session's info.
AUDIT_KEY = "audit_key"


class Base(DeclarativeBase):
    pass


def utc_now() -> datetime:
    """Default for timestamp columns."""
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator[datetime]):
    """A timestamp column that is timezone-aware UTC on every database.

    SQLite stores datetimes without a timezone and returns them naive, which
    makes comparisons such as "has this grant expired?" fail or, worse, give
    the wrong answer. This type refuses naive values on the way in and always
    returns UTC on the way out.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


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


def create_session_factory(engine: Engine, audit_key: bytes | None = None) -> sessionmaker[Session]:
    """Sessions for ``engine``. With ``audit_key``, every audit record they write is sealed."""
    info = {} if audit_key is None else {AUDIT_KEY: audit_key}
    return sessionmaker(bind=engine, expire_on_commit=False, info=info)
