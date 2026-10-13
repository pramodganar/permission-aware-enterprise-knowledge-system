"""Index versions: publishing, rollback, pruning and noticing an out-of-date index."""

import logging
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

import app.retrieval.index_versions as versions
import scripts.index as index_command
from acmecorp import World
from app.authorization.document_access import change_labels
from app.core.config import Settings
from app.core.database import Base, create_db_engine, create_session_factory
from app.ingestion.pipeline import (
    Upload,
    delete_document,
    ingest,
    rechunk_documents,
    replace_document,
)
from app.models import Chunk, Document
from app.retrieval.embeddings import EmbeddingError
from app.retrieval.index import build_index
from app.retrieval.index_store import INDEX_FILE, MANIFEST_FILE, IndexLoadError, IndexManifest
from app.retrieval.index_versions import (
    POINTER_FILE,
    VERSIONS_FOLDER,
    check_freshness,
    content_fingerprint,
    current_version,
    list_versions,
    load_current_index,
    prune_versions,
    publish_index,
    rollback_index,
)
from fakes import FakeEmbedder
from logcheck import everything_logged
from scripts.seed import seed

GUIDE = "\n".join(
    f"Step {number}. The on-call engineer checks item {number} before the release goes out."
    for number in range(1, 31)
)
POLICY = "\n".join(
    f"Rule {number}. Annual leave request {number} is approved by the line manager."
    for number in range(1, 31)
)


@pytest.fixture
def small(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "chunk_max_characters": 300,
            "chunk_overlap_characters": 80,
            "embedding_model": "fake-embedder",
        }
    )


def store(world: World, settings: Settings, title: str, text: str, department: str) -> Document:
    upload = Upload(
        filename=f"{title}.txt",
        data=text.encode(),
        title=title,
        department_code=department,
        classification="departmental",
    )
    return ingest(world.db, world.users["admin"], upload, settings)


@pytest.fixture
def guide(world: World, small: Settings) -> Document:
    return store(world, small, "Release Guide", GUIDE, "engineering")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "index"


def publish(world: World, settings: Settings, root: Path) -> int:
    return publish_index(world.db, FakeEmbedder(), settings, root).version


def is_up_to_date(world: World, settings: Settings, root: Path) -> bool:
    index, manifest = load_current_index(settings, root)
    return check_freshness(world.db, index, manifest).up_to_date


# --- publishing ---------------------------------------------------------------------


def test_the_first_build_is_version_one_and_current(
    world: World, small: Settings, guide: Document, root: Path
) -> None:
    manifest = publish_index(world.db, FakeEmbedder(), small, root)

    assert manifest.version == 1
    assert current_version(root) == 1
    assert (root / POINTER_FILE).read_text(encoding="ascii") == "000001\n"
    assert sorted(path.name for path in (root / VERSIONS_FOLDER / "000001").iterdir()) == [
        MANIFEST_FILE,
        INDEX_FILE,
    ]


def test_each_build_gets_the_next_number(
    world: World, small: Settings, guide: Document, root: Path
) -> None:
    assert [publish(world, small, root) for _ in range(3)] == [1, 2, 3]
    assert current_version(root) == 3


def test_the_current_version_loads_with_the_index_of_the_database(
    world: World, small: Settings, guide: Document, root: Path
) -> None:
    publish(world, small, root)

    index, manifest = load_current_index(small, root)

    assert index.chunk_ids == {chunk.id for chunk in guide.chunks}
    assert manifest.content_sha256 == content_fingerprint(guide.chunks)


def test_a_build_never_overwrites_an_existing_version(
    world: World, small: Settings, guide: Document, root: Path
) -> None:
    publish(world, small, root)
    first = (root / VERSIONS_FOLDER / "000001" / INDEX_FILE).read_bytes()
    store(world, small, "Leave Rules", POLICY, "hr")

    publish(world, small, root)

    assert (root / VERSIONS_FOLDER / "000001" / INDEX_FILE).read_bytes() == first


def test_a_failed_build_leaves_the_current_version_in_use(
    world: World, small: Settings, guide: Document, root: Path
) -> None:
    publish(world, small, root)
    store(world, small, "Leave Rules", POLICY, "hr")

    # Every chunk is too long for this model, so the build stops at embedding.
    with pytest.raises(EmbeddingError):
        publish_index(world.db, FakeEmbedder(max_tokens=3), small, root)

    assert current_version(root) == 1
    assert load_current_index(small, root)[0].chunk_ids == {chunk.id for chunk in guide.chunks}


