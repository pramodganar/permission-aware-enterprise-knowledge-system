"""The AcmeCorp company, loaded fresh for a measurement.

Every run starts from the same place: an in-memory database seeded from
data/acmecorp, and an index built from it with the embedding model in the
settings. Nothing is read from or written to the database or index a
running service uses, so measuring never changes them, and two runs on the
same code give the same chunks.
"""

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import Base, create_db_engine, create_session_factory
from app.models import User
from app.retrieval.embeddings import Embedder, load_embedder
from app.retrieval.index import FaissVectorIndex, build_index
from scripts.seed import seed


@dataclass
class Company:
    db: Session
    settings: Settings
    embedder: Embedder
    index: FaissVectorIndex

    def person(self, name: str) -> User:
        return self.db.scalars(select(User).where(User.email == f"{name}@acmecorp.example")).one()


def measuring_settings(reranker_model: str | None = None) -> Settings:
    """The service's defaults, with nothing taken from .env and a throwaway key.

    A measurement should describe the code, not whatever this machine's
    .env happens to say.
    """
    return Settings(
        _env_file=None,
        jwt_secret_key=SecretStr(secrets.token_urlsafe(48)),
        reranker_model=reranker_model,
    )


@contextmanager
def loaded_company(
    settings: Settings | None = None, embedder: Embedder | None = None
) -> Iterator[Company]:
    """The seeded company and an index built from it."""
    chosen = settings or measuring_settings()
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with create_session_factory(engine)() as db:
            seed(db, chosen, secrets.token_urlsafe(16))
            model = embedder or load_embedder(chosen)
            yield Company(db, chosen, model, build_index(db, model))
    finally:
        engine.dispose()
