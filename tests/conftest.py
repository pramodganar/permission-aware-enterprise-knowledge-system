import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from hypothesis import settings as hypothesis_settings
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from acmecorp import World, build_company, build_world
from app.audit.seals import seal_key
from app.core.config import Settings
from app.core.database import Base, create_db_engine, create_session_factory
from app.models import Department

# Generated inputs are the same on every run, so a failure can be run again,
# and nothing is saved between runs. A parser can take a while on a damaged
# file, so there is no time limit per example.
hypothesis_settings.register_profile("project", derandomize=True, database=None, deadline=None)
hypothesis_settings.load_profile("project")

# Longer than the default time limit (pyproject.toml), for the kinds that do
# more: a first image build downloads PyTorch and the model, and a local model
# takes minutes to answer one question.
TIME_LIMITS = {"docker": 3600, "llm": 1800, "model": 900}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        limits = [seconds for kind, seconds in TIME_LIMITS.items() if item.get_closest_marker(kind)]
        if limits and item.get_closest_marker("timeout") is None:
            item.add_marker(pytest.mark.timeout(max(limits)))


# The database a developer runs the service on. Settings point at it by
# default, and the service creates any table it is missing at startup, so a
# test that started the app on the default settings changed it.
LOCAL_DATABASE = Path(__file__).resolve().parents[1] / "data" / "app.db"


def _fingerprint(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


@pytest.fixture(scope="session", autouse=True)
def local_database_untouched() -> Iterator[None]:
    """Fail the run if any test changed, created or deleted the local database."""
    before = _fingerprint(LOCAL_DATABASE)
    yield
    assert _fingerprint(LOCAL_DATABASE) == before, "a test changed data/app.db"


@pytest.fixture
def engine() -> Iterator[Engine]:
    """A fresh in-memory database for each test."""
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db(engine: Engine, settings: Settings) -> Iterator[Session]:
    # Sealed as the service seals them, so every record a test writes is too.
    with create_session_factory(engine, seal_key(settings))() as session:
        yield session


@pytest.fixture
def engineering(db: Session) -> Department:
    department = Department(code="engineering", name="Engineering")
    db.add(department)
    db.commit()
    return department


@pytest.fixture
def settings() -> Settings:
    # In memory, never the default file: see LOCAL_DATABASE. Tests that walk
    # every question as one person would be limited after ten, so the limits
    # are as high as they go; the tests of the limits set their own.
    return Settings(
        _env_file=None,
        jwt_secret_key=SecretStr("conftest-secret-" + "x" * 60),
        database_url="sqlite://",
        ask_limit_per_minute=10_000,
        ask_limit_per_day=100_000,
        search_limit_per_minute=10_000,
    )


@pytest.fixture
def world(db: Session) -> World:
    return build_world(db)


@pytest.fixture
def company(db: Session) -> World:
    return build_company(db)
