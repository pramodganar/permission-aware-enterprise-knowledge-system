import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Department, Role, RoleCode, User
from app.models.role import user_roles


def make_user(department: Department, email: str = "alice@acmecorp.example") -> User:
    return User(email=email, full_name="Test User", hashed_password="x", department=department)


def make_role(db: Session, code: RoleCode) -> Role:
    role = Role(code=code)
    db.add(role)
    db.commit()
    return role


def test_role_is_saved_and_read_back(db: Session) -> None:
    db.add(Role(code=RoleCode.EMPLOYEE, description="Reads own department and general documents"))
    db.commit()

    role = db.scalars(select(Role)).one()

    assert role.code == "employee"
    assert role.description == "Reads own department and general documents"


@pytest.mark.parametrize("code", list(RoleCode))
def test_every_known_role_can_be_created(db: Session, code: RoleCode) -> None:
    assert make_role(db, code).code == code


def test_code_is_stored_lowercase_and_trimmed() -> None:
    assert Role(code="  Executive ").code == "executive"


@pytest.mark.parametrize("code", ["exective", "admin", "superuser", ""])
def test_unknown_role_code_is_rejected(code: str) -> None:
    with pytest.raises(ValueError, match="is not a valid RoleCode"):
        Role(code=code)


def test_role_code_must_be_unique(db: Session) -> None:
    make_role(db, RoleCode.EMPLOYEE)

    db.add(Role(code=RoleCode.EMPLOYEE))
    with pytest.raises(IntegrityError):
        db.commit()


def test_new_user_has_no_roles(db: Session, engineering: Department) -> None:
    user = make_user(engineering)
    db.add(user)
    db.commit()

    assert user.roles == []
    assert not any(user.has_role(code) for code in RoleCode)


def test_user_can_be_given_a_role(db: Session, engineering: Department) -> None:
    employee = make_role(db, RoleCode.EMPLOYEE)
    user = make_user(engineering)
    user.roles.append(employee)
    db.add(user)
    db.commit()
    db.expire_all()

    saved = db.scalars(select(User)).one()

    assert saved.has_role(RoleCode.EMPLOYEE)
    assert not saved.has_role(RoleCode.EXECUTIVE)


def test_user_can_hold_several_roles(db: Session, engineering: Department) -> None:
    roles = [make_role(db, RoleCode.EMPLOYEE), make_role(db, RoleCode.SECURITY_REVIEWER)]
    user = make_user(engineering)
    user.roles = roles
    db.add(user)
    db.commit()
    db.expire_all()

    saved = db.scalars(select(User)).one()

    assert sorted(role.code for role in saved.roles) == ["employee", "security_reviewer"]


def test_admin_role_alone_is_not_the_employee_role(db: Session, engineering: Department) -> None:
    admin = make_role(db, RoleCode.KNOWLEDGE_ADMIN)
    user = make_user(engineering)
    user.roles.append(admin)
    db.add(user)
    db.commit()

    assert user.has_role(RoleCode.KNOWLEDGE_ADMIN)
    assert not user.has_role(RoleCode.EMPLOYEE)
    assert not user.has_role(RoleCode.EXECUTIVE)


def test_role_can_be_removed_from_a_user(db: Session, engineering: Department) -> None:
    executive = make_role(db, RoleCode.EXECUTIVE)
    user = make_user(engineering)
    user.roles.append(executive)
    db.add(user)
    db.commit()

    user.roles.remove(executive)
    db.commit()
    db.expire_all()

    assert not db.scalars(select(User)).one().has_role(RoleCode.EXECUTIVE)


def test_one_role_can_be_shared_by_several_users(db: Session, engineering: Department) -> None:
    employee = make_role(db, RoleCode.EMPLOYEE)
    alice = make_user(engineering, "alice@acmecorp.example")
    adam = make_user(engineering, "adam@acmecorp.example")
    alice.roles.append(employee)
    adam.roles.append(employee)
    db.add_all([alice, adam])
    db.commit()

    assert db.scalars(select(Role)).all() == [employee]
    assert alice.has_role(RoleCode.EMPLOYEE)
    assert adam.has_role(RoleCode.EMPLOYEE)


def test_giving_a_role_to_one_user_does_not_give_it_to_another(
    db: Session, engineering: Department
) -> None:
    executive = make_role(db, RoleCode.EXECUTIVE)
    alice = make_user(engineering, "alice@acmecorp.example")
    adam = make_user(engineering, "adam@acmecorp.example")
    alice.roles.append(executive)
    db.add_all([alice, adam])
    db.commit()
    db.expire_all()

    assert not adam.has_role(RoleCode.EXECUTIVE)


def test_same_role_cannot_be_assigned_twice(db: Session, engineering: Department) -> None:
    employee = make_role(db, RoleCode.EMPLOYEE)
    user = make_user(engineering)
    db.add(user)
    db.commit()

    db.execute(insert(user_roles).values(user_id=user.id, role_id=employee.id))
    with pytest.raises(IntegrityError):
        db.execute(insert(user_roles).values(user_id=user.id, role_id=employee.id))


def test_role_that_does_not_exist_cannot_be_assigned(db: Session, engineering: Department) -> None:
    user = make_user(engineering)
    db.add(user)
    db.commit()

    with pytest.raises(IntegrityError):
        db.execute(insert(user_roles).values(user_id=user.id, role_id=999))
