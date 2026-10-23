"""Working out who is calling: from an email and password, or from a token."""

import logging
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.trail import write
from app.auth.passwords import hash_password, verify_password
from app.auth.tokens import decode_access_token
from app.core.config import Settings
from app.core.exceptions import AuthenticationError
from app.models import AuditAction, User

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
    reason is logged and audited, without the email or password that was
    typed. A login that succeeds is audited too: it is where looking into a
    stolen password starts. If the audit record can't be stored, this raises AuditWriteError
    instead (decision record 0006).
    """
    if len(password) > MAX_PASSWORD_LENGTH:
        raise _failed(db, "password too long")

    user = db.scalars(select(User).where(User.email == email.strip().lower())).one_or_none()

    if user is None:
        verify_password(password, _DUMMY_HASH)
        raise _failed(db, "unknown email")
    if not verify_password(password, user.hashed_password):
        raise _failed(db, "wrong password", user.id)
    if not user.is_active:
        raise _failed(db, "inactive account", user.id)

    write(db, AuditAction.LOGIN_SUCCEEDED, "allowed", actor_id=user.id)
    logger.info("login succeeded", extra={"user_id": user.id})
    return user


def record_throttled(db: Session, email: str) -> None:
    """Audit a login refused for too many failures, before its password was looked at."""
    user_id = db.scalar(select(User.id).where(User.email == email.strip().lower()))
    _failed(db, "too many attempts", user_id)


def user_from_token(db: Session, token: str, settings: Settings) -> User:
    """Return the active user a token belongs to, or raise AuthenticationError.

    The user is loaded from the database every time. A token that is still
    valid stops working as soon as its user is deactivated, and the roles and
    department on the returned user are the current ones.
    """
    user_id = decode_access_token(token, settings)
    user = db.get(User, user_id)

    if user is None:
        raise AuthenticationError("token for a user that does not exist", user_id=user_id)
    if not user.is_active:
        raise AuthenticationError("token for an inactive account", user_id=user_id)
    return user


def _failed(db: Session, reason: str, user_id: int | None = None) -> AuthenticationError:
    logger.warning("login failed", extra={"reason": reason, "user_id": user_id})
    write(db, AuditAction.LOGIN_FAILED, "failed", actor_id=user_id, details={"reason": reason})
    return AuthenticationError(reason, user_id=user_id)
