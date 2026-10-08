"""The current-user dependency, exercised through real HTTP requests."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.auth.dependencies import CurrentUser
from app.auth.tokens import create_access_token
from app.core.config import Settings, get_settings
from app.core.database import create_session_factory
from app.core.exceptions import AppError, error_response
from app.models import Department, Role, RoleCode, User


@pytest.fixture
def client(engine: Engine, settings: Settings) -> TestClient:
    app = FastAPI()
    app.state.session_factory = create_session_factory(engine)
    app.dependency_overrides[get_settings] = lambda: settings

    @app.exception_handler(AppError)
    def handle_app_error(_: Request, error: AppError) -> JSONResponse:
        status_code, body = error_response(error)
        return JSONResponse(body, status_code=status_code)

    @app.get("/whoami")
    def whoami(user: CurrentUser) -> dict[str, object]:
        return {
            "id": user.id,
            "email": user.email,
            "department": user.department.code,
            "roles": sorted(role.code for role in user.roles),
        }

    return TestClient(app)


@pytest.fixture
def alice(db: Session, engineering: Department) -> User:
    employee = Role(code=RoleCode.EMPLOYEE)
    db.add(employee)
    db.commit()
    user = User(
        email="alice@acmecorp.example",
        full_name="Alice Example",
        hashed_password="x",
        department=engineering,
        roles=[employee],
    )
    db.add(user)
    db.commit()
    return user


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_valid_token_identifies_the_user(
    client: TestClient, alice: User, settings: Settings
) -> None:
    response = client.get("/whoami", headers=bearer(create_access_token(alice.id, settings)))

    assert response.status_code == 200
    assert response.json() == {
        "id": alice.id,
        "email": "alice@acmecorp.example",
        "department": "engineering",
        "roles": ["employee"],
    }


def test_request_without_a_token_is_rejected(client: TestClient, alice: User) -> None:
    assert client.get("/whoami").status_code == 401


@pytest.mark.parametrize(
    "header",
    ["Basic YWxpY2U6cGFzc3dvcmQ=", "Bearer", "Bearer not-a-token", "Token abc", "abc"],
)
def test_malformed_authorization_header_is_rejected(
    client: TestClient, alice: User, header: str
) -> None:
    assert client.get("/whoami", headers={"Authorization": header}).status_code == 401


def test_token_in_the_query_string_is_not_accepted(
    client: TestClient, alice: User, settings: Settings
) -> None:
    token = create_access_token(alice.id, settings)

    assert client.get("/whoami", params={"token": token, "access_token": token}).status_code == 401


def test_expired_token_is_rejected(client: TestClient, alice: User, settings: Settings) -> None:
    token = create_access_token(alice.id, settings, now=datetime.now(UTC) - timedelta(days=1))

    assert client.get("/whoami", headers=bearer(token)).status_code == 401


def test_token_for_a_user_that_does_not_exist_is_rejected(
    client: TestClient, alice: User, settings: Settings
) -> None:
    token = create_access_token(alice.id + 1000, settings)

    assert client.get("/whoami", headers=bearer(token)).status_code == 401


def test_deactivated_user_is_cut_off_while_the_token_is_still_valid(
    client: TestClient, db: Session, alice: User, settings: Settings
) -> None:
    headers = bearer(create_access_token(alice.id, settings))
    assert client.get("/whoami", headers=headers).status_code == 200

    alice.is_active = False
    db.commit()

    assert client.get("/whoami", headers=headers).status_code == 401


def test_role_change_applies_on_the_next_request_with_the_same_token(
    client: TestClient, db: Session, alice: User, settings: Settings
) -> None:
    headers = bearer(create_access_token(alice.id, settings))
    executive = Role(code=RoleCode.EXECUTIVE)
    db.add(executive)
    db.commit()

    alice.roles.append(executive)
    db.commit()
    assert client.get("/whoami", headers=headers).json()["roles"] == ["employee", "executive"]

    alice.roles.remove(executive)
    db.commit()
    assert client.get("/whoami", headers=headers).json()["roles"] == ["employee"]


def test_department_change_applies_on_the_next_request_with_the_same_token(
    client: TestClient, db: Session, alice: User, settings: Settings
) -> None:
    headers = bearer(create_access_token(alice.id, settings))
    finance = Department(code="finance", name="Finance")
    db.add(finance)
    db.commit()

    alice.department = finance
    db.commit()

    assert client.get("/whoami", headers=headers).json()["department"] == "finance"


def test_identity_claimed_by_the_client_is_ignored(
    client: TestClient, alice: User, settings: Settings
) -> None:
    response = client.get(
        "/whoami",
        params={"department": "executive", "role": "executive", "user_id": 1},
        headers={
            **bearer(create_access_token(alice.id, settings)),
            "X-Department": "executive",
            "X-Role": "executive",
            "X-User-Id": "1",
        },
    )

    assert response.json()["department"] == "engineering"
    assert response.json()["roles"] == ["employee"]
    assert response.json()["id"] == alice.id


def test_every_rejection_looks_the_same(
    client: TestClient, db: Session, alice: User, settings: Settings
) -> None:
    valid = create_access_token(alice.id, settings)
    expired = create_access_token(alice.id, settings, now=datetime.now(UTC) - timedelta(days=1))
    unknown_user = create_access_token(alice.id + 1000, settings)

    responses = [
        client.get("/whoami"),
        client.get("/whoami", headers=bearer("garbage")),
        client.get("/whoami", headers=bearer(expired)),
        client.get("/whoami", headers=bearer(unknown_user)),
    ]
    alice.is_active = False
    db.commit()
    responses.append(client.get("/whoami", headers=bearer(valid)))

    assert {response.status_code for response in responses} == {401}
    assert all(response.json() == responses[0].json() for response in responses)
    assert responses[0].json() == {
        "error": {"code": "authentication_failed", "message": "Authentication failed."}
    }
