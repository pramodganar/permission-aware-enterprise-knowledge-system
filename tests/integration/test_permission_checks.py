"""Permission checks, exercised through real HTTP requests."""

import logging
from datetime import timedelta
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from acmecorp import World
from app.auth.dependencies import CurrentUser, DbSession
from app.auth.tokens import create_access_token
from app.authorization.checks import get_readable_document
from app.authorization.dependencies import require_permission
from app.authorization.permissions import Permission
from app.core.config import Settings, get_settings
from app.core.database import create_session_factory, utc_now
from app.core.exceptions import AppError, error_response
from app.models import User

CanManage = Annotated[User, Depends(require_permission(Permission.MANAGE_DOCUMENTS))]
CanReadAudit = Annotated[User, Depends(require_permission(Permission.READ_AUDIT_LOG))]

NOT_FOUND = {"error": {"code": "not_found", "message": "Not found."}}
DENIED = {
    "error": {
        "code": "permission_denied",
        "message": "You are not allowed to perform this action.",
    }
}


@pytest.fixture
def client(engine: Engine, settings: Settings) -> TestClient:
    app = FastAPI()
    app.state.session_factory = create_session_factory(engine)
    app.dependency_overrides[get_settings] = lambda: settings

    @app.exception_handler(AppError)
    def handle_app_error(_: Request, error: AppError) -> JSONResponse:
        status_code, body = error_response(error)
        return JSONResponse(body, status_code=status_code)

    @app.post("/documents")
    def ingest(user: CanManage) -> dict[str, object]:
        return {"ingested_by": user.id}

    @app.get("/audit")
    def audit(user: CanReadAudit) -> dict[str, object]:
        return {"read_by": user.id}

    @app.get("/documents/{document_id}")
    def read_document(document_id: int, user: CurrentUser, db: DbSession) -> dict[str, object]:
        document = get_readable_document(db, user, document_id)
        return {"id": document.id, "title": document.title}

    return TestClient(app)


@pytest.fixture
def as_user(world: World, settings: Settings) -> dict[str, dict[str, str]]:
    """Authorization headers for each user in the test company."""
    return {
        name: {"Authorization": f"Bearer {create_access_token(user.id, settings)}"}
        for name, user in world.users.items()
    }


Headers = dict[str, dict[str, str]]


# Actions: 403 when the permission is missing


def test_knowledge_admin_can_manage_documents(client: TestClient, as_user: Headers) -> None:
    assert client.post("/documents", headers=as_user["admin"]).status_code == 200


@pytest.mark.parametrize("user", ["alice", "carol", "grace", "reviewer", "nobody"])
def test_others_cannot_manage_documents(client: TestClient, as_user: Headers, user: str) -> None:
    response = client.post("/documents", headers=as_user[user])

    assert response.status_code == 403
    assert response.json() == DENIED


def test_security_reviewer_can_read_the_audit_log(client: TestClient, as_user: Headers) -> None:
    assert client.get("/audit", headers=as_user["reviewer"]).status_code == 200


@pytest.mark.parametrize("user", ["alice", "carol", "grace", "admin", "nobody"])
def test_others_cannot_read_the_audit_log(client: TestClient, as_user: Headers, user: str) -> None:
    response = client.get("/audit", headers=as_user[user])

    assert response.status_code == 403
    assert response.json() == DENIED


def test_denied_response_does_not_name_the_missing_permission(
    client: TestClient, as_user: Headers
) -> None:
    body = client.post("/documents", headers=as_user["alice"]).text

    assert "manage" not in body
    assert "documents:" not in body


@pytest.mark.parametrize("path", ["/documents", "/audit"])
def test_missing_token_is_401_not_403(client: TestClient, world: World, path: str) -> None:
    response = client.post(path) if path == "/documents" else client.get(path)

    assert response.status_code == 401


def test_deactivated_admin_is_rejected_before_the_permission_check(
    client: TestClient, world: World, as_user: Headers
) -> None:
    world.users["admin"].is_active = False
    world.db.commit()

    assert client.post("/documents", headers=as_user["admin"]).status_code == 401


