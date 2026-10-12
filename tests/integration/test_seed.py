"""The seed script and the AcmeCorp data it loads."""

import json
import shutil
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.service import authenticate
from app.authorization.service import can_read, readable_documents
from app.core.config import Settings
from app.core.database import create_db_engine, create_session_factory, utc_now
from app.core.exceptions import AuthenticationError
from app.models import Classification, Department, Document, DocumentGrant, Role, User
from scripts.seed import COMPANY_DIR, SeedError, load_manifest, main, seed

PASSWORD = "seed-password-for-tests"

# email prefix: titles of the departmental and restricted documents they may read.
# Everyone who can read at all also reads the three general documents.
GENERAL = {"Leave Policy", "Code of Conduct", "Travel and Expense Policy"}
ENGINEERING = {"Deployment Runbook", "Payments Outage Postmortem"}
FINANCE = {"Vendor Payment Approval Limits", "Headcount Budget 2027"}
HR = {"Performance Improvement Plan Process", "Salary Bands 2026"}
SALES = {"Discount Approval Policy"}
LEGAL = {"Contract Data Retention Obligations", "Supplier Dispute Status"}
OPERATIONS = {"Warehouse Incident SOP"}
EXECUTIVE = {"Executive Compensation Strategy 2027", "Three-Year Strategy 2027 to 2029"}
CASE_FILE = "HR Case File 2026-014"
ACQUISITION = "Project Falcon Acquisition Paper"

EXPECTED_READING = {
    "alice": GENERAL | ENGINEERING,
    "bob": GENERAL | FINANCE,
    "carol": GENERAL | HR | {CASE_FILE},
    "david": GENERAL | SALES,
    "emma": GENERAL | LEGAL | {ACQUISITION},
    "frank": GENERAL | OPERATIONS,
    "grace": (
        GENERAL
        | ENGINEERING
        | FINANCE
        | HR
        | SALES
        | LEGAL
        | OPERATIONS
        | EXECUTIVE
        | {ACQUISITION}
    ),
    "henry": set(),
    "irene": set(),
}


def find_user(db: Session, name: str) -> User:
    return db.scalars(select(User).where(User.email == f"{name}@acmecorp.example")).one()


def readable_titles(db: Session, name: str) -> set[str]:
    return {document.title for document in db.scalars(readable_documents(find_user(db, name)))}


def count(db: Session, model: Any) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


@pytest.fixture
def seeded(db: Session, settings: Settings) -> Session:
    seed(db, settings, PASSWORD)
    return db


@pytest.fixture
def company_copy(tmp_path: Path) -> Path:
    """A copy of the company data folder that a test can damage."""
    target = tmp_path / "acmecorp"
    shutil.copytree(COMPANY_DIR, target)
    return target


def edit_manifest(company_dir: Path, change: Any) -> None:
    path = company_dir / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    change(manifest)
    path.write_text(json.dumps(manifest), encoding="utf-8")


def test_loads_the_whole_company(db: Session, settings: Settings) -> None:
    result = seed(db, settings, PASSWORD)

    assert (result.departments_created, result.users_created) == (7, 9)
    assert (result.documents_ingested, result.documents_skipped) == (17, 0)
    assert count(db, Department) == 7
    assert count(db, Role) == 4
    assert count(db, User) == 9
    assert count(db, Document) == 17
    assert count(db, DocumentGrant) == 3


def test_running_it_again_changes_nothing(seeded: Session, settings: Settings) -> None:
    result = seed(seeded, settings, PASSWORD)

    assert (result.departments_created, result.users_created) == (0, 0)
    assert (result.documents_ingested, result.documents_skipped) == (0, 17)
    assert count(seeded, User) == 9
    assert count(seeded, Document) == 17
    assert count(seeded, DocumentGrant) == 3


def test_running_it_again_does_not_reset_a_password(seeded: Session, settings: Settings) -> None:
    seed(seeded, settings, "a-different-password")

    assert authenticate(seeded, "alice@acmecorp.example", PASSWORD).full_name == "Alice Chen"
    with pytest.raises(AuthenticationError):
        authenticate(seeded, "alice@acmecorp.example", "a-different-password")


@pytest.mark.parametrize("name", sorted(EXPECTED_READING))
def test_every_seeded_user_can_log_in(seeded: Session, name: str) -> None:
    assert authenticate(seeded, f"{name}@acmecorp.example", PASSWORD).is_active


def test_the_password_is_stored_hashed(seeded: Session) -> None:
    for user in seeded.scalars(select(User)):
        assert user.hashed_password.startswith("$argon2id$")
        assert PASSWORD not in user.hashed_password


@pytest.mark.parametrize("name", sorted(EXPECTED_READING))
def test_each_person_reads_exactly_what_they_should(seeded: Session, name: str) -> None:
    assert readable_titles(seeded, name) == EXPECTED_READING[name]


