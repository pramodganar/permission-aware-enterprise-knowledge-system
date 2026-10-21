"""How errors become responses.

Every error response has the same shape, from ``error_response``:

    {"error": {"code": "...", "message": "...", "request_id": "..."}}

Three kinds of error reach a client:

- The service's own errors (``AppError``): a fixed public message per type,
  so a response can't say which document, user or rule was involved.
- Requests FastAPI rejects before an endpoint runs: a body that doesn't
  match its schema, an unknown route, the wrong method. FastAPI's default
  answer to a bad body includes the input itself, which here can be a
  password or a question. This one names the fields that are wrong and
  never what was in them.
- Anything else: a fixed "something went wrong" with the request ID, so the
  log can be found. That is handled in the request middleware
  (``app/api/middleware.py``), which is the only place that still has the
  request ID when an unexpected error escapes.
"""

import re
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.core.exceptions import (
    AppError,
    InvalidInputError,
    NotFoundError,
    TooManyRequestsError,
    error_response,
)
from app.core.logging_setup import current_request_id

UNRECOGNISED_FIELD = "(unrecognised)"
_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}|[0-9]{1,6}")


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(RequestValidationError, _invalid_request)
    app.add_exception_handler(HTTPException, _http_error)


def _respond(status_code: int, body: dict[str, Any]) -> JSONResponse:
    return JSONResponse(body, status_code=status_code)


async def _app_error(_: Request, error: Exception) -> JSONResponse:
    response = _respond(*error_response(error, current_request_id()))
    if isinstance(error, TooManyRequestsError) and error.retry_after:
        response.headers["Retry-After"] = str(error.retry_after)
    return response


async def _invalid_request(_: Request, error: Exception) -> JSONResponse:
    if not isinstance(error, RequestValidationError):  # pragma: no cover - registered for it only
        raise error
    status_code, body = error_response(InvalidInputError(), current_request_id())
    # Where the problem is, never what was sent.
    body["error"]["fields"] = sorted(
        {".".join(_field_part(part) for part in item["loc"]) for item in error.errors()}
    )
    return _respond(status_code, body)


def _field_part(part: object) -> str:
    # The name of a field the client added is the client's own text: it is
    # shown only if it looks like a field name.
    text = str(part)
    return text if _FIELD_NAME.fullmatch(text) else UNRECOGNISED_FIELD


async def _http_error(_: Request, error: Exception) -> JSONResponse:
    if not isinstance(error, HTTPException):  # pragma: no cover - registered for it only
        raise error
    if error.status_code == 404:
        return _respond(*error_response(NotFoundError(), current_request_id()))
    _status, body = error_response(AppError(), current_request_id())
    body["error"]["code"] = "method_not_allowed" if error.status_code == 405 else "http_error"
    body["error"]["message"] = (
        "This method is not allowed here." if error.status_code == 405 else "Request refused."
    )
    return _respond(error.status_code, body)
