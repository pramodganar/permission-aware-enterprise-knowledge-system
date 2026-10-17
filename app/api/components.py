"""The slow parts the API loads once at startup, and how endpoints get them.

The embedding model, the vector index and the language model provider are
loaded when the service starts, not per request: the model takes seconds to
load, the index is checked against its manifest.

The service refuses to start without a usable index: one that is missing,
damaged, or built with another embedding model or other chunk settings
(NFR-6). An index that is merely behind the database starts, with a warning
in the log. That is safe: access is decided from the database on every
question, so a document deleted or locked since the build is not served
from it. What is missing is the documents added since, until the index is
rebuilt (NFR-7).
"""

import logging
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.rag.llm import LLMProvider
from app.rag.providers import load_provider
from app.retrieval.embeddings import Embedder, load_embedder
from app.retrieval.index import VectorIndex
from app.retrieval.index_versions import check_freshness, load_current_index

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Components:
    embedder: Embedder
    index: VectorIndex
    # None when answers are switched off (APP_LLM_PROVIDER=none).
    provider: LLMProvider | None


def load_components(settings: Settings, db: Session) -> Components:
    """Load the model, the current index and the provider named in the settings."""
    embedder = load_embedder(settings)
    index, manifest = load_current_index(settings, settings.index_directory)
    freshness = check_freshness(db, index, manifest)
    if not freshness.up_to_date:
        logger.warning(
            "the vector index is behind the database; "
            "rebuild it with: python -m scripts.index build",
            extra={"missing": freshness.missing, "retired": freshness.retired},
        )
    return Components(embedder=embedder, index=index, provider=load_provider(settings))


def close_components(components: Components) -> None:
    """Close what holds connections open. Only the provider does."""
    close = getattr(components.provider, "close", None)
    if callable(close):
        close()


def get_components(request: Request) -> Components:
    components: Components = request.app.state.components
    return components


Loaded = Annotated[Components, Depends(get_components)]
