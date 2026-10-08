"""FastAPI dependencies for the database session and the current user.

An endpoint that needs to know who is calling declares a ``CurrentUser``
parameter. That is the only way an endpoint learns the caller's identity.
Department and roles are never taken from the request.
"""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.auth.service import user_from_token
from app.core.config import Settings, get_settings
from app.core.exceptions import AuthenticationError
from app.models import User

# auto_error=False so a missing header raises our own error, with the same
# response as any other authentication failure.
_bearer = HTTPBearer(auto_error=False)


def get_db(request: Request) -> Iterator[Session]:
    """One database session per request, closed when the request ends."""
    with request.app.state.session_factory() as session:
        yield session


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> User:
    if credentials is None:
        raise AuthenticationError("no bearer token in the request")
    return user_from_token(db, credentials.credentials, settings)


DbSession = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]
