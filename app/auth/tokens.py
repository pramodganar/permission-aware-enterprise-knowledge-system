"""Access tokens.

A token says who the user is and when it expires, nothing else. Department,
roles and whether the account is active are read from the database on every
request, so changing them takes effect immediately.

It also carries the user's token version when it was issued (``ver``).
Signing out moves the user's version on, which is how a token is withdrawn
before it expires (decision record 0015). A token from before versions
existed has none, and counts as version 0.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import jwt

from app.core.config import Settings
from app.core.database import utc_now
from app.core.exceptions import AuthenticationError

# Fixed here and not configurable. The token's own header is never trusted to
# say which algorithm to verify with.
ALGORITHM = "HS256"
REQUIRED_CLAIMS = ("sub", "iat", "exp")


@dataclass(frozen=True)
class TokenClaims:
    user_id: int
    # The user's token version when the token was issued.
    version: int


def create_access_token(
    user_id: int, settings: Settings, now: datetime | None = None, version: int = 0
) -> str:
    issued_at = now or utc_now()
    claims = {
        "sub": str(user_id),
        "ver": version,
        "iat": issued_at,
        "exp": issued_at + timedelta(minutes=settings.access_token_expire_minutes),
    }
    return jwt.encode(claims, settings.jwt_secret_key.get_secret_value(), algorithm=ALGORITHM)


def decode_access_token(token: str, settings: Settings) -> TokenClaims:
    """Return who a valid token is for and its version, or raise AuthenticationError."""
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret_key.get_secret_value(),
            algorithms=[ALGORITHM],
            options={"require": list(REQUIRED_CLAIMS)},
        )
        version = claims.get("ver", 0)
        # A whole number only: True, 1.0 or "1" isn't a version.
        if type(version) is not int or version < 0:
            raise ValueError("ver")
        return TokenClaims(user_id=int(claims["sub"]), version=version)
    except (jwt.PyJWTError, ValueError) as error:
        raise AuthenticationError(f"invalid token: {type(error).__name__}") from error
