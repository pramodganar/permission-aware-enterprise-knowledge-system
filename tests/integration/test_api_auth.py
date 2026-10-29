"""Logging in and asking who you are, through the API."""

import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, event, inspect, select, text
from sqlalchemy.orm import Session

from apiclient import PASSWORD, company_client, email, headers, token
from app.api.components import Components
from app.auth.tokens import ALGORITHM
from app.core.config import Settings
from app.main import create_app
from app.models import Department, Role, RoleCode, User
from app.retrieval.index import build_index
from fakes import FakeEmbedder
from scripts.seed import seed


@pytest.fixture
def client(db: Session, engine: Engine, settings: Settings) -> Iterator[TestClient]:
    with company_client(db, engine, settings) as client:
        yield client


def login(client: TestClient, address: str, password: str = PASSWORD) -> Response:
    return client.post("/auth/login", json={"email": address, "password": password})


def without_request_id(response: Response) -> dict[str, dict[str, object]]:
    body: dict[str, dict[str, object]] = response.json()
    body["error"].pop("request_id")
    return body


# --- logging in -------------------------------------------------------------------------


def test_logging_in_gives_a_bearer_token(client: TestClient, settings: Settings) -> None:
    response = login(client, email("david"))

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == settings.access_token_expire_minutes * 60
    assert set(body) == {"access_token", "token_type", "expires_in"}


def test_the_token_response_must_not_be_cached(client: TestClient) -> None:
    assert login(client, email("david")).headers["Cache-Control"] == "no-store"


def test_the_email_is_matched_however_it_is_typed(client: TestClient) -> None:
    assert login(client, "  David@AcmeCorp.EXAMPLE ").status_code == 200


@pytest.mark.parametrize(
    ("address", "password"),
    [
        (email("david"), "wrong-password"),
        ("nobody@acmecorp.example", PASSWORD),
        (email("david"), PASSWORD + " "),
    ],
    ids=["wrong password", "unknown email", "almost right"],
)
def test_every_failed_login_looks_the_same(client: TestClient, address: str, password: str) -> None:
    reference = login(client, email("david"), "wrong-password")
    response = login(client, address, password)

    assert response.status_code == 401
    assert without_request_id(response) == without_request_id(reference)
    assert without_request_id(response) == {
        "error": {"code": "authentication_failed", "message": "Authentication failed."}
    }


def test_a_deactivated_account_can_not_log_in_and_looks_like_a_wrong_password(
    client: TestClient, db: Session
) -> None:
    frank = db.scalars(select(User).where(User.email == email("frank"))).one()
    frank.is_active = False
    db.commit()

    response = login(client, email("frank"))

    assert response.status_code == 401
    assert without_request_id(response) == without_request_id(
        login(client, email("david"), "wrong-password")
    )


