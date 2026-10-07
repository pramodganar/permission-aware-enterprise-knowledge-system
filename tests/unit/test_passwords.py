import pytest

from app.auth.passwords import hash_password, verify_password

PASSWORD = "correct horse battery staple"


def test_hash_is_not_the_password() -> None:
    hashed = hash_password(PASSWORD)

    assert PASSWORD not in hashed
    assert hashed.startswith("$argon2id$")


def test_same_password_hashes_differently_each_time() -> None:
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


def test_correct_password_verifies() -> None:
    assert verify_password(PASSWORD, hash_password(PASSWORD)) is True


@pytest.mark.parametrize("attempt", ["", "wrong", PASSWORD.upper(), PASSWORD + " "])
def test_wrong_password_does_not_verify(attempt: str) -> None:
    assert verify_password(attempt, hash_password(PASSWORD)) is False


@pytest.mark.parametrize("stored", ["", "not-a-hash", PASSWORD, "$argon2id$broken"])
def test_malformed_stored_hash_does_not_verify_or_raise(stored: str) -> None:
    assert verify_password(PASSWORD, stored) is False