def test_a_build_that_does_not_load_back_is_never_made_current(
    world: World, small: Settings, guide: Document, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish(world, small, root)

    def broken_load(settings: Settings, directory: Path) -> None:
        raise IndexLoadError("the index file does not match its manifest")

    monkeypatch.setattr(versions, "load_index", broken_load)
    with pytest.raises(IndexLoadError):
        publish_index(world.db, FakeEmbedder(), small, root)

    assert current_version(root) == 1


def test_publishing_is_logged_with_numbers_only(
    world: World, small: Settings, guide: Document, root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        publish(world, small, root)

    record = next(r for r in caplog.records if r.message == "index version published")
    assert (record.version, record.chunks) == (1, len(guide.chunks))  # type: ignore[attr-defined]
    assert "on-call" not in everything_logged(caplog)


# --- the pointer ---------------------------------------------------------------------


def test_no_index_yet(small: Settings, root: Path) -> None:
    with pytest.raises(IndexLoadError, match="no index has been published"):
        load_current_index(small, root)


@pytest.mark.parametrize(
    "content", [b"", b"1", b"00001", b"0000001", b"../../etc", b"000001/../x", b"\xff\xfe"]
)
def test_a_pointer_that_is_not_a_version_number_is_refused(
    world: World, small: Settings, guide: Document, root: Path, content: bytes
) -> None:
    publish(world, small, root)
    (root / POINTER_FILE).write_bytes(content)

    with pytest.raises(IndexLoadError, match="pointer can't be read"):
        load_current_index(small, root)


def test_a_pointer_to_a_version_that_is_gone_is_refused(
    world: World, small: Settings, guide: Document, root: Path
) -> None:
    publish(world, small, root)
    (root / POINTER_FILE).write_text("000009\n", encoding="ascii")

    with pytest.raises(IndexLoadError, match="no index has been built"):
        load_current_index(small, root)


# --- rollback ------------------------------------------------------------------------


def test_rollback_goes_to_the_previous_version(
    world: World, small: Settings, guide: Document, root: Path
) -> None:
    publish(world, small, root)
    store(world, small, "Leave Rules", POLICY, "hr")
    publish(world, small, root)

    manifest = rollback_index(small, root)

    assert manifest.version == 1
    assert current_version(root) == 1
    assert load_current_index(small, root)[0].chunk_ids == {chunk.id for chunk in guide.chunks}


def test_rollback_to_a_named_version(
    world: World, small: Settings, guide: Document, root: Path
) -> None:
    for _ in range(3):
        publish(world, small, root)

    assert rollback_index(small, root, 1).version == 1
    assert current_version(root) == 1


def test_rollback_skips_a_version_that_cannot_be_used(
    world: World, small: Settings, guide: Document, root: Path
) -> None:
    for _ in range(3):
        publish(world, small, root)
    (root / VERSIONS_FOLDER / "000002" / INDEX_FILE).write_bytes(b"damaged")

    assert rollback_index(small, root).version == 1


def test_rollback_works_when_the_current_version_is_damaged(
    world: World, small: Settings, guide: Document, root: Path
) -> None:
    publish(world, small, root)
    publish(world, small, root)
    (root / VERSIONS_FOLDER / "000002" / MANIFEST_FILE).write_text("{}", encoding="utf-8")

    assert rollback_index(small, root).version == 1


def test_rollback_will_not_switch_to_a_version_built_with_another_model(
    world: World, small: Settings, guide: Document, root: Path
) -> None:
    publish(world, small, root)
    publish(world, small, root)
    other_model = small.model_copy(update={"embedding_model": "another-model"})

    with pytest.raises(IndexLoadError, match="no earlier version"):
        rollback_index(other_model, root)
    with pytest.raises(IndexLoadError, match="built with fake-embedder"):
        rollback_index(other_model, root, 1)
    assert current_version(root) == 2


@pytest.mark.parametrize(("target", "problem"), [(2, "already current"), (7, "no version 7")])
def test_rollback_to_a_version_that_makes_no_sense_is_refused(
    world: World, small: Settings, guide: Document, root: Path, target: int, problem: str
) -> None:
    publish(world, small, root)
    publish(world, small, root)

    with pytest.raises(IndexLoadError, match=problem):
        rollback_index(small, root, target)
    assert current_version(root) == 2


def test_rollback_with_only_one_version_is_refused(
    world: World, small: Settings, guide: Document, root: Path
) -> None:
    publish(world, small, root)

    with pytest.raises(IndexLoadError, match="no earlier version"):
        rollback_index(small, root)


def test_after_a_rollback_the_next_build_still_gets_a_new_number(
    world: World, small: Settings, guide: Document, root: Path
) -> None:
    publish(world, small, root)
    publish(world, small, root)
    rollback_index(small, root)

    assert publish(world, small, root) == 3


# --- keeping old versions -------------------------------------------------------------


def test_only_the_newest_versions_are_kept(
    world: World, small: Settings, guide: Document, root: Path
) -> None:
    for _ in range(5):
        publish(world, small, root)

    assert [version.number for version in list_versions(small, root)] == [5, 4, 3]


def test_the_current_version_is_never_pruned(
    world: World, small: Settings, guide: Document, root: Path
) -> None:
    for _ in range(4):
        publish(world, small, root)
    rollback_index(small, root, 2)

    removed = prune_versions(root, keep=1)

    # Version 1 went when version 4 was built (three are kept). Of 4, 3 and 2,
    # keeping one would leave only 4, but 2 is current, so 3 is the only one removed.
    assert removed == [3]
    assert [version.number for version in list_versions(small, root)] == [4, 2]
    assert current_version(root) == 2
    assert load_current_index(small, root)[0].size == len(guide.chunks)


def test_prune_needs_to_keep_something(
    world: World, small: Settings, guide: Document, root: Path
) -> None:
    publish(world, small, root)

    with pytest.raises(ValueError, match="at least one"):
        prune_versions(root, keep=0)


def test_the_version_list_marks_the_current_one_and_the_unusable_ones(
    world: World, small: Settings, guide: Document, root: Path
) -> None:
    for _ in range(3):
        publish(world, small, root)
    rollback_index(small, root, 2)
    (root / VERSIONS_FOLDER / "000003" / INDEX_FILE).unlink()

    versions = list_versions(small, root)

    assert [(v.number, v.current, v.manifest is not None) for v in versions] == [
        (3, False, False),
        (2, True, True),
        (1, False, True),
    ]
    assert versions[0].problem == "the index file is missing"


def test_other_folders_and_files_are_ignored(
    world: World, small: Settings, guide: Document, root: Path
) -> None:
    publish(world, small, root)
    (root / VERSIONS_FOLDER / "notes").mkdir()
    (root / VERSIONS_FOLDER / "000099.txt").write_text("x", encoding="utf-8")

    assert [version.number for version in list_versions(small, root)] == [1]
    assert publish(world, small, root) == 2


# --- up to date or not ----------------------------------------------------------------


def test_a_fresh_index_is_up_to_date(
    world: World, small: Settings, guide: Document, root: Path
) -> None:
    publish(world, small, root)
    index, manifest = load_current_index(small, root)

    freshness = check_freshness(world.db, index, manifest)

    assert freshness.up_to_date
    assert (freshness.missing, freshness.retired) == (0, 0)
    assert freshness.indexed_chunks == freshness.active_chunks == len(guide.chunks)


def test_a_new_document_makes_the_index_out_of_date(
    world: World, small: Settings, guide: Document, root: Path
) -> None:
    publish(world, small, root)
    policy = store(world, small, "Leave Rules", POLICY, "hr")
    index, manifest = load_current_index(small, root)

    freshness = check_freshness(world.db, index, manifest)

    assert not freshness.up_to_date
    assert (freshness.missing, freshness.retired) == (len(policy.chunks), 0)


def test_a_deleted_document_makes_the_index_out_of_date(
    world: World, small: Settings, guide: Document, root: Path
) -> None:
    publish(world, small, root)
    delete_document(world.db, world.users["admin"], guide.id)
    index, manifest = load_current_index(small, root)

    freshness = check_freshness(world.db, index, manifest)

    assert not freshness.up_to_date
    assert (freshness.missing, freshness.retired) == (0, len(guide.chunks))


def test_a_replaced_document_makes_the_index_out_of_date(
    world: World, small: Settings, guide: Document, root: Path
) -> None:
    publish(world, small, root)
    replace_document(
        world.db,
        world.users["admin"],
        guide.id,
        filename="guide.txt",
        data=(GUIDE + "\nStep 31. Tell the release channel.").encode(),
        settings=small,
    )

    assert not is_up_to_date(world, small, root)


def test_changed_chunk_text_under_the_same_id_is_noticed(
    world: World, small: Settings, guide: Document, root: Path
) -> None:
    # Same IDs, different text: comparing ID lists alone would miss this.
    publish(world, small, root)
    world.db.execute(text("UPDATE chunks SET text = 'something else' WHERE position = 0"))
    world.db.commit()
    world.db.expire_all()

    assert not is_up_to_date(world, small, root)


def test_a_changed_title_is_noticed(
    world: World, small: Settings, guide: Document, root: Path
) -> None:
    # The title is part of what was embedded.
    publish(world, small, root)
    world.db.execute(
        text("UPDATE documents SET title = 'Renamed' WHERE id = :id"), {"id": guide.id}
    )
    world.db.commit()
    world.db.expire_all()

    assert not is_up_to_date(world, small, root)


def test_a_label_change_does_not_make_the_index_out_of_date(
    world: World, small: Settings, guide: Document, root: Path
) -> None:
    # Access lives in the database. The vectors are still right.
    publish(world, small, root)
    change_labels(world.db, world.users["admin"], guide.id, classification="general")

    assert is_up_to_date(world, small, root)


def test_an_index_without_a_fingerprint_counts_as_out_of_date(
    world: World, small: Settings, guide: Document
) -> None:
    index = build_index(world.db, FakeEmbedder())
    manifest = IndexManifest(
        format_version=2,
        version=1,
        embedding_model="fake-embedder",
        dimension=64,
        chunk_max_characters=300,
        chunk_overlap_characters=80,
        chunk_count=index.size,
        index_sha256="0" * 64,
        built_at="2026-10-01T00:00:00Z",
    )

    assert not check_freshness(world.db, index, manifest).up_to_date


def test_rebuilding_makes_it_up_to_date_again(
    world: World, small: Settings, guide: Document, root: Path
) -> None:
    publish(world, small, root)
    store(world, small, "Leave Rules", POLICY, "hr")
    assert not is_up_to_date(world, small, root)

    publish(world, small, root)

    assert is_up_to_date(world, small, root)


def test_a_deleted_chunk_id_is_not_given_to_a_new_chunk(
    world: World, small: Settings, settings: Settings, guide: Document
) -> None:
    # If SQLite reused the ID of a deleted chunk, an old vector would sit
    # under a new chunk's ID.
    highest = max(chunk.id for chunk in guide.chunks)
    world.db.execute(text("DELETE FROM chunks WHERE id = :id"), {"id": highest})
    world.db.commit()
    world.db.expire_all()

    rechunk_documents(world.db, world.users["admin"], small)

    new_ids = set(world.db.scalars(select(Chunk.id)))
    assert highest not in new_ids
    assert min(new_ids) > highest


# --- the command ------------------------------------------------------------------------


@pytest.fixture
def command_line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings: Settings) -> Settings:
    """A seeded database file and an index folder, both in a temporary folder."""
    local = settings.model_copy(
        update={
            "database_url": f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
            "index_directory": tmp_path / "index",
            "embedding_model": "fake-embedder",
        }
    )
    engine = create_db_engine(local.database_url)
    Base.metadata.create_all(engine)
    with create_session_factory(engine)() as db:
        seed(db, local, "index-command-password")
    engine.dispose()

    monkeypatch.setattr(index_command, "get_settings", lambda: local)
    monkeypatch.setattr(index_command, "configure_logging", lambda level: None)
    monkeypatch.setattr(index_command, "load_embedder", lambda settings: FakeEmbedder())
    return local


def database(settings: Settings) -> Session:
    return create_session_factory(create_db_engine(settings.database_url))()


def test_build_publishes_a_version(
    command_line: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert index_command.main(["build"]) == 0

    output = capsys.readouterr().out
    assert "version 1 is now current" in output
    assert "chunks indexed: 57" in output


def test_status_reports_an_up_to_date_index(
    command_line: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    index_command.main(["build"])
    capsys.readouterr()

    assert index_command.main(["status"]) == 0
    output = capsys.readouterr().out
    assert "* 000001  57 chunks" in output
    assert "version 1 is up to date" in output


def test_status_reports_an_out_of_date_index_with_its_own_exit_code(
    command_line: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    index_command.main(["build"])
    with database(command_line) as db:
        document = db.scalars(select(Document).order_by(Document.id)).first()
        assert document is not None
        delete_document(db, document.uploaded_by, document.id)
    capsys.readouterr()

    assert index_command.main(["status"]) == index_command.OUT_OF_DATE
    assert "is out of date: 0 chunks not indexed, 3 indexed chunks no longer active" in (
        capsys.readouterr().out
    )


def test_rollback_from_the_command_line(
    command_line: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    index_command.main(["build"])
    index_command.main(["build"])
    capsys.readouterr()

    assert index_command.main(["rollback"]) == 0
    assert "version 1 is now current" in capsys.readouterr().out
    assert current_version(command_line.index_directory) == 1


def test_errors_are_reported_without_a_traceback(
    command_line: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert index_command.main(["status"]) == 1
    assert index_command.main(["rollback", "4"]) == 1

    errors = capsys.readouterr().err
    assert "error: no index has been published yet" in errors
    assert "Traceback" not in errors
