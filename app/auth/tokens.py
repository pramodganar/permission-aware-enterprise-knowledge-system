"""Access tokens.

A token says who the user is and when it expires, nothing else. Department,
roles and whether the account is active are read from the database on every
request, so changing them takes effect immediately.
"""

from datetime import datetime, timedelta

import jwt

from app.core.config import Settings
from app.core.database import utc_now
from app.core.exceptions import AuthenticationError

# Fixed here and not configurable. The token's own header is never trusted to
# say which algorithm to verify with.
ALGORITHM = "HS256"
REQUIRED_CLAIMS = ("sub", "iat", "exp")


def create_access_token(user_id: int, settings: Settings, now: datetime | None = None) -> str:
    issued_at = now or utc_now()
    claims = {
        "sub": str(user_id),
        "iat": issued_at,
        "exp": issued_at + timedelta(minutes=settings.access_token_expire_minutes),
    }
    return jwt.encode(claims, settings.jwt_secret_key.get_secret_value(), algorithm=ALGORITHM)


def decode_access_token(token: str, settings: Settings) -> int:
    """Return the user ID from a valid token, or raise AuthenticationError."""
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret_key.get_secret_value(),
            algorithms=[ALGORITHM],
            options={"require": list(REQUIRED_CLAIMS)},
        )
        return int(claims["sub"])
    except (jwt.PyJWTError, ValueError) as error:
        raise AuthenticationError(f"invalid token: {type(error).__name__}") from error
