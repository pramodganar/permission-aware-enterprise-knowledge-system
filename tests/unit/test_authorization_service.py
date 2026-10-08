from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.authorization.service import can_read, readable_documents
from app.models import Classification, Department, Document, DocumentGrant, Role, RoleCode, User

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@dataclass
class World:
    """A small company: three departments, five users, five documents."""

    db: Session
    users: dict[str, User]
    documents: dict[str, Document]

    def grant(self, document: str, user: str, expires_at: datetime | None = None) -> None:
        self.db.add(
            DocumentGrant(
                document=self.documents[document],
                user_id=self.users[user].id,
                granted_by_id=self.users["admin"].id,
                expires_at=expires_at,
            )
        )
        self.db.commit()

    def can_read(self, user: str, document: str) -> bool:
        return can_read(self.users[user], self.documents[document], NOW)

    def readable(self, user: str) -> set[str]:
        found = self.db.scalars(readable_documents(self.users[user], NOW)).all()
        return {name for name, document in self.documents.items() if document in found}


@pytest.fixture
def world(db: Session) -> World:
    departments = {
        code: Department(code=code, name=code.title())
        for code in ("engineering", "hr", "executive")
    }
    roles = {code: Role(code=code) for code in RoleCode}
    db.add_all([*departments.values(), *roles.values()])

    def user(name: str, department: str, *role_codes: RoleCode) -> User:
        return User(
            email=f"{name}@acmecorp.example",
            full_name=name.title(),
            hashed_password="x",
            department=departments[department],
            roles=[roles[code] for code in role_codes],
        )

    users = {
        "alice": user("alice", "engineering", RoleCode.EMPLOYEE),
        "carol": user("carol", "hr", RoleCode.EMPLOYEE),
        "grace": user("grace", "executive", RoleCode.EXECUTIVE),
        "admin": user("admin", "engineering", RoleCode.KNOWLEDGE_ADMIN),
        "nobody": user("nobody", "engineering"),
    }

    def document(title: str, department: str, classification: Classification) -> Document:
        return Document(
            title=title, department=departments[department], classification=classification
        )

    documents = {
        "leave_policy": document("Leave Policy", "hr", Classification.GENERAL),
        "runbook": document("Deployment Runbook", "engineering", Classification.DEPARTMENTAL),
        "salary_bands": document("Salary Bands", "hr", Classification.DEPARTMENTAL),
        "exec_comp": document(
            "Executive Compensation Strategy", "executive", Classification.DEPARTMENTAL
        ),
        "case_file": document("HR Case File", "hr", Classification.RESTRICTED),
    }

    db.add_all([*users.values(), *documents.values()])
    db.commit()
    return World(db, users, documents)


# Rule 1: general documents


@pytest.mark.parametrize("user", ["alice", "carol", "grace"])
def test_readers_can_read_general_documents(world: World, user: str) -> None:
    assert world.can_read(user, "leave_policy")


# Rule 2: departmental documents of the user's own department


def test_employee_can_read_own_department_documents(world: World) -> None:
    assert world.can_read("alice", "runbook")
    assert world.can_read("carol", "salary_bands")


def test_employee_cannot_read_another_departments_documents(world: World) -> None:
    assert not world.can_read("alice", "salary_bands")
    assert not world.can_read("alice", "exec_comp")
    assert not world.can_read("carol", "runbook")
    assert not world.can_read("carol", "exec_comp")


# Rule 3: departmental documents of every department


def test_executive_can_read_every_departments_documents(world: World) -> None:
    assert world.can_read("grace", "runbook")
    assert world.can_read("grace", "salary_bands")
    assert world.can_read("grace", "exec_comp")


# Restricted documents: no rule applies without a grant


@pytest.mark.parametrize("user", ["alice", "carol", "grace", "admin", "nobody"])
def test_nobody_can_read_a_restricted_document_without_a_grant(world: World, user: str) -> None:
    assert not world.can_read(user, "case_file")


# Rule 4: grants


def test_grant_lets_the_named_user_read_a_restricted_document(world: World) -> None:
    world.grant("case_file", "carol")

    assert world.can_read("carol", "case_file")


def test_grant_does_not_extend_to_anyone_else(world: World) -> None:
    world.grant("case_file", "carol")

    assert not world.can_read("alice", "case_file")
    assert not world.can_read("grace", "case_file")


def test_executive_needs_a_grant_for_a_restricted_document_too(world: World) -> None:
    assert not world.can_read("grace", "case_file")

    world.grant("case_file", "grace")

    assert world.can_read("grace", "case_file")


def test_grant_lets_someone_outside_the_department_read_one_document(world: World) -> None:
    world.grant("runbook", "carol")

    assert world.can_read("carol", "runbook")
    assert not world.can_read("carol", "exec_comp")


