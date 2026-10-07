import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import User

HASH = "not-a-real-hash"


def make_user(email: str = "alice@acmecorp.example", **overrides: object) -> User:
    fields: dict[str, object] = {
        "email": email,
        "full_name": "Alice Example",
        "hashed_password": HASH,
    }
    fields.update(overrides)
    return User(**fields)


def test_user_is_saved_and_read_back(db: Session) -> None:
    db.add(make_user())
    db.commit()

    user = db.scalars(select(User)).one()

    assert user.id is not None
    assert user.email == "alice@acmecorp.example"
    assert user.full_name == "Alice Example"
    assert user.created_at is not None
    assert user.updated_at is not None


def test_new_user_is_active_by_default(db: Session) -> None:
    user = make_user()
    db.add(user)
    db.commit()

    assert user.is_active is True


def test_user_can_be_deactivated(db: Session) -> None:
    user = make_user()
    db.add(user)
    db.commit()

    user.is_active = False
    db.commit()
    db.expire_all()

    assert db.scalars(select(User)).one().is_active is False


def test_email_is_stored_lowercase_and_trimmed(db: Session) -> None:
    user = make_user("  Alice@AcmeCorp.Example ")
    db.add(user)
    db.commit()

    assert user.email == "alice@acmecorp.example"


def test_email_must_be_unique(db: Session) -> None:
    db.add(make_user())
    db.commit()

    db.add(make_user())
    with pytest.raises(IntegrityError):
        db.commit()


def test_same_email_in_different_case_is_a_duplicate(db: Session) -> None:
    db.add(make_user("alice@acmecorp.example"))
    db.commit()

    db.add(make_user("ALICE@ACMECORP.EXAMPLE"))
    with pytest.raises(IntegrityError):
        db.commit()


@pytest.mark.parametrize("field", ["email", "full_name", "hashed_password"])
def test_required_fields_cannot_be_missing(db: Session, field: str) -> None:
    fields = {
        "email": "alice@acmecorp.example",
        "full_name": "Alice Example",
        "hashed_password": HASH,
    }
    del fields[field]

    db.add(User(**fields))
    with pytest.raises(IntegrityError):
        db.commit()


def test_repr_does_not_show_the_password_hash() -> None:
    assert HASH not in repr(make_user())
