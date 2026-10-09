import logging

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from acmecorp import World
from app.core.config import Settings
from app.core.exceptions import InvalidInputError, PermissionDeniedError
from app.ingestion import extractors, pipeline
from app.ingestion.extractors import base_name, extension_of
from app.ingestion.pipeline import Upload, ingest
from app.models import Document

RUNBOOK_TEXT = "Rollbacks are triggered from the deploy dashboard.\nOn-call approves them."


def upload(**changes: object) -> Upload:
    fields: dict[str, object] = {
        "filename": "rollback-guide.txt",
        "data": RUNBOOK_TEXT.encode(),
        "title": "Rollback Guide",
        "department_code": "engineering",
        "classification": "departmental",
    }
    fields.update(changes)
    return Upload(**fields)  # type: ignore[arg-type]


def document_count(world: World) -> int:
    return world.db.scalar(select(func.count()).select_from(Document)) or 0


# The normal case


def test_admin_ingests_a_text_file(world: World, settings: Settings) -> None:
    document = ingest(world.db, world.users["admin"], upload(), settings)
    world.db.expire_all()

    stored = world.db.get(Document, document.id)

    assert stored is not None
    assert stored.title == "Rollback Guide"
    assert stored.department.code == "engineering"
    assert stored.classification == "departmental"
    assert stored.content == RUNBOOK_TEXT


def test_ingested_document_follows_the_access_rules(world: World, settings: Settings) -> None:
    world.documents["new"] = ingest(world.db, world.users["admin"], upload(), settings)

    assert world.can_read("alice", "new")
    assert world.can_read("grace", "new")
    assert not world.can_read("carol", "new")
    assert not world.can_read("admin", "new")


def test_title_department_and_classification_are_tidied(world: World, settings: Settings) -> None:
    document = ingest(
        world.db,
        world.users["admin"],
        upload(
            title="  Rollback Guide  ", department_code=" Engineering ", classification="GENERAL"
        ),
        settings,
    )

    assert document.title == "Rollback Guide"
    assert document.department.code == "engineering"
    assert document.classification == "general"


def test_byte_order_mark_is_dropped(world: World, settings: Settings) -> None:
    document = ingest(world.db, world.users["admin"], upload(data=b"\xef\xbb\xbfHello"), settings)

    assert document.content == "Hello"


def test_text_in_other_scripts_is_kept(world: World, settings: Settings) -> None:
    text = "नीति दस्तावेज़ — Zürich café 東京"

    document = ingest(world.db, world.users["admin"], upload(data=text.encode()), settings)

    assert document.content == text


# Who may ingest


@pytest.mark.parametrize("user", ["alice", "carol", "grace", "reviewer", "nobody"])
def test_only_a_knowledge_admin_can_ingest(world: World, settings: Settings, user: str) -> None:
    before = document_count(world)

    with pytest.raises(PermissionDeniedError):
        ingest(world.db, world.users[user], upload(), settings)

    assert document_count(world) == before