@pytest.mark.parametrize(
    ("expires_in", "allowed"),
    [
        (timedelta(days=30), True),
        (timedelta(seconds=1), True),
        (timedelta(0), False),
        (timedelta(seconds=-1), False),
        (timedelta(days=-30), False),
    ],
)
def test_grant_works_only_until_it_expires(
    world: World, expires_in: timedelta, allowed: bool
) -> None:
    world.grant("case_file", "carol", expires_at=NOW + expires_in)

    assert world.can_read("carol", "case_file") is allowed
    assert ("case_file" in world.readable("carol")) is allowed


def test_grant_does_not_make_a_non_reader_into_a_reader(world: World) -> None:
    world.grant("case_file", "admin")
    world.grant("runbook", "nobody")

    assert not world.can_read("admin", "case_file")
    assert not world.can_read("nobody", "runbook")


# Deny by default


@pytest.mark.parametrize(
    "document", ["leave_policy", "runbook", "salary_bands", "exec_comp", "case_file"]
)
@pytest.mark.parametrize("user", ["admin", "nobody"])
def test_users_without_a_reading_role_can_read_nothing(
    world: World, user: str, document: str
) -> None:
    assert not world.can_read(user, document)


@pytest.mark.parametrize("user", ["alice", "grace"])
def test_inactive_user_can_read_nothing(world: World, user: str) -> None:
    world.grant("case_file", user)
    world.users[user].is_active = False
    world.db.commit()

    assert world.readable(user) == set()
    assert not any(world.can_read(user, document) for document in world.documents)


def test_document_with_an_unknown_classification_is_readable_by_nobody(world: World) -> None:
    # Written with SQL, as it would be if someone edited the database by hand.
    world.db.execute(
        text("UPDATE documents SET classification = 'public' WHERE id = :id"),
        {"id": world.documents["leave_policy"].id},
    )
    world.db.commit()
    world.db.expire_all()

    for user in world.users:
        assert not world.can_read(user, "leave_policy")
        assert "leave_policy" not in world.readable(user)


def test_grant_does_not_rescue_a_document_with_an_unknown_classification(world: World) -> None:
    world.db.execute(
        text("UPDATE documents SET classification = 'public' WHERE id = :id"),
        {"id": world.documents["leave_policy"].id},
    )
    world.db.commit()
    world.db.expire_all()
    world.grant("leave_policy", "alice")

    assert not world.can_read("alice", "leave_policy")
    assert "leave_policy" not in world.readable("alice")


# Changes apply immediately


def test_reclassifying_a_document_changes_who_can_read_it(world: World) -> None:
    assert world.can_read("alice", "leave_policy")

    world.documents["leave_policy"].classification = Classification.DEPARTMENTAL
    world.db.commit()

    assert not world.can_read("alice", "leave_policy")
    assert world.can_read("carol", "leave_policy")


def test_moving_department_moves_access(world: World) -> None:
    alice = world.users["alice"]
    alice.department = world.users["carol"].department
    world.db.commit()

    assert world.readable("alice") == {"leave_policy", "salary_bands"}


def test_losing_the_executive_role_removes_cross_department_access(world: World) -> None:
    grace = world.users["grace"]
    grace.roles = list(world.users["alice"].roles)
    world.db.commit()

    assert world.readable("grace") == {"leave_policy", "exec_comp"}


# The scope query


def test_scope_query_returns_what_each_user_may_read(world: World) -> None:
    world.grant("case_file", "carol")

    assert world.readable("alice") == {"leave_policy", "runbook"}
    assert world.readable("carol") == {"leave_policy", "salary_bands", "case_file"}
    assert world.readable("grace") == {"leave_policy", "runbook", "salary_bands", "exec_comp"}
    assert world.readable("admin") == set()
    assert world.readable("nobody") == set()


def test_scope_query_and_single_check_always_agree(world: World) -> None:
    world.grant("case_file", "carol")
    world.grant("runbook", "carol", expires_at=NOW + timedelta(days=1))
    world.grant("exec_comp", "alice", expires_at=NOW - timedelta(days=1))
    world.grant("case_file", "admin")
    world.grant("salary_bands", "grace")

    for user in world.users:
        one_by_one = {name for name in world.documents if world.can_read(user, name)}
        assert world.readable(user) == one_by_one, user


def test_current_time_is_used_when_none_is_given(world: World, db: Session) -> None:
    world.grant("case_file", "carol", expires_at=datetime.now(UTC) + timedelta(hours=1))
    world.grant("runbook", "carol", expires_at=datetime.now(UTC) - timedelta(hours=1))
    carol = world.users["carol"]

    assert can_read(carol, world.documents["case_file"])
    assert not can_read(carol, world.documents["runbook"])
    assert set(db.scalars(readable_documents(carol)).all()) == {
        world.documents["leave_policy"],
        world.documents["salary_bands"],
        world.documents["case_file"],
    }
