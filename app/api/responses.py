"""What a route can answer besides success, for the service's own description of itself.

FastAPI describes every route as answering its success status or a 422 in
FastAPI's own shape, which this service never sends. Each route says here
instead which errors it can give, and they are described with the body the
service really sends (``app/api/errors.py``):

    {"error": {"code": "...", "message": "...", "request_id": "..."}}

A test makes every route give every error it declares, so the description
can't promise a status that never comes or leave out one that does
(``tests/integration/test_api_responses.py``). Errors any route can give are
left out: 405, 411 and 413 from the request's shape, and 500.
"""

from typing import Any

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    code: str = Field(examples=["not_found"])
    message: str = Field(examples=["Not found."])
    request_id: str = Field(examples=["6f1c2a9e4b7d4e0c9a1b3d5f7e2c4a68"])
    fields: list[str] | None = Field(
        default=None,
        description="Only for a request that doesn't match the route: where, never what was sent.",
        examples=[["body.question"]],
    )


class ErrorBody(BaseModel):
    error: ErrorDetail


WHY = {
    401: (
        "No token, or one that is invalid, expired or withdrawn by signing out, or an account "
        "that is no longer active. The same body for every one."
    ),
    403: (
        "The caller's roles don't give the permission this route needs. Recorded in the "
        "audit trail."
    ),
    404: "No document with this ID, or one the caller may not read: the same response for both.",
    422: (
        "The body, a parameter or the path isn't what the route takes, or breaks one of its rules. "
        "`fields` says where, and nothing that was sent is repeated."
    ),
    429: "Too many calls. `Retry-After` says how many seconds to wait.",
    503: "The request, or its refusal, couldn't be recorded in the audit trail, so it wasn't done.",
}


def errors(*statuses: int, **why: str) -> dict[int | str, dict[str, Any]]:
    """The responses to declare for these statuses. ``e401="..."`` replaces one's description."""
    return {
        status: {"model": ErrorBody, "description": why.get(f"e{status}", WHY[status])}
        for status in statuses
    }
