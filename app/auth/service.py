"""Checking an email and password against the users table."""

import logging
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.passwords import hash_password, verify_password
from app.core.exceptions import AuthenticationError
from app.models import User

logger = logging.getLogger(__name__)

# Longer inputs are rejected before hashing, so a huge "password" can't be
# used to keep the server busy.
MAX_PASSWORD_LENGTH = 1024

# Verified against when the email is unknown, so that case takes about as long
# as a wrong password and can't be told apart by timing.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(32))


def authenticate(db: Session, email: str, password: str) -> User:
    """Return the user for these credentials, or raise AuthenticationError.

    Every failure raises the same error, so a caller can't learn whether the
    email exists, the password was wrong or the account is deactivated. The
    reason is logged, without the email or password that was typed.
    """
    if len(password) > MAX_PASSWORD_LENGTH:
        raise _failed("password too long")

    user = db.scalars(select(User).where(User.email == email.strip().lower())).one_or_none()

    if user is None:
        verify_password(password, _DUMMY_HASH)
        raise _failed("unknown email")
    if not verify_password(password, user.hashed_password):
        raise _failed("wrong password", user.id)
    if not user.is_active:
        raise _failed("inactive account", user.id)

    logger.info("login succeeded", extra={"user_id": user.id})
    return user


def _failed(reason: str, user_id: int | None = None) -> AuthenticationError:
    logger.warning("login failed", extra={"reason": reason, "user_id": user_id})
    return AuthenticationError(reason, user_id=user_id)
