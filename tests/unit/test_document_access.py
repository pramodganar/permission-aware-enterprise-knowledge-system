import logging
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from acmecorp import NOW, World
from app.authorization.document_access import (
    GrantRequest,
    change_labels,
    grant_access,
    revoke_access,
)
from app.core.config import Settings
from app.core.exceptions import InvalidInputError, NotFoundError, PermissionDeniedError
from app.ingestion.pipeline import Upload, ingest
from app.models import Document, DocumentGrant, Role, RoleCode

CAROL = "carol@acmecorp.example"
ALICE = "alice@acmecorp.example"
NOT_ADMINS = ["alice", "carol", "grace", "reviewer", "nobody"]


def grant_count(world: World) -> int:
    return world.db.scalar(select(func.count()).select_from(DocumentGrant)) or 0


def make_admin_a_reader_too(world: World) -> None:
    employee = world.db.scalars(select(Role).where(Role.code == RoleCode.EMPLOYEE)).one()
    world.users["admin"].roles.append(employee)
    world.db.commit()


# Granting


def test_admin_grants_a_named_user_access_to_a_restricted_document(world: World) -> None:
    assert not world.can_read("carol", "case_file")

    grant = grant_access(
        world.db, world.users["admin"], world.documents["case_file"].id, GrantRequest(CAROL)
    )

    assert grant.user_id == world.users["carol"].id
    assert grant.granted_by_id == world.users["admin"].id
    assert grant.expires_at is None
    assert world.can_read("carol", "case_file")
    assert not world.can_read("alice", "case_file")
    assert not world.can_read("grace", "case_file")


def test_grant_can_have_an_expiry(world: World) -> None:
    expires = NOW + timedelta(days=30)

    grant_access(
        world.db,
        world.users["admin"],
        world.documents["runbook"].id,
        GrantRequest(CAROL, expires),
        now=NOW,
    )

    assert world.can_read("carol", "runbook")
    assert world.documents["runbook"].grants[0].expires_at == expires


def test_granting_again_replaces_the_expiry_and_keeps_one_grant(world: World) -> None:
    document_id = world.documents["case_file"].id
    grant_access(world.db, world.users["admin"], document_id, GrantRequest(CAROL), now=NOW)

    grant_access(
        world.db,
        world.users["admin"],
        document_id,
        GrantRequest(CAROL, NOW + timedelta(days=7)),
        now=NOW,
    )

    assert grant_count(world) == 1
    assert world.documents["case_file"].grants[0].expires_at == NOW + timedelta(days=7)


@pytest.mark.parametrize("email", ["CAROL@ACMECORP.EXAMPLE", "  carol@acmecorp.example "])
def test_grantee_email_is_matched_without_case_or_spaces(world: World, email: str) -> None:
    grant_access(
        world.db, world.users["admin"], world.documents["case_file"].id, GrantRequest(email)
    )

    assert world.can_read("carol", "case_file")


@pytest.mark.parametrize("user", NOT_ADMINS)
def test_only_an_admin_can_grant(world: World, user: str) -> None:
    with pytest.raises(PermissionDeniedError):
        grant_access(
            world.db, world.users[user], world.documents["case_file"].id, GrantRequest(CAROL)
        )

    assert grant_count(world) == 0


def test_reader_cannot_grant_themselves_access(world: World) -> None:
    with pytest.raises(PermissionDeniedError):
        grant_access(
            world.db, world.users["alice"], world.documents["exec_comp"].id, GrantRequest(ALICE)
        )

    assert not world.can_read("alice", "exec_comp")


def test_admin_who_is_also_a_reader_cannot_grant_themselves_access(world: World) -> None:
    make_admin_a_reader_too(world)
    admin = world.users["admin"]

    with pytest.raises(PermissionDeniedError):
        grant_access(world.db, admin, world.documents["case_file"].id, GrantRequest(admin.email))

    assert grant_count(world) == 0
    assert not world.can_read("admin", "case_file")


def test_admin_who_is_also_a_reader_can_still_grant_to_others(world: World) -> None:
    make_admin_a_reader_too(world)

    grant_access(
        world.db, world.users["admin"], world.documents["case_file"].id, GrantRequest(CAROL)
    )

    assert world.can_read("carol", "case_file")
    assert not world.can_read("admin", "case_file")


def test_grant_to_an_unknown_user_is_rejected(world: World) -> None:
    with pytest.raises(InvalidInputError, match="no active user"):
        grant_access(
            world.db,
            world.users["admin"],
            world.documents["case_file"].id,
            GrantRequest("mallory@elsewhere.example"),
        )


def test_grant_to_an_inactive_user_is_rejected(world: World) -> None:
    world.users["carol"].is_active = False
    world.db.commit()

    with pytest.raises(InvalidInputError, match="no active user"):
        grant_access(
            world.db, world.users["admin"], world.documents["case_file"].id, GrantRequest(CAROL)
        )


