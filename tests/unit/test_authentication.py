import logging

import pytest
from sqlalchemy.orm import Session

from app.auth import service
from app.auth.passwords import hash_password
from app.auth.service import MAX_PASSWORD_LENGTH, authenticate
from app.core.exceptions import AuthenticationError, error_response
from app.models import Department, User

EMAIL = "alice@acmecorp.example"
PASSWORD = "alice-test-password"


@pytest.fixture
def alice(db: Session, engineering: Department) -> User:
    user = User(
        email=EMAIL,
        full_name="Alice Example",
        hashed_password=hash_password(PASSWORD),
        department=engineering,
    )
    db.add(user)
    db.commit()
    return user


def test_correct_credentials_return_the_user(db: Session, alice: User) -> None:
    assert authenticate(db, EMAIL, PASSWORD).id == alice.id


@pytest.mark.parametrize("email", ["ALICE@ACMECORP.EXAMPLE", "  alice@acmecorp.example  "])
def test_email_is_matched_without_case_or_spaces(db: Session, alice: User, email: str) -> None:
    assert authenticate(db, email, PASSWORD).id == alice.id


@pytest.mark.parametrize("password", ["", "wrong", PASSWORD.upper(), PASSWORD + " "])
def test_wrong_password_fails(db: Session, alice: User, password: str) -> None:
    with pytest.raises(AuthenticationError):
        authenticate(db, EMAIL, password)


def test_unknown_email_fails(db: Session, alice: User) -> None:
    with pytest.raises(AuthenticationError):
        authenticate(db, "mallory@acmecorp.example", PASSWORD)


def test_inactive_user_fails_even_with_the_right_password(db: Session, alice: User) -> None:
    alice.is_active = False
    db.commit()

    with pytest.raises(AuthenticationError):
        authenticate(db, EMAIL, PASSWORD)


def test_reactivated_user_can_log_in_again(db: Session, alice: User) -> None:
    alice.is_active = False
    db.commit()
    alice.is_active = True
    db.commit()

    assert authenticate(db, EMAIL, PASSWORD).id == alice.id


def test_overlong_password_fails_without_being_hashed(
    db: Session, alice: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_if_called(password: str, hashed_password: str) -> bool:
        raise AssertionError("an overlong password must not reach the hasher")

    monkeypatch.setattr(service, "verify_password", fail_if_called)

    with pytest.raises(AuthenticationError):
        authenticate(db, EMAIL, "x" * (MAX_PASSWORD_LENGTH + 1))


def test_every_failure_gives_the_client_the_same_response(db: Session, alice: User) -> None:
    def response_for(email: str, password: str) -> object:
        with pytest.raises(AuthenticationError) as error:
            authenticate(db, email, password)
        return error_response(error.value)

    wrong_password = response_for(EMAIL, "wrong")
    unknown_email = response_for("mallory@acmecorp.example", PASSWORD)
    alice.is_active = False
    db.commit()
    inactive = response_for(EMAIL, PASSWORD)

    assert wrong_password == unknown_email == inactive


def test_unknown_email_still_does_a_password_check(
    db: Session, alice: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def record(password: str, hashed_password: str) -> bool:
        calls.append(hashed_password)
        return False

    monkeypatch.setattr(service, "verify_password", record)

    with pytest.raises(AuthenticationError):
        authenticate(db, "mallory@acmecorp.example", PASSWORD)

    assert len(calls) == 1
    assert calls[0] != alice.hashed_password


def test_failed_login_is_logged_with_the_reason_but_not_what_was_typed(
    db: Session, alice: User, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="app.auth.service")

    with pytest.raises(AuthenticationError):
        authenticate(db, "typed-my-password-here@acmecorp.example", "secret-attempt")
    with pytest.raises(AuthenticationError):
        authenticate(db, EMAIL, "secret-attempt")

    reasons = [getattr(record, "reason", None) for record in caplog.records]
    logged = " ".join(str(vars(record)) for record in caplog.records)

    assert reasons == ["unknown email", "wrong password"]
    assert caplog.records[1].__dict__["user_id"] == alice.id
    assert "secret-attempt" not in logged
    assert "typed-my-password-here" not in logged


def test_successful_login_is_logged_with_the_user_id(
    db: Session, alice: User, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="app.auth.service")

    authenticate(db, EMAIL, PASSWORD)

    assert caplog.records[-1].getMessage() == "login succeeded"
    assert caplog.records[-1].__dict__["user_id"] == alice.id