@pytest.mark.parametrize("name", sorted(EXPECTED_READING))
def test_both_forms_of_the_access_rule_agree(seeded: Session, name: str) -> None:
    user = find_user(seeded, name)
    one_by_one = {
        document.title for document in seeded.scalars(select(Document)) if can_read(user, document)
    }
    assert one_by_one == readable_titles(seeded, name)


def test_the_expected_reading_covers_every_document(seeded: Session) -> None:
    # Guards the table above: a document added to the corpus must be added there too.
    titles = set(seeded.scalars(select(Document.title)))
    assert titles == EXPECTED_READING["grace"] | {CASE_FILE}


def test_nobody_but_the_case_handler_reads_the_case_file(seeded: Session) -> None:
    readers = {name for name in EXPECTED_READING if CASE_FILE in readable_titles(seeded, name)}
    assert readers == {"carol"}


def test_the_temporary_grant_ends_on_its_own(seeded: Session) -> None:
    emma = find_user(seeded, "emma")
    paper = seeded.scalars(select(Document).where(Document.title == ACQUISITION)).one()

    assert can_read(emma, paper, utc_now() + timedelta(days=59))
    assert not can_read(emma, paper, utc_now() + timedelta(days=61))


def test_documents_went_through_the_pipeline(seeded: Session) -> None:
    henry = find_user(seeded, "henry")
    for document in seeded.scalars(select(Document)):
        assert document.uploaded_by_id == henry.id
        assert len(document.sha256) == 64
        assert len(document.content) > 1000


def test_the_documents_come_in_all_three_formats(seeded: Session) -> None:
    formats = Counter(seeded.scalars(select(Document.file_type)))
    assert formats == {".pdf": 6, ".docx": 8, ".txt": 3}


# One fact per document that a persona question depends on. If extraction
# loses it, no later stage can find it.
FACTS = {
    "Leave Policy": "25 days of paid annual leave",
    "Code of Conduct": "gifts from customers or suppliers worth up to 50 EUR",
    "Travel and Expense Policy": "160 EUR per night",
    "Deployment Runbook": "deployctl rollback <service> --to previous",
    "Payments Outage Postmortem": "connection pool exhaustion",
    "Vendor Payment Approval Limits": "Up to 5,000 EUR: Finance Analyst",
    "Headcount Budget 2027": "Engineering | 28",
    "Performance Improvement Plan Process": "The standard length is 60 days",
    "Salary Bands 2026": "E3 Senior Engineer | 68,000 to 92,000, midpoint 80,000",
    "Discount Approval Policy": "Up to 10 percent: Account Executive, no approval needed",
    "Contract Data Retention Obligations": "Customer contracts: 7 years after the contract ends",
    "Supplier Dispute Status": "A second session is set for 14 October 2026",
    "Warehouse Incident SOP": "files an incident report in the operations system within 24 hours",
    "Executive Compensation Strategy 2027": "Operating margin (EBITDA) against plan: weight 40",
    "Three-Year Strategy 2027 to 2029": "Open sales and operations in Poland and Spain",
    CASE_FILE: "Employee number E-2291",
    ACQUISITION: "58 to 64 million EUR",
}


@pytest.mark.parametrize("title", sorted(FACTS))
def test_the_key_fact_survives_extraction(seeded: Session, title: str) -> None:
    content = seeded.scalars(select(Document.content).where(Document.title == title)).one()
    # A PDF breaks lines where the page does, so compare with whitespace collapsed.
    assert FACTS[title] in " ".join(content.split())


def test_page_headers_and_footers_are_not_in_the_stored_text(seeded: Session) -> None:
    for content in seeded.scalars(select(Document.content)):
        assert "Uncontrolled when printed" not in content
        assert "ACMECORP" not in content
        assert "Page 1 of" not in content
        assert "Confidential - " not in content


def test_there_is_a_fact_for_every_document(seeded: Session) -> None:
    assert set(FACTS) == set(seeded.scalars(select(Document.title)))


def test_restricted_documents_are_the_two_expected(seeded: Session) -> None:
    restricted = seeded.scalars(
        select(Document.title).where(Document.classification == Classification.RESTRICTED)
    )
    assert set(restricted) == {CASE_FILE, ACQUISITION}


def test_every_file_in_the_folder_is_in_the_manifest() -> None:
    listed = {entry.file for entry in load_manifest(COMPANY_DIR).documents}
    on_disk = {
        path.relative_to(COMPANY_DIR).as_posix()
        for path in (COMPANY_DIR / "documents").rglob("*")
        if path.is_file()
    }
    assert listed == on_disk


def test_text_documents_are_plain_ascii() -> None:
    # Keeps the corpus free of the invisible characters the cleaning step removes.
    for path in (COMPANY_DIR / "documents").rglob("*.txt"):
        assert path.read_bytes().isascii(), path.name


def test_refuses_a_production_environment(db: Session) -> None:
    production = Settings(
        _env_file=None, environment="production", jwt_secret_key=SecretStr("x" * 64)
    )

    with pytest.raises(SeedError, match="production"):
        seed(db, production, PASSWORD)
    assert count(db, User) == 0


