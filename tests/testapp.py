"""A small API over the real auth and authorization code, for tests.

The production routes don't exist yet. These endpoints do nothing except call
the dependencies and the authorization service, so tests can attack them the
way a client would.
"""

from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Engine

from app.auth.dependencies import CurrentUser, DbSession
from app.authorization.checks import get_readable_document
from app.authorization.dependencies import require_permission
from app.authorization.permissions import Permission
from app.authorization.service import readable_documents
from app.core.config import Settings, get_settings
from app.core.database import create_session_factory
from app.core.exceptions import AppError, error_response
from app.models import User

CanManage = Annotated[User, Depends(require_permission(Permission.MANAGE_DOCUMENTS))]
CanReadAudit = Annotated[User, Depends(require_permission(Permission.READ_AUDIT_LOG))]


def build_test_app(engine: Engine, settings: Settings) -> FastAPI:
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
            "department": user.department.code,
            "roles": sorted(role.code for role in user.roles),
        }

    @app.get("/documents")
    def list_documents(user: CurrentUser, db: DbSession) -> list[dict[str, object]]:
        documents = db.scalars(readable_documents(user)).all()
        return [{"id": document.id, "title": document.title} for document in documents]

    @app.get("/documents/{document_id}")
    def read_document(document_id: int, user: CurrentUser, db: DbSession) -> dict[str, object]:
        document = get_readable_document(db, user, document_id)
        return {"id": document.id, "title": document.title}

    @app.post("/documents")
    def ingest(user: CanManage) -> dict[str, object]:
        return {"ingested_by": user.id}

    @app.get("/audit")
    def audit(user: CanReadAudit) -> dict[str, object]:
        return {"read_by": user.id}

    return app
