"""What happens around every request: a request ID, one log line, and a safe 500.

The request ID ties together every log line of one request (NFR-8). It is
returned in the ``X-Request-ID`` header and in every error body, so a person
reporting a problem can quote it. A client may send its own, for tracing
across services, but only one made of letters, digits and dashes, at most
64 long: anything else is replaced, because it is written into every log
line.

The log line has the method, the route pattern, the status and the time. Not the
query string, which can hold a question, and not the client's address.

An error nothing else handled becomes a fixed 500 here rather than in an
exception handler: the handler for unexpected errors runs outside this
middleware, after the request ID is gone.

A request body is limited in size here, before anything reads it. An
uploaded form is parsed before the endpoint's dependencies run, so before
the caller is known to be allowed to upload; without a limit, anyone could
make the service spool a body of any size. A body over the limit gets 413.
A body that doesn't say how long it is (chunked) gets 411, because its size
can only be known by reading it. The server holds a client to the length it
declared, so the declared length is a real bound.
"""

import logging
import re
import time
import traceback
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.core.exceptions import (
    AppError,
    LengthRequiredError,
    RequestTooLargeError,
    error_response,
)
from app.core.logging_setup import bind_request_id, clear_request_id

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
UNMATCHED = "(no matching route)"
_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9-]{1,64}")

Handler = Callable[[Request], Awaitable[Response]]


def install_middleware(app: FastAPI, max_body_bytes: int) -> None:
    async def around_request(request: Request, call_next: Handler) -> Response:
        return await _around_request(request, call_next, max_body_bytes)

    app.middleware("http")(around_request)


def _body_refusal(request: Request, max_body_bytes: int) -> AppError | None:
    if "transfer-encoding" in request.headers:
        return LengthRequiredError("a request body without a declared length")
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > max_body_bytes:
        return RequestTooLargeError(f"a request body of {declared} bytes")
    return None


async def _around_request(request: Request, call_next: Handler, max_body_bytes: int) -> Response:
    sent = request.headers.get(REQUEST_ID_HEADER, "")
    request_id = bind_request_id(sent if _SAFE_REQUEST_ID.fullmatch(sent) else None)
    started = time.perf_counter()
    try:
        try:
            refusal = _body_refusal(request, max_body_bytes)
            if refusal is not None:
                status_code, body = error_response(refusal, request_id)
                response: Response = JSONResponse(body, status_code=status_code)
            else:
                response = await call_next(request)
        except Exception as error:
            # The type and where it happened go to the log, not the message:
            # an error from a library can carry document text in it (NFR-9).
            # The client gets the fixed message and the ID to quote.
            logger.error(
                "unexpected error",
                extra={
                    "error": type(error).__name__,
                    "where": "".join(traceback.format_tb(error.__traceback__)),
                },
            )
            status_code, body = error_response(RuntimeError(), request_id)
            response = JSONResponse(body, status_code=status_code)
        response.headers[REQUEST_ID_HEADER] = request_id
        route = request.scope.get("route")
        logger.info(
            "request handled",
            extra={
                "method": request.method,
                # The route's pattern, /documents/{document_id}. A path that
                # matched no route is not logged: it is whatever the client
                # typed, and could be a question or a line made to look like
                # a log entry.
                "path": getattr(route, "path", UNMATCHED),
                "status": response.status_code,
                "milliseconds": round((time.perf_counter() - started) * 1000),
            },
        )
        return response
    finally:
        clear_request_id()
