import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Department, User


def make_user(email: str, department: Department) -> User:
    return User(email=email, full_name="Test User", hashed_password="x", department=department)


def test_department_is_saved_and_read_back(db: Session) -> None:
    db.add(Department(code="finance", name="Finance"))
    db.commit()

    department = db.scalars(select(Department)).one()

    assert department.id is not None
    assert department.code == "finance"
    assert department.name == "Finance"
    assert department.created_at is not None


def test_code_is_stored_lowercase_and_trimmed(db: Session) -> None:
    department = Department(code="  HR ", name="Human Resources")
    db.add(department)
    db.commit()

    assert department.code == "hr"


def test_code_must_be_unique_ignoring_case(db: Session) -> None:
    db.add(Department(code="legal", name="Legal"))
    db.commit()

    db.add(Department(code="LEGAL", name="Legal again"))
    with pytest.raises(IntegrityError):
        db.commit()


@pytest.mark.parametrize("fields", [{"code": "sales"}, {"name": "Sales"}])
def test_code_and_name_are_required(db: Session, fields: dict[str, str]) -> None:
    db.add(Department(**fields))

    with pytest.raises(IntegrityError):
        db.commit()


def test_user_belongs_to_a_department(db: Session, engineering: Department) -> None:
    db.add(make_user("alice@acmecorp.example", engineering))
    db.commit()
    db.expire_all()

    user = db.scalars(select(User)).one()

    assert user.department.code == "engineering"
    assert user.department_id == engineering.id


def test_department_lists_its_users(db: Session, engineering: Department) -> None:
    db.add_all(
        [
            make_user("alice@acmecorp.example", engineering),
            make_user("adam@acmecorp.example", engineering),
        ]
    )
    db.commit()
    db.expire_all()

    emails = sorted(user.email for user in engineering.users)

    assert emails == ["adam@acmecorp.example", "alice@acmecorp.example"]


def test_user_cannot_point_at_a_department_that_does_not_exist(db: Session) -> None:
    db.add(
        User(
            email="ghost@acmecorp.example",
            full_name="Ghost",
            hashed_password="x",
            department_id=999,
        )
    )

    with pytest.raises(IntegrityError):
        db.commit()


def test_department_with_users_cannot_be_deleted(db: Session, engineering: Department) -> None:
    db.add(make_user("alice@acmecorp.example", engineering))
    db.commit()

    db.delete(engineering)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    assert db.scalars(select(User)).one().department_id == engineering.id


def test_empty_department_can_be_deleted(db: Session, engineering: Department) -> None:
    db.delete(engineering)
    db.commit()

    assert db.scalars(select(Department)).all() == []


def test_users_in_different_departments_are_kept_apart(
    db: Session, engineering: Department
) -> None:
    finance = Department(code="finance", name="Finance")
    db.add_all(
        [
            finance,
            make_user("alice@acmecorp.example", engineering),
            make_user("bob@acmecorp.example", finance),
        ]
    )
    db.commit()
    db.expire_all()

    assert [user.email for user in engineering.users] == ["alice@acmecorp.example"]
    assert [user.email for user in finance.users] == ["bob@acmecorp.example"]
