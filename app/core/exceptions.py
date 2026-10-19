"""Errors the service raises on purpose, and how they are shown to a client.

Each error has two messages. ``detail`` is for logs and can say exactly what
happened. ``public_message`` is fixed per error type and is all a client sees,
so an error response can't reveal which document, user or rule was involved.

A question the user isn't allowed to have answered is not an error. It gets
the normal "no information available" response. For a direct lookup of a
document the user can't read, raise NotFoundError, the same as for a document
that doesn't exist.
"""

from typing import Any


class AppError(Exception):
    """Base class. Unexpected failures are reported with these values too."""

    code = "internal_error"
    status_code = 500
    public_message = "Something went wrong."
    # True only where the detail describes the caller's own input.
    expose_detail = False

    def __init__(self, detail: str = "", **context: Any) -> None:
        super().__init__(detail or self.public_message)
        self.detail = detail
        self.context = context


class AuthenticationError(AppError):
    """Missing, invalid or expired credentials, or an inactive account.

    One message for every cause, so a caller can't tell a wrong password
    from an unknown user.
    """

    code = "authentication_failed"
    status_code = 401
    public_message = "Authentication failed."


class PermissionDeniedError(AppError):
    """The user may not perform this action, for example an admin-only one.

    Not for document content. See the module docstring.
    """

    code = "permission_denied"
    status_code = 403
    public_message = "You are not allowed to perform this action."


class NotFoundError(AppError):
    code = "not_found"
    status_code = 404
    public_message = "Not found."


class InvalidInputError(AppError):
    code = "invalid_input"
    status_code = 422
    public_message = "The request is not valid."
    expose_detail = True


class TooManyRequestsError(AppError):
    """Too many attempts in a short time. The same for every cause it has."""

    code = "too_many_requests"
    status_code = 429
    public_message = "Too many attempts. Try again later."

    def __init__(self, detail: str = "", retry_after: int = 0, **context: Any) -> None:
        super().__init__(detail, **context)
        # Seconds until the next attempt can succeed, for the Retry-After header.
        self.retry_after = retry_after


class LengthRequiredError(AppError):
    """A request body sent without saying how long it is."""

    code = "length_required"
    status_code = 411
    public_message = "The request must say how long its body is."


class RequestTooLargeError(AppError):
    code = "request_too_large"
    status_code = 413
    public_message = "The request is too large."


class ServiceUnavailableError(AppError):
    """Something the request depends on failed, so the request is refused."""

    code = "service_unavailable"
    status_code = 503
    public_message = "The service is temporarily unavailable."


class AnswersSwitchedOffError(ServiceUnavailableError):
    """No language model is configured (APP_LLM_PROVIDER=none). Search still works."""

    public_message = "Answers are switched off on this service. Search still works."


def error_response(error: Exception, request_id: str | None = None) -> tuple[int, dict[str, Any]]:
    """Return the HTTP status and response body for an error.

    Anything that is not an AppError is reported as a plain internal error,
    without its message.
    """
    if isinstance(error, AppError):
        status_code, code = error.status_code, error.code
        message = error.detail if error.expose_detail and error.detail else error.public_message
    else:
        status_code, code, message = AppError.status_code, AppError.code, AppError.public_message

    body: dict[str, Any] = {"code": code, "message": message}
    if request_id:
        body["request_id"] = request_id
    return status_code, {"error": body}
