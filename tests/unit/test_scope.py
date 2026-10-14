"""Working out what one person may search."""

import dataclasses
import logging
from datetime import timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.authorization.document_access import revoke_access
from app.authorization.permissions import Permission
from app.core.config import Settings
from app.core.database import utc_now
from app.ingestion.pipeline import delete_document
from app.models import Chunk, Document, DocumentGrant, Role, RoleCode, User
from app.retrieval.scope import AccessScope, access_scope
from logcheck import everything_logged
from scripts.seed import seed

# person: the documents they may read, by title
GENERAL = {"Leave Policy", "Code of Conduct", "Travel and Expense Policy"}
EXPECTED_DOCUMENTS = {
    "alice": GENERAL | {"Deployment Runbook", "Payments Outage Postmortem"},
    "bob": GENERAL | {"Vendor Payment Approval Limits", "Headcount Budget 2027"},
    "carol": GENERAL
    | {"Performance Improvement Plan Process", "Salary Bands 2026", "HR Case File 2026-014"},
    "david": GENERAL | {"Discount Approval Policy"},
    "emma": GENERAL
    | {
        "Contract Data Retention Obligations",
        "Supplier Dispute Status",
        "Project Falcon Acquisition Paper",
    },
    "frank": GENERAL | {"Warehouse Incident SOP"},
    "henry": set(),
    "irene": set(),
}


@pytest.fixture
def company(db: Session, settings: Settings) -> Session:
    seed(db, settings, "scope-test-password")
    return db


def person(db: Session, name: str) -> User:
    return db.scalars(select(User).where(User.email == f"{name}@acmecorp.example")).one()


def titles(db: Session, scope: AccessScope) -> set[str]:
    return set(db.scalars(select(Document.title).where(Document.id.in_(scope.document_ids))))


def chunks_of(db: Session, document_ids: frozenset[int]) -> set[int]:
    return set(db.scalars(select(Chunk.id).where(Chunk.document_id.in_(document_ids))))


# --- what is in a scope ---------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(EXPECTED_DOCUMENTS))
def test_each_person_gets_exactly_their_documents(company: Session, name: str) -> None:
    scope = access_scope(company, person(company, name))

    assert titles(company, scope) == EXPECTED_DOCUMENTS[name]


def test_an_executive_gets_every_department_but_not_the_case_file(company: Session) -> None:
    scope = access_scope(company, person(company, "grace"))

    assert len(scope.document_ids) == 16
    assert "HR Case File 2026-014" not in titles(company, scope)


@pytest.mark.parametrize("name", [*EXPECTED_DOCUMENTS, "grace"])
def test_the_chunks_are_all_the_chunks_of_those_documents(company: Session, name: str) -> None:
    scope = access_scope(company, person(company, name))

    assert scope.chunk_ids == chunks_of(company, scope.document_ids)


def test_the_scope_records_who_and_when_and_with_which_permissions(company: Session) -> None:
    alice = person(company, "alice")
    moment = utc_now()

    scope = access_scope(company, alice, moment)

    assert scope.user_id == alice.id
    assert scope.decided_at == moment
    assert scope.permissions == {
        Permission.READ_GENERAL_DOCUMENTS,
        Permission.READ_OWN_DEPARTMENT_DOCUMENTS,
        Permission.READ_GRANTED_DOCUMENTS,
    }


def test_allows_chunk_answers_for_one_chunk(company: Session) -> None:
    scope = access_scope(company, person(company, "david"))
    every_chunk = set(company.scalars(select(Chunk.id)))

    for chunk_id in every_chunk:
        assert scope.allows_chunk(chunk_id) == (chunk_id in scope.chunk_ids)
    assert not scope.allows_chunk(999_999)


# --- people who may read nothing ------------------------------------------------------


@pytest.mark.parametrize("name", ["henry", "irene"])
def test_a_role_that_does_not_read_documents_gives_an_empty_scope(
    company: Session, name: str
) -> None:
    scope = access_scope(company, person(company, name))

    assert scope.is_empty
    assert scope.document_ids == frozenset()
    assert scope.permissions  # they have permissions, just not reading ones


def test_no_query_is_made_for_someone_who_may_read_nothing(company: Session) -> None:
    henry = person(company, "henry")
    _ = henry.roles  # loaded before counting
    statements: list[str] = []
    engine = company.get_bind()

    def record(*arguments: object) -> None:
        statements.append(str(arguments[2]))

    event.listen(engine, "before_cursor_execute", record)
    try:
        access_scope(company, henry)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert statements == []


