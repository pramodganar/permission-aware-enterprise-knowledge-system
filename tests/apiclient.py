"""The real app over the seeded company, for testing the API the way a client uses it.

The fake embedder and model stand in for the slow parts; everything else is
the service as it runs. Log in as any of the nine people with ``headers``.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.api.components import Components
from app.core.config import Settings
from app.main import create_app
from app.rag.llm import LLMProvider
from app.retrieval.index import build_index
from app.retrieval.reranking import Reranker
from fakes import FakeEmbedder
from scripts.seed import seed

PASSWORD = "api-test-password"


def email(name: str) -> str:
    return f"{name}@acmecorp.example"


@contextmanager
def company_client(
    db: Session,
    engine: Engine,
    settings: Settings,
    provider: LLMProvider | None = None,
    reranker: Reranker | None = None,
) -> Iterator[TestClient]:
    seed(db, settings, PASSWORD)
    embedder = FakeEmbedder()
    components = Components(
        embedder=embedder,
        index=build_index(db, embedder),
        provider=provider,
        reranker=reranker,
    )
    app = create_app(settings, engine=engine, components=components)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def token(client: TestClient, name: str) -> str:
    response = client.post("/auth/login", json={"email": email(name), "password": PASSWORD})
    assert response.status_code == 200, response.text
    value: str = response.json()["access_token"]
    return value


def headers(client: TestClient, name: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(client, name)}"}
