"""Health check, for a load balancer or a container orchestrator.

It needs no token, so it says nothing but whether the service can answer:
no version, no model name, no counts. A failed database check is a 503 with
the usual fixed message.
"""

from fastapi import APIRouter
from sqlalchemy import text

from app.auth.dependencies import DbSession
from app.core.exceptions import ServiceUnavailableError

router = APIRouter(tags=["health"])


@router.get("/health")
def health(db: DbSession) -> dict[str, str]:
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        raise ServiceUnavailableError("the database did not answer the health check") from None
    return {"status": "ok"}
