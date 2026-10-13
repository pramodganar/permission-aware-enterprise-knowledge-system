"""Build the vector index from the database and save it.

    python -m scripts.build_index

Embeds the chunks of every active document with the configured model and
writes the index to APP_INDEX_DIRECTORY. Run it after documents are added,
replaced or deleted, or after the embedding model or chunk settings change.
"""

import sys

from app.core.config import get_settings
from app.core.database import Base, create_db_engine, create_session_factory
from app.core.logging_setup import configure_logging
from app.retrieval.embeddings import EmbeddingError, load_embedder
from app.retrieval.index import VectorIndexError, build_index
from app.retrieval.index_store import save_index


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)

    embedder = load_embedder(settings)
    engine = create_db_engine(settings.database_url)
    try:
        Base.metadata.create_all(engine)
        with create_session_factory(engine)() as db:
            index = build_index(db, embedder)
        manifest = save_index(index, embedder, settings, settings.index_directory)
    except (EmbeddingError, VectorIndexError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()

    print(f"chunks indexed: {manifest.chunk_count}")
    print(f"model:          {manifest.embedding_model}")
    print(f"saved to:       {settings.index_directory}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
