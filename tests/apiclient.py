"""The real app over the seeded company, for testing the API the way a client uses it.

The fake embedder and model stand in for the slow parts; everything else is
the service as it runs. Log in as any of the nine people with ``headers``.
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.api.components import Components
from app.core.config import Settings
from app.main import create_app
from app.models import Document
from app.rag.llm import LLMProvider
from app.retrieval.index import build_index
from app.retrieval.reranking import Reranker
from fakes import FakeEmbedder
from scripts.seed import seed

PASSWORD = "api-test-password"
# A document ID no document has: real ones are random, and never this.
MISSING = UUID("0badc0de-0000-4000-8000-000000000000")
# A document named by its number in the database, as in /admin/documents/<3>/grants.
_NUMBERED = re.compile(r"<(\d+)>")


def email(name: str) -> str:
    return f"{name}@acmecorp.example"


def company_app(
    db: Session,
    engine: Engine,
    settings: Settings,
    provider: LLMProvider | None = None,
    reranker: Reranker | None = None,
) -> FastAPI:
    """The service over the seeded company, not yet started."""
    seed(db, settings, PASSWORD)
    embedder = FakeEmbedder()
    components = Components(
        embedder=embedder,
        index=build_index(db, embedder),
        provider=provider,
        reranker=reranker,
    )
    return create_app(settings, engine=engine, components=components)


@contextmanager
def company_client(
    db: Session,
    engine: Engine,
    settings: Settings,
    provider: LLMProvider | None = None,
    reranker: Reranker | None = None,
) -> Iterator[TestClient]:
    app = company_app(db, engine, settings, provider, reranker)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def token(client: TestClient, name: str) -> str:
    response = client.post("/auth/login", json={"email": email(name), "password": PASSWORD})
    assert response.status_code == 200, response.text
    value: str = response.json()["access_token"]
    return value


def headers(client: TestClient, name: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(client, name)}"}


def public_id(client: TestClient, number: int) -> UUID:
    """The public ID of the document the database numbers ``number``."""
    with client.app.state.session_factory() as db:  # type: ignore[attr-defined]
        found: UUID = db.scalars(select(Document).where(Document.id == number)).one().public_id
    return found


def public_path(client: TestClient, path: str) -> str:
    """The path with each <n> in it changed to that document's public ID."""
    return _NUMBERED.sub(lambda m: str(public_id(client, int(m.group(1)))), path)
