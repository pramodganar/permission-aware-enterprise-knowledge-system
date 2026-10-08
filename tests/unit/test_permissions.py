import pytest
from sqlalchemy import insert, text
from sqlalchemy.orm import Session

from app.authorization.permissions import ROLE_PERMISSIONS, Permission, permissions_for
from app.models import Department, Role, RoleCode, User
from app.models.role import user_roles

READ_PERMISSIONS = {
    Permission.READ_GENERAL_DOCUMENTS,
    Permission.READ_OWN_DEPARTMENT_DOCUMENTS,
    Permission.READ_GRANTED_DOCUMENTS,
    Permission.READ_ALL_DEPARTMENT_DOCUMENTS,
}

# Written out in full on purpose. Changing the policy means changing this
# table too, so a change can't slip through unnoticed.
EXPECTED = {
    RoleCode.EMPLOYEE: {
        Permission.READ_GENERAL_DOCUMENTS,
        Permission.READ_OWN_DEPARTMENT_DOCUMENTS,
        Permission.READ_GRANTED_DOCUMENTS,
    },
    RoleCode.EXECUTIVE: {
        Permission.READ_GENERAL_DOCUMENTS,
        Permission.READ_OWN_DEPARTMENT_DOCUMENTS,
        Permission.READ_GRANTED_DOCUMENTS,
        Permission.READ_ALL_DEPARTMENT_DOCUMENTS,
    },
    RoleCode.KNOWLEDGE_ADMIN: {Permission.MANAGE_DOCUMENTS},
    RoleCode.SECURITY_REVIEWER: {Permission.READ_AUDIT_LOG},
}


def user_with(db: Session, department: Department, *codes: RoleCode) -> User:
    roles = [Role(code=code) for code in codes]
    db.add_all(roles)
    db.commit()
    user = User(
        email="someone@acmecorp.example",
        full_name="Some One",
        hashed_password="x",
        department=department,
        roles=roles,
    )
    db.add(user)
    db.commit()
    return user


@pytest.mark.parametrize("code", list(RoleCode))
def test_each_role_grants_exactly_its_permissions(
    db: Session, engineering: Department, code: RoleCode
) -> None:
    assert permissions_for(user_with(db, engineering, code)) == EXPECTED[code]


def test_every_role_has_an_entry_in_the_policy() -> None:
    assert set(ROLE_PERMISSIONS) == set(RoleCode)


def test_every_permission_is_granted_by_at_least_one_role() -> None:
    granted = set().union(*ROLE_PERMISSIONS.values())

    assert granted == set(Permission)


def test_user_with_no_roles_has_no_permissions(db: Session, engineering: Department) -> None:
    assert permissions_for(user_with(db, engineering)) == frozenset()


def test_permissions_of_several_roles_add_up(db: Session, engineering: Department) -> None:
    user = user_with(db, engineering, RoleCode.EMPLOYEE, RoleCode.SECURITY_REVIEWER)

    assert permissions_for(user) == {
        Permission.READ_GENERAL_DOCUMENTS,
        Permission.READ_OWN_DEPARTMENT_DOCUMENTS,
        Permission.READ_GRANTED_DOCUMENTS,
        Permission.READ_AUDIT_LOG,
    }


@pytest.mark.parametrize("code", [RoleCode.KNOWLEDGE_ADMIN, RoleCode.SECURITY_REVIEWER])
def test_admin_and_reviewer_roles_include_no_read_access(
    db: Session, engineering: Department, code: RoleCode
) -> None:
    assert permissions_for(user_with(db, engineering, code)).isdisjoint(READ_PERMISSIONS)


def test_holding_both_admin_and_reviewer_still_gives_no_read_access(
    db: Session, engineering: Department
) -> None:
    user = user_with(db, engineering, RoleCode.KNOWLEDGE_ADMIN, RoleCode.SECURITY_REVIEWER)

    assert permissions_for(user).isdisjoint(READ_PERMISSIONS)


@pytest.mark.parametrize("code", [RoleCode.EMPLOYEE, RoleCode.EXECUTIVE])
def test_reading_roles_cannot_manage_documents_or_read_the_audit_log(
    db: Session, engineering: Department, code: RoleCode
) -> None:
    permissions = permissions_for(user_with(db, engineering, code))

    assert Permission.MANAGE_DOCUMENTS not in permissions
    assert Permission.READ_AUDIT_LOG not in permissions


def test_only_the_executive_role_reads_all_departments() -> None:
    roles = [
        code
        for code, permissions in ROLE_PERMISSIONS.items()
        if Permission.READ_ALL_DEPARTMENT_DOCUMENTS in permissions
    ]

    assert roles == [RoleCode.EXECUTIVE]


def test_inactive_user_has_no_permissions(db: Session, engineering: Department) -> None:
    user = user_with(db, engineering, RoleCode.EXECUTIVE)

    user.is_active = False

    assert permissions_for(user) == frozenset()


def test_role_unknown_to_the_code_grants_nothing(db: Session, engineering: Department) -> None:
    user = user_with(db, engineering)
    # Inserted with SQL, the way it would arrive if someone edited the database by hand.
    db.execute(text("INSERT INTO roles (code, description) VALUES ('superuser', '')"))
    role_id = db.execute(text("SELECT id FROM roles WHERE code = 'superuser'")).scalar_one()
    db.execute(insert(user_roles).values(user_id=user.id, role_id=role_id))
    db.commit()
    db.expire_all()

    assert [role.code for role in user.roles] == ["superuser"]
    assert permissions_for(user) == frozenset()


def test_unknown_role_does_not_remove_permissions_from_known_roles(
    db: Session, engineering: Department
) -> None:
    user = user_with(db, engineering, RoleCode.EMPLOYEE)
    db.execute(text("INSERT INTO roles (code, description) VALUES ('superuser', '')"))
    role_id = db.execute(text("SELECT id FROM roles WHERE code = 'superuser'")).scalar_one()
    db.execute(insert(user_roles).values(user_id=user.id, role_id=role_id))
    db.commit()
    db.expire_all()

    assert permissions_for(user) == EXPECTED[RoleCode.EMPLOYEE]


def test_policy_cannot_be_changed_at_runtime() -> None:
    with pytest.raises(TypeError):
        ROLE_PERMISSIONS[RoleCode.EMPLOYEE] = frozenset(Permission)  # type: ignore[index]

    with pytest.raises(AttributeError):
        ROLE_PERMISSIONS[RoleCode.EMPLOYEE].add(  # type: ignore[attr-defined]
            Permission.READ_ALL_DEPARTMENT_DOCUMENTS
        )