@pytest.mark.parametrize("user", ["reviewer", "nobody"])
def test_grant_to_someone_who_cannot_read_documents_is_rejected(world: World, user: str) -> None:
    with pytest.raises(InvalidInputError, match="does not have a role that can read"):
        grant_access(
            world.db,
            world.users["admin"],
            world.documents["case_file"].id,
            GrantRequest(world.users[user].email),
        )

    assert grant_count(world) == 0


@pytest.mark.parametrize("expires_in", [timedelta(0), timedelta(seconds=-1), timedelta(days=-30)])
def test_expiry_must_be_in_the_future(world: World, expires_in: timedelta) -> None:
    with pytest.raises(InvalidInputError, match="in the future"):
        grant_access(
            world.db,
            world.users["admin"],
            world.documents["case_file"].id,
            GrantRequest(CAROL, NOW + expires_in),
            now=NOW,
        )


def test_expiry_without_a_timezone_is_rejected(world: World) -> None:
    with pytest.raises(InvalidInputError, match="timezone"):
        grant_access(
            world.db,
            world.users["admin"],
            world.documents["case_file"].id,
            GrantRequest(CAROL, datetime(2030, 1, 1, 12, 0)),
        )


def test_grant_on_a_document_that_does_not_exist_is_not_found(world: World) -> None:
    with pytest.raises(NotFoundError):
        grant_access(world.db, world.users["admin"], 999999, GrantRequest(CAROL))


