from collections.abc import Iterator

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from acmecorp import World, build_company, build_world
from app.core.config import Settings
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


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, jwt_secret_key=SecretStr("conftest-secret-" + "x" * 60))


@pytest.fixture
def world(db: Session) -> World:
    return build_world(db)


@pytest.fixture
def company(db: Session) -> World:
    return build_company(db)