def test_one_query_is_made_for_a_reader(company: Session) -> None:
    grace = person(company, "grace")
    _ = grace.roles
    statements: list[str] = []
    engine = company.get_bind()

    def record(*arguments: object) -> None:
        statements.append(str(arguments[2]))

    event.listen(engine, "before_cursor_execute", record)
    try:
        access_scope(company, grace)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert len(statements) == 1


def test_an_inactive_person_gets_an_empty_scope(company: Session) -> None:
    grace = person(company, "grace")
    grace.is_active = False
    company.commit()

    scope = access_scope(company, grace)

    assert scope.is_empty
    assert scope.permissions == frozenset()


def test_a_person_with_no_roles_gets_an_empty_scope(company: Session) -> None:
    alice = person(company, "alice")
    alice.roles = []
    company.commit()

    assert access_scope(company, alice).is_empty


def test_an_empty_scope_is_not_an_error(company: Session) -> None:
    # The answer for "may read nothing" must look like any other question
    # with no match, so working out the scope must not fail.
    scope = access_scope(company, person(company, "irene"))

    assert isinstance(scope, AccessScope)


# --- changes take effect on the next question ---------------------------------------


def test_a_revoked_grant_is_gone_from_the_next_scope(company: Session) -> None:
    carol = person(company, "carol")
    case_file = company.scalars(
        select(Document).where(Document.title == "HR Case File 2026-014")
    ).one()
    before = access_scope(company, carol)

    revoke_access(company, person(company, "henry"), case_file.id, carol.email)
    after = access_scope(company, carol)

    assert case_file.id in before.document_ids
    assert case_file.id not in after.document_ids
    assert after.chunk_ids < before.chunk_ids


def test_a_scope_already_worked_out_does_not_change(company: Session) -> None:
    carol = person(company, "carol")
    case_file = company.scalars(
        select(Document).where(Document.title == "HR Case File 2026-014")
    ).one()
    scope = access_scope(company, carol)

    revoke_access(company, person(company, "henry"), case_file.id, carol.email)

    # The question already being answered keeps the scope it started with.
    assert case_file.id in scope.document_ids


def test_a_role_change_is_in_the_next_scope(company: Session) -> None:
    alice = person(company, "alice")
    before = access_scope(company, alice)

    executive = company.scalars(select(Role).where(Role.code == RoleCode.EXECUTIVE)).one()
    alice.roles = [executive]
    company.commit()
    after = access_scope(company, alice)

    assert len(before.document_ids) == 5
    # Every general and departmental document. Not the restricted ones: Grace
    # reads one of those through a grant, which Alice does not have.
    assert len(after.document_ids) == 15


def test_a_deleted_document_is_gone_from_the_next_scope(company: Session) -> None:
    alice = person(company, "alice")
    runbook = company.scalars(select(Document).where(Document.title == "Deployment Runbook")).one()

    delete_document(company, person(company, "henry"), runbook.id)

    assert runbook.id not in access_scope(company, alice).document_ids


def test_the_scope_is_decided_at_one_moment(company: Session) -> None:
    emma = person(company, "emma")
    paper = company.scalars(
        select(Document).where(Document.title == "Project Falcon Acquisition Paper")
    ).one()
    grant = company.scalars(
        select(DocumentGrant).where(
            DocumentGrant.document_id == paper.id, DocumentGrant.user_id == emma.id
        )
    ).one()
    assert grant.expires_at is not None

    just_before = access_scope(company, emma, grant.expires_at - timedelta(seconds=1))
    at_expiry = access_scope(company, emma, grant.expires_at)

    assert paper.id in just_before.document_ids
    assert paper.id not in at_expiry.document_ids
    assert just_before.chunk_ids == chunks_of(company, just_before.document_ids)
    assert at_expiry.chunk_ids == chunks_of(company, at_expiry.document_ids)


# --- the scope can't be widened ---------------------------------------------------------


def test_the_scope_cannot_be_changed_after_it_is_made(company: Session) -> None:
    scope = access_scope(company, person(company, "david"))

    with pytest.raises(dataclasses.FrozenInstanceError):
        scope.chunk_ids = frozenset({1, 2, 3})  # type: ignore[misc]
    with pytest.raises(AttributeError):
        scope.chunk_ids.add(1)  # type: ignore[attr-defined]


def test_a_scope_says_which_documents_but_logs_only_how_many(
    company: Session, caplog: pytest.LogCaptureFixture
) -> None:
    carol = person(company, "carol")

    with caplog.at_level(logging.INFO):
        access_scope(company, carol)

    record = next(r for r in caplog.records if r.message == "access scope decided")
    assert record.user_id == carol.id  # type: ignore[attr-defined]
    assert record.documents == 6  # type: ignore[attr-defined]
    logged = everything_logged(caplog)
    assert "Case File" not in logged
    assert "carol@" not in logged
