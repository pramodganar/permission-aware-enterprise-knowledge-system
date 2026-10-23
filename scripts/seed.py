"""Load the AcmeCorp company data into the database.

    python -m scripts.seed

Creates the tables, the departments, the roles and the users, then
ingests the company documents. Documents go through the same ingestion pipeline
as any other upload, as the knowledge admin named in the manifest, so the
labels and grants are checked by the real code and not written straight to
the tables.

It is safe to run more than once: anything that already exists is left alone.

All seeded users share one password. Give it with --password or the
SEED_PASSWORD environment variable; otherwise one is generated and printed
once. The script refuses to run when the environment is production.
"""

import argparse
import hashlib
import os
import secrets
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.seals import seal_key
from app.auth.passwords import hash_password
from app.authorization.document_access import GrantRequest
from app.core.config import Settings, get_settings
from app.core.database import Base, create_db_engine, create_session_factory, utc_now
from app.core.exceptions import AppError
from app.core.logging_setup import configure_logging
from app.ingestion.pipeline import Upload, ingest, rechunk_documents
from app.models import Department, Document, DocumentStatus, Role, RoleCode, User

COMPANY_DIR = Path(__file__).resolve().parent.parent / "data" / "acmecorp"
MANIFEST_NAME = "manifest.json"
MIN_PASSWORD_LENGTH = 12


class SeedError(Exception):
    """The company data can't be loaded as it is."""


class _Entry(BaseModel):
    # A misspelt key in the manifest is an error, not something to skip.
    model_config = ConfigDict(extra="forbid")


class DepartmentEntry(_Entry):
    code: str
    name: str


class UserEntry(_Entry):
    email: str
    full_name: str
    department: str
    roles: list[RoleCode]


class GrantEntry(_Entry):
    email: str
    # Relative, so the data doesn't go stale on a fixed date.
    expires_in_days: int | None = Field(default=None, gt=0)


class DocumentEntry(_Entry):
    file: str
    title: str
    department: str
    classification: str
    grants: list[GrantEntry] = []


class Manifest(_Entry):
    departments: list[DepartmentEntry]
    users: list[UserEntry]
    uploaded_by: str
    documents: list[DocumentEntry]


@dataclass
class SeedResult:
    departments_created: int = 0
    users_created: int = 0
    documents_ingested: int = 0
    documents_skipped: int = 0
    documents_rechunked: int = 0


def load_manifest(company_dir: Path) -> Manifest:
    return Manifest.model_validate_json((company_dir / MANIFEST_NAME).read_text(encoding="utf-8"))


def seed(
    db: Session, settings: Settings, password: str, company_dir: Path = COMPANY_DIR
) -> SeedResult:
    """Load the company data. Existing rows are left as they are."""
    if settings.environment == "production":
        raise SeedError("refusing to load the AcmeCorp demo data into a production environment")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SeedError(f"the password must be at least {MIN_PASSWORD_LENGTH} characters")

    manifest = load_manifest(company_dir)
    result = SeedResult()

    departments = _seed_departments(db, manifest, result)
    roles = _seed_roles(db)
    _seed_users(db, manifest, departments, roles, password, result)
    db.commit()

    uploader = db.scalars(
        select(User).where(User.email == manifest.uploaded_by.strip().lower())
    ).one_or_none()
    if uploader is None:
        raise SeedError(f"uploaded_by names an unknown user: {manifest.uploaded_by}")

    for entry in manifest.documents:
        _seed_document(db, settings, uploader, entry, company_dir, result)
    # Covers documents stored before chunking existed, or with other chunk settings.
    result.documents_rechunked = rechunk_documents(db, uploader, settings)
    return result


def _seed_departments(db: Session, manifest: Manifest, result: SeedResult) -> dict[str, Department]:
    departments = {department.code: department for department in db.scalars(select(Department))}
    for entry in manifest.departments:
        code = entry.code.strip().lower()
        if code not in departments:
            departments[code] = Department(code=code, name=entry.name)
            db.add(departments[code])
            result.departments_created += 1
    return departments


def _seed_roles(db: Session) -> dict[str, Role]:
    roles = {role.code: role for role in db.scalars(select(Role))}
    for code in RoleCode:
        if code.value not in roles:
            roles[code.value] = Role(code=code.value)
            db.add(roles[code.value])
    return roles


def _seed_users(
    db: Session,
    manifest: Manifest,
    departments: dict[str, Department],
    roles: dict[str, Role],
    password: str,
    result: SeedResult,
) -> None:
    existing = set(db.scalars(select(User.email)))
    # Hashed once: Argon2 is slow on purpose, and the seeded users share a password.
    hashed_password = hash_password(password)
    for entry in manifest.users:
        email = entry.email.strip().lower()
        if email in existing:
            # Never reset the password or the roles of an account that is already there.
            continue
        department = departments.get(entry.department.strip().lower())
        if department is None:
            raise SeedError(f"{entry.email} is in an unknown department: {entry.department}")
        db.add(
            User(
                email=email,
                full_name=entry.full_name,
                hashed_password=hashed_password,
                department=department,
                roles=[roles[code.value] for code in entry.roles],
            )
        )
        result.users_created += 1


def _seed_document(
    db: Session,
    settings: Settings,
    uploader: User,
    entry: DocumentEntry,
    company_dir: Path,
    result: SeedResult,
) -> None:
    path = (company_dir / entry.file).resolve()
    if not path.is_relative_to(company_dir.resolve()):
        raise SeedError(f"{entry.file} is outside the company data folder")
    if not path.is_file():
        raise SeedError(f"{entry.file} is listed in the manifest but does not exist")
    data = path.read_bytes()

    already_stored = db.scalars(
        select(Document.id).where(
            Document.sha256 == hashlib.sha256(data).hexdigest(),
            Document.status == DocumentStatus.ACTIVE,
        )
    ).first()
    if already_stored is not None:
        result.documents_skipped += 1
        return

    now = utc_now()
    upload = Upload(
        filename=path.name,
        data=data,
        title=entry.title,
        department_code=entry.department,
        classification=entry.classification,
        grants=tuple(
            GrantRequest(
                email=grant.email,
                expires_at=(
                    now + timedelta(days=grant.expires_in_days)
                    if grant.expires_in_days is not None
                    else None
                ),
            )
            for grant in entry.grants
        ),
    )
    try:
        ingest(db, uploader, upload, settings)
    except AppError as error:
        # Name the file: the pipeline's message alone doesn't say which one it was.
        raise SeedError(f"{entry.file}: {error}") from error
    result.documents_ingested += 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load the AcmeCorp company data.")
    parser.add_argument(
        "--password",
        help="password for every seeded user (default: SEED_PASSWORD, or a generated one)",
    )
    arguments = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)

    password = arguments.password or os.environ.get("SEED_PASSWORD")
    generated = password is None
    if password is None:
        password = secrets.token_urlsafe(12)

    engine = create_db_engine(settings.database_url)
    try:
        Base.metadata.create_all(engine)
        with create_session_factory(engine, seal_key(settings))() as db:
            result = seed(db, settings, password)
    except SeedError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()

    print(f"departments created: {result.departments_created}")
    print(f"users created:       {result.users_created}")
    print(f"documents ingested:  {result.documents_ingested}")
    print(f"documents skipped:   {result.documents_skipped} (already stored)")
    print(f"documents rechunked: {result.documents_rechunked}")
    if generated and result.users_created:
        # Shown once and stored nowhere. It is not written to the logs.
        print(f"password for the new users: {password}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