def test_refuses_a_short_password(db: Session, settings: Settings) -> None:
    with pytest.raises(SeedError, match="at least 12"):
        seed(db, settings, "short")
    assert count(db, User) == 0


def test_a_misspelt_key_in_the_manifest_is_an_error(
    db: Session, settings: Settings, company_copy: Path
) -> None:
    def misspell(manifest: dict[str, Any]) -> None:
        document = manifest["documents"][0]
        document["clasification"] = document.pop("classification")

    edit_manifest(company_copy, misspell)

    with pytest.raises(ValidationError):
        seed(db, settings, PASSWORD, company_copy)
    assert count(db, Department) == 0


def test_an_unknown_role_in_the_manifest_is_an_error(
    db: Session, settings: Settings, company_copy: Path
) -> None:
    edit_manifest(company_copy, lambda manifest: manifest["users"][0].update(roles=["superuser"]))

    with pytest.raises(ValidationError):
        seed(db, settings, PASSWORD, company_copy)


def test_a_user_in_an_unknown_department_is_an_error(
    db: Session, settings: Settings, company_copy: Path
) -> None:
    edit_manifest(
        company_copy, lambda manifest: manifest["users"][0].update(department="marketing")
    )

    with pytest.raises(SeedError, match="unknown department"):
        seed(db, settings, PASSWORD, company_copy)


def test_an_unknown_uploader_is_an_error(
    db: Session, settings: Settings, company_copy: Path
) -> None:
    edit_manifest(company_copy, lambda manifest: manifest.update(uploaded_by="x@acmecorp.example"))

    with pytest.raises(SeedError, match="uploaded_by"):
        seed(db, settings, PASSWORD, company_copy)
    assert count(db, Document) == 0


def test_an_uploader_who_is_not_an_admin_stores_nothing(
    db: Session, settings: Settings, company_copy: Path
) -> None:
    edit_manifest(
        company_copy, lambda manifest: manifest.update(uploaded_by="grace@acmecorp.example")
    )

    with pytest.raises(SeedError, match="leave-policy"):
        seed(db, settings, PASSWORD, company_copy)
    assert count(db, Document) == 0


def test_a_restricted_document_without_readers_is_an_error(
    db: Session, settings: Settings, company_copy: Path
) -> None:
    def remove_grants(manifest: dict[str, Any]) -> None:
        for document in manifest["documents"]:
            document.pop("grants", None)

    edit_manifest(company_copy, remove_grants)

    with pytest.raises(SeedError, match="case-file-2026-014"):
        seed(db, settings, PASSWORD, company_copy)
    assert count(db, DocumentGrant) == 0


def test_a_missing_file_is_an_error(db: Session, settings: Settings, company_copy: Path) -> None:
    (company_copy / "documents" / "hr" / "leave-policy.pdf").unlink()

    with pytest.raises(SeedError, match="does not exist"):
        seed(db, settings, PASSWORD, company_copy)


def test_a_file_outside_the_data_folder_is_refused(
    db: Session, settings: Settings, company_copy: Path
) -> None:
    (company_copy.parent / "outside.txt").write_text(
        "not part of the company data", encoding="utf-8"
    )
    edit_manifest(
        company_copy, lambda manifest: manifest["documents"][0].update(file="../outside.txt")
    )

    with pytest.raises(SeedError, match="outside the company data folder"):
        seed(db, settings, PASSWORD, company_copy)
    assert count(db, Document) == 0


@pytest.fixture
def command_line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings: Settings) -> Settings:
    """Point the command at a database file in a temporary folder."""
    database = tmp_path / "seed.db"
    local = settings.model_copy(update={"database_url": f"sqlite:///{database.as_posix()}"})
    monkeypatch.setattr("scripts.seed.get_settings", lambda: local)
    # The real one replaces the root log handlers, which would outlive the test.
    monkeypatch.setattr("scripts.seed.configure_logging", lambda level: None)
    monkeypatch.delenv("SEED_PASSWORD", raising=False)
    return local


def test_command_generates_a_password_and_shows_it_once(
    command_line: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([]) == 0
    first = capsys.readouterr().out
    assert main([]) == 0
    second = capsys.readouterr().out

    assert "documents ingested:  17" in first
    assert "password for the new users: " in first
    assert "documents skipped:   17" in second
    assert "password" not in second


def test_command_does_not_print_a_password_it_was_given(
    command_line: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SEED_PASSWORD", PASSWORD)

    assert main([]) == 0
    assert PASSWORD not in capsys.readouterr().out


def test_command_line_password_wins_over_the_environment(
    command_line: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SEED_PASSWORD", "from-the-environment")

    assert main(["--password", PASSWORD]) == 0

    engine = create_db_engine(command_line.database_url)
    with create_session_factory(engine)() as db:
        assert authenticate(db, "grace@acmecorp.example", PASSWORD).full_name == "Grace Patel"
    engine.dispose()


def test_command_reports_an_error_without_a_traceback(
    command_line: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--password", "short"]) == 1

    captured = capsys.readouterr()
    assert captured.err.startswith("error: the password must be")
    assert "Traceback" not in captured.err