def test_permission_is_checked_before_the_file_is_looked_at(
    world: World, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_if_called(filename: str, data: bytes) -> str:
        raise AssertionError("the file must not be parsed for a user who may not ingest")

    monkeypatch.setattr(pipeline, "extract_text", fail_if_called)

    with pytest.raises(PermissionDeniedError):
        ingest(world.db, world.users["alice"], upload(data=b"\xff\xfe"), settings)


# Labels


@pytest.mark.parametrize("classification", ["", "  ", "public", "confidential", "secret"])
def test_missing_or_unknown_classification_is_rejected(
    world: World, settings: Settings, classification: str
) -> None:
    before = document_count(world)

    with pytest.raises(InvalidInputError, match="classification must be one of"):
        ingest(world.db, world.users["admin"], upload(classification=classification), settings)

    assert document_count(world) == before


@pytest.mark.parametrize("department", ["", "marketing", "engineering; DROP TABLE documents"])
def test_missing_or_unknown_department_is_rejected(
    world: World, settings: Settings, department: str
) -> None:
    before = document_count(world)

    with pytest.raises(InvalidInputError, match="unknown department"):
        ingest(world.db, world.users["admin"], upload(department_code=department), settings)

    assert document_count(world) == before


@pytest.mark.parametrize("title", ["", "   ", "x" * 256])
def test_bad_title_is_rejected(world: World, settings: Settings, title: str) -> None:
    with pytest.raises(InvalidInputError, match="title"):
        ingest(world.db, world.users["admin"], upload(title=title), settings)


# The file


@pytest.mark.parametrize(
    "filename",
    ["malware.exe", "notes", "archive.txt.exe", "report.html", ".txt.bak", "script.txt.js", ""],
)
def test_unsupported_file_type_is_rejected(world: World, settings: Settings, filename: str) -> None:
    with pytest.raises(InvalidInputError, match="unsupported file type"):
        ingest(world.db, world.users["admin"], upload(filename=filename), settings)


@pytest.mark.parametrize("filename", ["REPORT.TXT", "Report.Txt", "my notes.txt", "v1.2.final.txt"])
def test_extension_is_matched_without_case(world: World, settings: Settings, filename: str) -> None:
    assert ingest(world.db, world.users["admin"], upload(filename=filename), settings).id


def test_empty_file_is_rejected(world: World, settings: Settings) -> None:
    with pytest.raises(InvalidInputError, match="empty"):
        ingest(world.db, world.users["admin"], upload(data=b""), settings)


@pytest.mark.parametrize("data", [b" ", b"\n\n\t  \r\n", b"\xef\xbb\xbf  "])
def test_file_with_no_real_text_is_rejected(world: World, settings: Settings, data: bytes) -> None:
    before = document_count(world)

    with pytest.raises(InvalidInputError, match="no text"):
        ingest(world.db, world.users["admin"], upload(data=data), settings)

    assert document_count(world) == before


@pytest.mark.parametrize(
    "data",
    [b"\x80\x81\x82", b"MZ\x90\x00\x03", b"plain text \x00 with a zero byte", b"\xff\xfe\x00"],
    ids=["undefined bytes", "executable header", "zero byte", "broken utf-16"],
)
def test_file_that_is_not_text_is_rejected(world: World, settings: Settings, data: bytes) -> None:
    before = document_count(world)

    with pytest.raises(InvalidInputError, match="not a readable text file"):
        ingest(world.db, world.users["admin"], upload(data=data), settings)

    assert document_count(world) == before


def test_text_over_the_character_limit_is_rejected(
    world: World, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "MAX_EXTRACTED_CHARACTERS", 50)
    before = document_count(world)

    assert ingest(world.db, world.users["admin"], upload(data=b"x" * 50), settings).id
    with pytest.raises(InvalidInputError, match="more than 50 characters"):
        ingest(world.db, world.users["admin"], upload(data=b"x" * 51), settings)

    assert document_count(world) == before + 1


def test_file_over_the_size_limit_is_rejected_without_being_parsed(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    small_limit = Settings(_env_file=None, jwt_secret_key=SecretStr("x" * 64), max_upload_bytes=100)

    def fail_if_called(filename: str, data: bytes) -> str:
        raise AssertionError("an oversized file must not be parsed")

    monkeypatch.setattr(pipeline, "extract_text", fail_if_called)

    with pytest.raises(InvalidInputError, match="larger than the limit"):
        ingest(world.db, world.users["admin"], upload(data=b"x" * 101), small_limit)


def test_file_exactly_at_the_size_limit_is_accepted(world: World) -> None:
    limit = Settings(_env_file=None, jwt_secret_key=SecretStr("x" * 64), max_upload_bytes=100)

    assert ingest(world.db, world.users["admin"], upload(data=b"x" * 100), limit).id


# File names


@pytest.mark.parametrize(
    ("filename", "name"),
    [
        ("report.txt", "report.txt"),
        ("../../etc/passwd.txt", "passwd.txt"),
        ("C:\\Users\\bob\\Desktop\\report.txt", "report.txt"),
        ("..\\..\\windows\\system32\\report.txt", "report.txt"),
        ("/var/data/report.txt", "report.txt"),
        ("folder/sub\\mixed/report.txt", "report.txt"),
    ],
)
def test_directory_parts_of_a_file_name_are_dropped(filename: str, name: str) -> None:
    assert base_name(filename) == name
    assert extension_of(filename) == ".txt"


# Failure behaviour


def test_parser_errors_do_not_escape_as_anything_but_invalid_input(
    world: World, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(data: bytes) -> str:
        raise InvalidInputError("the file could not be read")

    monkeypatch.setattr(extractors, "EXTRACTORS", {".txt": broken})
    before = document_count(world)

    with pytest.raises(InvalidInputError, match="could not be read"):
        ingest(world.db, world.users["admin"], upload(), settings)

    assert document_count(world) == before


def test_ingestion_is_logged_without_the_content_or_title(
    world: World, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="app.ingestion.pipeline")

    document = ingest(world.db, world.users["admin"], upload(), settings)

    record = caplog.records[-1]
    assert record.getMessage() == "document ingested"
    assert record.__dict__["document_id"] == document.id
    assert record.__dict__["user_id"] == world.users["admin"].id
    assert record.__dict__["classification"] == "departmental"
    assert record.__dict__["characters"] == len(RUNBOOK_TEXT)
    logged = str(vars(record))
    assert "Rollbacks" not in logged
    assert "Rollback Guide" not in logged


def test_extractor_lookup_rejects_an_unsupported_type_by_itself() -> None:
    # The pipeline checks the type first, but the lookup must not rely on that.
    with pytest.raises(InvalidInputError, match="unsupported file type"):
        extractors.extract_text("payload.exe", b"MZ")