def test_a_client_can_not_claim_a_role_or_department_at_login(client: TestClient) -> None:
    response = client.post(
        "/auth/login",
        json={"email": email("david"), "password": PASSWORD, "role": "executive"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["fields"] == ["body.role"]


@pytest.mark.parametrize(
    "body",
    [
        {"email": email("david")},
        {"password": PASSWORD},
        {"email": "", "password": PASSWORD},
        {"email": email("david"), "password": ""},
        {"email": email("david"), "password": "x" * 1025},
        {"email": email("david"), "password": 12345678},
    ],
)
def test_a_malformed_login_is_refused_without_echoing_it(
    client: TestClient, body: dict[str, object]
) -> None:
    response = client.post("/auth/login", json=body)

    assert response.status_code == 422
    assert PASSWORD not in response.text
    assert "x" * 50 not in response.text


def test_a_failed_login_logs_why_but_not_what_was_typed(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        login(client, email("david"), "my-guess-Summer2026")
        login(client, "nobody@acmecorp.example", "another-guess")

    reasons = [r.reason for r in caplog.records if r.message == "login failed"]  # type: ignore[attr-defined]
    assert reasons == ["wrong password", "unknown email"]
    service = "\n".join(str(vars(r)) for r in caplog.records if r.name.startswith("app."))
    assert "Summer2026" not in service
    assert "another-guess" not in service
    assert "nobody@acmecorp" not in service
    assert "david@acmecorp" not in service


# --- too many failures ------------------------------------------------------------------


def test_too_many_failures_for_one_email_are_refused_even_with_the_right_password(
    client: TestClient, settings: Settings
) -> None:
    for _ in range(settings.login_max_failures):
        assert login(client, email("david"), "guess").status_code == 401

    response = login(client, email("david"))

    assert response.status_code == 429
    assert without_request_id(response) == {
        "error": {"code": "too_many_requests", "message": "Too many attempts. Try again later."}
    }
    assert 0 < int(response.headers["Retry-After"]) <= settings.login_failure_window_seconds


def test_an_unknown_email_is_limited_exactly_like_a_real_one(
    client: TestClient, settings: Settings
) -> None:
    # Otherwise the limit would say which emails have accounts.
    for address in (email("david"), "nobody@acmecorp.example"):
        statuses = [login(client, address, "guess").status_code for _ in range(6)]
        assert statuses == [401] * settings.login_max_failures + [429]


def test_one_email_s_failures_do_not_lock_out_another(
    client: TestClient, settings: Settings
) -> None:
    for _ in range(settings.login_max_failures):
        login(client, email("david"), "guess")

    assert login(client, email("grace")).status_code == 200


def test_a_successful_login_starts_the_count_again(client: TestClient, settings: Settings) -> None:
    for _ in range(settings.login_max_failures - 1):
        login(client, email("david"), "guess")
    assert login(client, email("david")).status_code == 200

    for _ in range(settings.login_max_failures - 1):
        login(client, email("david"), "guess")

    assert login(client, email("david")).status_code == 200


def test_a_refused_attempt_does_not_check_the_password(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    for _ in range(settings.login_max_failures):
        login(client, email("david"), "guess")
    checked: list[str] = []
    from app.api import auth as auth_routes

    monkeypatch.setattr(auth_routes, "authenticate", lambda *a: checked.append("yes"))

    assert login(client, email("david")).status_code == 429
    assert checked == []


# --- who am I ---------------------------------------------------------------------------


def test_me_describes_the_person_the_token_belongs_to(client: TestClient) -> None:
    response = client.get("/auth/me", headers=headers(client, "david"))

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == email("david")
    assert body["full_name"] == "David Okafor"
    assert body["department"] == {"code": "sales", "name": "Sales"}
    assert body["roles"] == ["employee"]
    # As docs/authorization.md lists them for the employee role.
    assert body["permissions"] == [
        "documents:read:general",
        "documents:read:granted",
        "documents:read:own_department",
    ]


@pytest.mark.parametrize(
    ("name", "roles", "permissions"),
    [
        (
            "grace",
            ["executive"],
            [
                "documents:read:all_departments",
                "documents:read:general",
                "documents:read:granted",
                "documents:read:own_department",
            ],
        ),
        ("henry", ["knowledge_admin"], ["documents:manage"]),
        ("irene", ["security_reviewer"], ["audit:read"]),
    ],
)
def test_me_shows_what_each_role_allows(
    client: TestClient, name: str, roles: list[str], permissions: list[str]
) -> None:
    body = client.get("/auth/me", headers=headers(client, name)).json()

    assert body["roles"] == roles
    assert body["permissions"] == permissions


def test_me_needs_a_token(client: TestClient) -> None:
    response = client.get("/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_failed"


@pytest.mark.parametrize(
    "value", ["Bearer not-a-token", "Bearer ", "Basic ZGF2aWQ6cGFzcw==", "david"]
)
def test_me_refuses_anything_but_a_valid_token_in_one_way(client: TestClient, value: str) -> None:
    response = client.get("/auth/me", headers={"Authorization": value})

    assert response.status_code == 401
    assert without_request_id(response) == without_request_id(client.get("/auth/me"))


def test_me_follows_a_role_change_without_a_new_token(client: TestClient, db: Session) -> None:
    david_headers = headers(client, "david")
    david = db.scalars(select(User).where(User.email == email("david"))).one()
    executive = db.scalars(select(Role).where(Role.code == RoleCode.EXECUTIVE)).one()
    david.roles.append(executive)
    db.commit()

    body = client.get("/auth/me", headers=david_headers).json()

    assert "executive" in body["roles"]
    assert "documents:read:all_departments" in body["permissions"]


def test_me_follows_a_move_to_another_department(client: TestClient, db: Session) -> None:
    david_headers = headers(client, "david")
    david = db.scalars(select(User).where(User.email == email("david"))).one()
    david.department = db.scalars(select(Department).where(Department.code == "legal")).one()
    db.commit()

    assert client.get("/auth/me", headers=david_headers).json()["department"]["code"] == "legal"


def test_a_token_stops_working_when_the_account_is_deactivated(
    client: TestClient, db: Session
) -> None:
    grace_headers = headers(client, "grace")
    grace = db.scalars(select(User).where(User.email == email("grace"))).one()
    grace.is_active = False
    db.commit()

    assert client.get("/auth/me", headers=grace_headers).status_code == 401


def test_a_token_from_another_service_s_key_is_refused(
    client: TestClient, settings: Settings
) -> None:
    from pydantic import SecretStr

    from app.auth.tokens import create_access_token

    other = settings.model_copy(update={"jwt_secret_key": SecretStr("another-service-" + "k" * 40)})
    forged = create_access_token(1, other)

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {forged}"})

    assert response.status_code == 401


def test_tokens_for_two_people_are_different(client: TestClient) -> None:
    assert token(client, "david") != token(client, "grace")


# --- signing out ------------------------------------------------------------------------


def bearer(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


def test_signing_out_withdraws_the_token_at_once(client: TestClient) -> None:
    david = token(client, "david")
    assert client.get("/auth/me", headers=bearer(david)).status_code == 200

    signed_out = client.post("/auth/logout", headers=bearer(david))

    assert signed_out.status_code == 204
    assert signed_out.content == b""
    for method, path, body in [
        ("GET", "/auth/me", None),
        ("GET", "/documents", None),
        ("POST", "/search", {"question": "How many days of annual leave do I get?"}),
        ("POST", "/auth/logout", None),
    ]:
        response = client.request(method, path, headers=bearer(david), json=body)
        assert response.status_code == 401, path
        assert without_request_id(response) == {
            "error": {"code": "authentication_failed", "message": "Authentication failed."}
        }


def test_signing_out_withdraws_every_token_the_person_holds_and_nobody_elses(
    client: TestClient,
) -> None:
    laptop, phone = token(client, "david"), token(client, "david")
    emma = token(client, "emma")

    assert client.post("/auth/logout", headers=bearer(laptop)).status_code == 204

    assert client.get("/auth/me", headers=bearer(phone)).status_code == 401
    assert client.get("/auth/me", headers=bearer(emma)).status_code == 200


def test_signing_in_again_after_signing_out_gives_a_token_that_works(client: TestClient) -> None:
    for _ in range(3):
        current = token(client, "david")
        assert client.get("/auth/me", headers=bearer(current)).status_code == 200
        assert client.post("/auth/logout", headers=bearer(current)).status_code == 204
        assert client.get("/auth/me", headers=bearer(current)).status_code == 401


def test_signing_out_needs_a_token_that_still_works(client: TestClient, db: Session) -> None:
    assert client.post("/auth/logout").status_code == 401
    assert client.post("/auth/logout", headers=bearer("not-a-token")).status_code == 401

    david = token(client, "david")
    client.post("/auth/logout", headers=bearer(david))
    client.post("/auth/logout", headers=bearer(david))

    db.expire_all()
    user = db.scalars(select(User).where(User.email == email("david"))).one()
    # The second, with a token already withdrawn, changed nothing.
    assert user.token_version == 1


def test_the_version_comes_with_the_user_and_needs_no_query_of_its_own(
    client: TestClient, engine: Engine
) -> None:
    david = bearer(token(client, "david"))
    statements: list[str] = []

    def ran(*args: object) -> None:
        statements.append(str(args[2]))

    event.listen(engine, "before_cursor_execute", ran)
    try:
        assert client.get("/auth/me", headers=david).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", ran)

    versions = [s for s in statements if "token_versions" in s]
    assert len(versions) == 1
    assert "FROM users" in versions[0]


def test_a_database_from_before_token_versions_keeps_everyone_signed_in(
    db: Session, engine: Engine, settings: Settings
) -> None:
    seed(db, settings, PASSWORD)
    embedder = FakeEmbedder()
    components = Components(embedder=embedder, index=build_index(db, embedder), provider=None)
    # A token issued before versions existed: it has none.
    david = db.scalars(select(User).where(User.email == email("david"))).one()
    secret = settings.jwt_secret_key.get_secret_value()
    now = datetime.now(UTC)
    old = jwt.encode(
        {"sub": str(david.id), "iat": now, "exp": now + timedelta(minutes=5)},
        secret,
        algorithm=ALGORITHM,
    )
    db.execute(text("DROP TABLE token_versions"))
    db.commit()
    db.expire_all()

    with TestClient(create_app(settings, engine=engine, components=components)) as client:
        assert "token_versions" in inspect(engine).get_table_names()
        assert client.get("/auth/me", headers=bearer(old)).status_code == 200
        assert client.post("/auth/logout", headers=bearer(old)).status_code == 204
        assert client.get("/auth/me", headers=bearer(old)).status_code == 401
