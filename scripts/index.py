"""Build, inspect and roll back the vector index.

    python -m scripts.index build              build a new version and make it current
    python -m scripts.index status             show the versions and whether the index is up to date
    python -m scripts.index rollback [VERSION] go back to an earlier version

Exit codes: 0 fine, 1 error, 2 for "status" when the index is out of date, so
a scheduled check can tell the three apart.
"""

import argparse
import sys

from app.core.config import Settings, get_settings
from app.core.database import Base, create_db_engine, create_session_factory
from app.core.logging_setup import configure_logging
from app.retrieval.embeddings import EmbeddingError, load_embedder
from app.retrieval.index import VectorIndexError
from app.retrieval.index_versions import (
    check_freshness,
    list_versions,
    load_current_index,
    publish_index,
    rollback_index,
)

OUT_OF_DATE = 2


def build(settings: Settings) -> int:
    embedder = load_embedder(settings)
    engine = create_db_engine(settings.database_url)
    try:
        Base.metadata.create_all(engine)
        with create_session_factory(engine)() as db:
            manifest = publish_index(db, embedder, settings, settings.index_directory)
    finally:
        engine.dispose()

    print(f"version {manifest.version} is now current")
    print(f"chunks indexed: {manifest.chunk_count}")
    print(f"model:          {manifest.embedding_model}")
    return 0


def status(settings: Settings) -> int:
    for version in list_versions(settings, settings.index_directory):
        marker = "*" if version.current else " "
        if version.manifest is None:
            print(f"{marker} {version.number:06d}  can't be used: {version.problem}")
        else:
            built = version.manifest.built_at.strftime("%Y-%m-%d %H:%M UTC")
            chunks = version.manifest.chunk_count
            print(f"{marker} {version.number:06d}  {chunks} chunks, built {built}")

    index, manifest = load_current_index(settings, settings.index_directory)
    engine = create_db_engine(settings.database_url)
    try:
        with create_session_factory(engine)() as db:
            freshness = check_freshness(db, index, manifest)
    finally:
        engine.dispose()

    if freshness.up_to_date:
        print(f"version {manifest.version} is up to date")
        return 0
    print(
        f"version {manifest.version} is out of date: {freshness.missing} chunks not indexed, "
        f"{freshness.retired} indexed chunks no longer active. Run: python -m scripts.index build"
    )
    return OUT_OF_DATE


def rollback(settings: Settings, version: int | None) -> int:
    manifest = rollback_index(settings, settings.index_directory, version)
    print(f"version {manifest.version} is now current")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build, inspect and roll back the vector index.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("build", help="build a new version and make it current")
    commands.add_parser("status", help="show the versions and whether the index is up to date")
    back = commands.add_parser("rollback", help="go back to an earlier version")
    back.add_argument("version", nargs="?", type=int, help="default: the previous usable version")
    arguments = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        if arguments.command == "build":
            return build(settings)
        if arguments.command == "status":
            return status(settings)
        return rollback(settings, arguments.version)
    except (EmbeddingError, VectorIndexError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
