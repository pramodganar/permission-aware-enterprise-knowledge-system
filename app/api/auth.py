"""Logging in, and asking who you are (FR-1).

    POST /auth/login   {"email": ..., "password": ...}  ->  a bearer token
    GET  /auth/me      who the token belongs to, read from the database now

A token holds only the user's ID and when it expires (decision record 0004).
"Who am I" reads the department, roles and permissions from the database on
every call, so it shows what every other request will be decided on.

Every failed login gets the same 401 with the same message: an unknown
email, a wrong password and a deactivated account look identical. Too many
failures for one email in a short time get a 429 instead, without the
password being checked (``app/auth/throttle.py``).

The token response says it must not be cached, and a failed login logs the
reason but never the email or password that was typed.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.auth.dependencies import CurrentUser, DbSession
from app.auth.service import MAX_PASSWORD_LENGTH, authenticate
from app.auth.throttle import LoginThrottle
from app.auth.tokens import create_access_token
from app.authorization.permissions import permissions_for
from app.core.config import Settings, get_settings
from app.core.exceptions import AuthenticationError

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - the scheme's name, not a secret
    # Seconds until the token stops working.
    expires_in: int


class Department(BaseModel):
    code: str
    name: str


class Me(BaseModel):
    id: int
    email: str
    full_name: str
    department: Department
    roles: list[str]
    # What the roles allow, as the authorization service decides it.
    permissions: list[str]


def get_login_throttle(request: Request) -> LoginThrottle:
    throttle: LoginThrottle = request.app.state.login_throttle
    return throttle


@router.post("/login")
def login(
    body: LoginRequest,
    db: DbSession,
    settings: Annotated[Settings, Depends(get_settings)],
    throttle: Annotated[LoginThrottle, Depends(get_login_throttle)],
    response: Response,
) -> TokenResponse:
    throttle.check(body.email)
    try:
        user = authenticate(db, body.email, body.password)
    except AuthenticationError:
        throttle.failed(body.email)
        raise
    throttle.succeeded(body.email)

    response.headers["Cache-Control"] = "no-store"
    return TokenResponse(
        access_token=create_access_token(user.id, settings),
        expires_in=settings.access_token_expire_minutes * 60,
    )


@router.get("/me")
def me(user: CurrentUser) -> Me:
    return Me(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        department=Department(code=user.department.code, name=user.department.name),
        roles=sorted(role.code for role in user.roles),
        permissions=sorted(permission.value for permission in permissions_for(user)),
    )
