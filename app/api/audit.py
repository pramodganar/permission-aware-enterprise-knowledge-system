"""Reading the audit trail (FR-14). Needs the audit:read permission.

    GET /audit   ?actor=david@acmecorp.example&action=question.asked
                 &document_id=3f0c9a52-8d1e-4b7a-9c61-2e5d8f4a7b10
                 &since=2026-10-01T00:00:00Z&until=...&limit=100&offset=0

Newest first. The records hold the questions people asked and which chunks
they were given, so only a security reviewer may read them, and every read
is itself recorded. Anyone else gets 403.

Times must include a timezone, like every timestamp in the service.

Documents are named by their public IDs, the ones readers and admins are
given. A record's details are as they were written: records from before
public IDs name documents by the service's own numbers.
"""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import AwareDatetime, BaseModel

from app.audit.trail import AuditQuery, read_trail
from app.auth.dependencies import DbSession
from app.authorization.dependencies import require_permission
from app.authorization.permissions import Permission
from app.models import AuditAction, User, public_ids

router = APIRouter(prefix="/audit", tags=["audit"])

CanReadAudit = Annotated[User, Depends(require_permission(Permission.READ_AUDIT_LOG))]
MAX_PAGE = 500
MAX_OFFSET = 10_000_000


class Actor(BaseModel):
    id: int
    email: str


class AuditEntry(BaseModel):
    id: int
    created_at: datetime
    request_id: str | None
    # None only for a failed login with an email no account has.
    actor: Actor | None
    action: str
    outcome: str
    document_id: UUID | None
    question: str | None
    chunk_ids: list[int]
    details: dict[str, Any]


class AuditList(BaseModel):
    total: int
    records: list[AuditEntry]


@router.get("")
def read_audit_trail(
    reviewer: CanReadAudit,
    db: DbSession,
    response: Response,
    actor: Annotated[str | None, Query(max_length=255)] = None,
    action: AuditAction | None = None,
    document_id: UUID | None = None,
    since: AwareDatetime | None = None,
    until: AwareDatetime | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 100,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> AuditList:
    page = read_trail(
        db,
        reviewer,
        AuditQuery(
            actor_email=actor,
            action=action,
            document_id=document_id,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        ),
    )

    named = public_ids(db, (entry.document_id for entry in page.records))
    response.headers["Cache-Control"] = "no-store"
    return AuditList(
        total=page.total,
        records=[
            AuditEntry(
                id=entry.id,
                created_at=entry.created_at,
                request_id=entry.request_id,
                actor=None
                if entry.actor_id is None
                else Actor(id=entry.actor_id, email=page.emails.get(entry.actor_id, "")),
                action=entry.action,
                outcome=entry.outcome,
                document_id=None if entry.document_id is None else named[entry.document_id],
                question=entry.question,
                chunk_ids=entry.chunk_ids,
                details=entry.details,
            )
            for entry in page.records
        ],
    )
