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
from typing import Literal

from pydantic import SecretStr
from sqlalchemy import Engine, select
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
    # For starting the service over this company, and signing in to it.
    engine: Engine
    password: str

    def person(self, name: str) -> User:
        return self.db.scalars(select(User).where(User.email == f"{name}@acmecorp.example")).one()


def measuring_settings(
    reranker_model: str | None = None,
    *,
    retrieval_min_score: float | None = None,
    llm_provider: Literal["none", "ollama", "groq"] = "none",
    llm_api_key: SecretStr | None = None,
    llm_model: str | None = None,
    llm_max_output_tokens: int | None = None,
) -> Settings:
    """The service's defaults, with a throwaway key and nothing from the environment.

    A measurement should describe the code, not whatever this machine's
    .env or shell happens to say: validating the values directly reads
    neither. What a measurement changes on purpose is passed in.
    """
    changes: dict[str, object] = {}
    if retrieval_min_score is not None:
        changes["retrieval_min_score"] = retrieval_min_score
    if llm_max_output_tokens is not None:
        changes["llm_max_output_tokens"] = llm_max_output_tokens
    return Settings.model_validate(
        {
            "jwt_secret_key": SecretStr(secrets.token_urlsafe(48)),
            "reranker_model": reranker_model,
            "llm_provider": llm_provider,
            "llm_api_key": llm_api_key,
            "llm_model": llm_model,
            **changes,
        }
    )


@contextmanager
def loaded_company(
    settings: Settings | None = None, embedder: Embedder | None = None
) -> Iterator[Company]:
    """The seeded company and an index built from it."""
    chosen = settings or measuring_settings()
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    password = secrets.token_urlsafe(16)
    try:
        with create_session_factory(engine)() as db:
            seed(db, chosen, password)
            model = embedder or load_embedder(chosen)
            yield Company(db, chosen, model, build_index(db, model), engine, password)
    finally:
        engine.dispose()
