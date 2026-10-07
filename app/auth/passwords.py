"""Password hashing with Argon2id."""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Return a salted hash. The same password gives a different hash each time."""
    return _hasher.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    """Check a password against a stored hash. Never raises."""
    try:
        return _hasher.verify(hashed_password, password)
    except (VerificationError, InvalidHashError):
        return False
