from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.core.database import Base, create_db_engine, create_session_factory
from app.models import Department


@pytest.fixture
def engine() -> Iterator[Engine]:
    """A fresh in-memory database for each test."""
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    with create_session_factory(engine)() as session:
        yield session


@pytest.fixture
def engineering(db: Session) -> Department:
    department = Department(code="engineering", name="Engineering")
    db.add(department)
    db.commit()
    return department