def test_removing_the_role_removes_the_action_on_the_next_request(
    client: TestClient, world: World, as_user: Headers
) -> None:
    assert client.post("/documents", headers=as_user["admin"]).status_code == 200

    world.users["admin"].roles = []
    world.db.commit()

    assert client.post("/documents", headers=as_user["admin"]).status_code == 403


def test_denied_action_is_logged_with_user_and_permission(
    client: TestClient, world: World, as_user: Headers, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="app.authorization.checks")

    client.post("/documents", headers=as_user["alice"])

    record = caplog.records[-1]
    assert record.getMessage() == "action denied"
    assert record.__dict__["user_id"] == world.users["alice"].id
    assert record.__dict__["permission"] == "documents:manage"


# Documents: 404 when the user may not read it


def test_user_can_fetch_a_document_they_may_read(
    client: TestClient, world: World, as_user: Headers
) -> None:
    runbook = world.documents["runbook"]

    response = client.get(f"/documents/{runbook.id}", headers=as_user["alice"])

    assert response.status_code == 200
    assert response.json() == {"id": runbook.id, "title": "Deployment Runbook"}


@pytest.mark.parametrize(
    ("user", "document"),
    [
        ("alice", "salary_bands"),
        ("alice", "exec_comp"),
        ("alice", "case_file"),
        ("carol", "runbook"),
        ("carol", "exec_comp"),
        ("carol", "case_file"),
        ("grace", "case_file"),
        ("admin", "runbook"),
        ("admin", "leave_policy"),
        ("reviewer", "salary_bands"),
        ("nobody", "leave_policy"),
    ],
)
def test_document_the_user_may_not_read_is_not_found(
    client: TestClient, world: World, as_user: Headers, user: str, document: str
) -> None:
    response = client.get(f"/documents/{world.documents[document].id}", headers=as_user[user])

    assert response.status_code == 404
    assert response.json() == NOT_FOUND


def test_hidden_document_and_missing_document_are_indistinguishable(
    client: TestClient, world: World, as_user: Headers
) -> None:
    hidden = client.get(f"/documents/{world.documents['exec_comp'].id}", headers=as_user["alice"])
    missing = client.get("/documents/999999", headers=as_user["alice"])

    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json()
    assert hidden.headers.keys() == missing.headers.keys()
    assert "Executive" not in hidden.text


def test_grant_makes_a_restricted_document_fetchable_until_it_expires(
    client: TestClient, world: World, as_user: Headers
) -> None:
    url = f"/documents/{world.documents['case_file'].id}"
    assert client.get(url, headers=as_user["carol"]).status_code == 404

    world.grant("case_file", "carol", expires_at=utc_now() + timedelta(hours=1))
    assert client.get(url, headers=as_user["carol"]).status_code == 200
    assert client.get(url, headers=as_user["grace"]).status_code == 404

    world.documents["case_file"].grants[0].expires_at = utc_now() - timedelta(seconds=1)
    world.db.commit()
    assert client.get(url, headers=as_user["carol"]).status_code == 404


def test_a_denied_document_and_a_missing_one_each_leave_one_line_that_tells_them_apart(
    client: TestClient, world: World, as_user: Headers, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="app.authorization.checks")
    exec_comp = world.documents["exec_comp"]

    client.get("/documents/999999", headers=as_user["alice"])
    client.get(f"/documents/{exec_comp.id}", headers=as_user["alice"])

    missing, denied = caplog.records
    assert missing.getMessage() == denied.getMessage() == "document not returned"
    assert (missing.__dict__["document_id"], missing.__dict__["exists"]) == (999999, False)
    assert (denied.__dict__["document_id"], denied.__dict__["exists"]) == (exec_comp.id, True)
    assert denied.__dict__["user_id"] == world.users["alice"].id
    assert "Executive" not in "".join(str(vars(record)) for record in caplog.records)


def test_unauthenticated_document_request_is_401(client: TestClient, world: World) -> None:
    assert client.get(f"/documents/{world.documents['leave_policy'].id}").status_code == 401