def test_grant_is_logged_with_ids_only(world: World, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.authorization.document_access")

    grant_access(
        world.db, world.users["admin"], world.documents["case_file"].id, GrantRequest(CAROL)
    )

    record = caplog.records[-1]
    assert record.getMessage() == "document access granted"
    assert record.__dict__["document_id"] == world.documents["case_file"].id
    assert record.__dict__["user_id"] == world.users["admin"].id
    assert record.__dict__["grantee_id"] == world.users["carol"].id
    assert "carol@" not in str(vars(record))
    assert "Case File" not in str(vars(record))


# Revoking


def test_revoking_removes_access(world: World) -> None:
    world.grant("case_file", "carol")
    assert world.can_read("carol", "case_file")

    revoke_access(world.db, world.users["admin"], world.documents["case_file"].id, CAROL)

    assert grant_count(world) == 0
    assert not world.can_read("carol", "case_file")


def test_revoking_one_grant_leaves_the_others(world: World) -> None:
    world.grant("case_file", "carol")
    world.grant("case_file", "grace")

    revoke_access(world.db, world.users["admin"], world.documents["case_file"].id, CAROL)

    assert not world.can_read("carol", "case_file")
    assert world.can_read("grace", "case_file")


@pytest.mark.parametrize("email", [ALICE, "mallory@elsewhere.example"])
def test_revoking_a_grant_that_does_not_exist_is_not_found(world: World, email: str) -> None:
    world.grant("case_file", "carol")

    with pytest.raises(NotFoundError):
        revoke_access(world.db, world.users["admin"], world.documents["case_file"].id, email)

    assert grant_count(world) == 1


@pytest.mark.parametrize("user", NOT_ADMINS)
def test_only_an_admin_can_revoke(world: World, user: str) -> None:
    world.grant("case_file", "carol")

    with pytest.raises(PermissionDeniedError):
        revoke_access(world.db, world.users[user], world.documents["case_file"].id, CAROL)

    assert grant_count(world) == 1


# Changing labels


def test_reclassifying_as_restricted_hides_a_document_from_everyone(world: World) -> None:
    assert world.can_read("grace", "exec_comp")

    change_labels(
        world.db,
        world.users["admin"],
        world.documents["exec_comp"].id,
        classification="restricted",
    )

    assert not any(world.can_read(user, "exec_comp") for user in world.users)


def test_reclassifying_as_general_opens_a_document_to_all_readers(world: World) -> None:
    assert not world.can_read("alice", "salary_bands")

    change_labels(
        world.db,
        world.users["admin"],
        world.documents["salary_bands"].id,
        classification=" General ",
    )

    assert world.can_read("alice", "salary_bands")
    assert not world.can_read("nobody", "salary_bands")


def test_moving_a_document_to_another_department_moves_its_readers(world: World) -> None:
    change_labels(
        world.db, world.users["admin"], world.documents["runbook"].id, department_code="hr"
    )

    assert not world.can_read("alice", "runbook")
    assert world.can_read("carol", "runbook")


def test_both_labels_can_change_at_once(world: World) -> None:
    document = change_labels(
        world.db,
        world.users["admin"],
        world.documents["runbook"].id,
        classification="general",
        department_code="hr",
    )

    assert document.classification == "general"
    assert document.department.code == "hr"


@pytest.mark.parametrize("classification", ["public", "confidential", " "])
def test_unknown_classification_is_rejected_and_nothing_changes(
    world: World, classification: str
) -> None:
    with pytest.raises(InvalidInputError, match="classification must be one of"):
        change_labels(
            world.db,
            world.users["admin"],
            world.documents["runbook"].id,
            classification=classification,
            department_code="hr",
        )

    world.db.rollback()
    assert world.documents["runbook"].classification == "departmental"
    assert world.documents["runbook"].department.code == "engineering"


def test_unknown_department_is_rejected_and_nothing_changes(world: World) -> None:
    with pytest.raises(InvalidInputError, match="unknown department"):
        change_labels(
            world.db,
            world.users["admin"],
            world.documents["runbook"].id,
            classification="general",
            department_code="marketing",
        )

    world.db.rollback()
    assert world.documents["runbook"].classification == "departmental"


def test_changing_nothing_is_rejected(world: World) -> None:
    with pytest.raises(InvalidInputError, match="nothing to change"):
        change_labels(world.db, world.users["admin"], world.documents["runbook"].id)


@pytest.mark.parametrize("user", NOT_ADMINS)
def test_only_an_admin_can_change_labels(world: World, user: str) -> None:
    with pytest.raises(PermissionDeniedError):
        change_labels(
            world.db, world.users[user], world.documents["exec_comp"].id, classification="general"
        )

    assert world.documents["exec_comp"].classification == "departmental"


def test_changing_labels_of_a_missing_document_is_not_found(world: World) -> None:
    with pytest.raises(NotFoundError):
        change_labels(world.db, world.users["admin"], 999999, classification="general")


def test_label_change_is_logged_with_old_and_new_values(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="app.authorization.document_access")

    change_labels(
        world.db, world.users["admin"], world.documents["runbook"].id, classification="general"
    )

    record = caplog.records[-1]
    assert record.getMessage() == "document labels changed"
    assert record.__dict__["old_classification"] == "departmental"
    assert record.__dict__["new_classification"] == "general"
    assert record.__dict__["old_department"] == "engineering"
    assert "Runbook" not in str(vars(record))


# Grants given at ingestion


def upload(**changes: object) -> Upload:
    fields: dict[str, object] = {
        "filename": "case-2026-021.txt",
        "data": b"Summary of the case and its outcome.",
        "title": "HR Case File 2026-021",
        "department_code": "hr",
        "classification": "restricted",
        "grants": (GrantRequest(CAROL),),
    }
    fields.update(changes)
    return Upload(**fields)  # type: ignore[arg-type]


def test_restricted_document_is_ingested_with_its_readers(world: World, settings: Settings) -> None:
    world.documents["new"] = ingest(world.db, world.users["admin"], upload(), settings)

    readers = {user for user in world.users if world.can_read(user, "new")}
    grant = world.documents["new"].grants[0]

    assert readers == {"carol"}
    assert grant.granted_by_id == world.users["admin"].id


def test_restricted_document_without_a_reader_is_rejected(world: World, settings: Settings) -> None:
    before = world.db.scalar(select(func.count()).select_from(Document))

    with pytest.raises(InvalidInputError, match="at least one named reader"):
        ingest(world.db, world.users["admin"], upload(grants=()), settings)

    assert world.db.scalar(select(func.count()).select_from(Document)) == before


@pytest.mark.parametrize("classification", ["general", "departmental"])
def test_other_classifications_do_not_need_a_reader(
    world: World, settings: Settings, classification: str
) -> None:
    assert ingest(
        world.db, world.users["admin"], upload(classification=classification, grants=()), settings
    ).id


def test_several_readers_and_an_expiry_at_ingestion(world: World, settings: Settings) -> None:
    soon = datetime.now(NOW.tzinfo) + timedelta(days=14)
    requests = (GrantRequest(CAROL), GrantRequest("grace@acmecorp.example", soon))

    world.documents["new"] = ingest(
        world.db, world.users["admin"], upload(grants=requests), settings
    )

    assert {user for user in world.users if world.can_read(user, "new")} == {"carol", "grace"}
    expiries = {grant.user_id: grant.expires_at for grant in world.documents["new"].grants}
    assert expiries == {world.users["carol"].id: None, world.users["grace"].id: soon}


@pytest.mark.parametrize(
    ("requests", "message"),
    [
        ((GrantRequest("mallory@elsewhere.example"),), "no active user"),
        ((GrantRequest(CAROL), GrantRequest("nobody@acmecorp.example")), "does not have a role"),
        ((GrantRequest(CAROL), GrantRequest(" Carol@AcmeCorp.example ")), "more than once"),
    ],
    ids=["unknown user", "user who cannot read", "same user twice"],
)
def test_bad_reader_list_stops_the_ingestion_and_stores_nothing(
    world: World, settings: Settings, requests: tuple[GrantRequest, ...], message: str
) -> None:
    documents = world.db.scalar(select(func.count()).select_from(Document))

    with pytest.raises(InvalidInputError, match=message):
        ingest(world.db, world.users["admin"], upload(grants=requests), settings)

    assert world.db.scalar(select(func.count()).select_from(Document)) == documents
    assert grant_count(world) == 0


def test_uploader_cannot_name_themselves_as_a_reader(world: World, settings: Settings) -> None:
    make_admin_a_reader_too(world)
    admin = world.users["admin"]

    with pytest.raises(PermissionDeniedError):
        ingest(world.db, admin, upload(grants=(GrantRequest(admin.email),)), settings)

    assert grant_count(world) == 0
