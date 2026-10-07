import json

import pytest

from app.core.exceptions import (
    AppError,
    AuthenticationError,
    InvalidInputError,
    NotFoundError,
    PermissionDeniedError,
    ServiceUnavailableError,
    error_response,
)

INTERNAL_DETAIL = "document 42 (Executive Compensation Strategy) is not visible to user 7"


@pytest.mark.parametrize(
    ("error_type", "status_code", "code"),
    [
        (AppError, 500, "internal_error"),
        (AuthenticationError, 401, "authentication_failed"),
        (PermissionDeniedError, 403, "permission_denied"),
        (NotFoundError, 404, "not_found"),
        (InvalidInputError, 422, "invalid_input"),
        (ServiceUnavailableError, 503, "service_unavailable"),
    ],
)
def test_status_and_code(error_type: type[AppError], status_code: int, code: str) -> None:
    status, body = error_response(error_type())

    assert status == status_code
    assert body["error"]["code"] == code
    assert body["error"]["message"] == error_type.public_message


@pytest.mark.parametrize(
    "error_type",
    [AppError, AuthenticationError, PermissionDeniedError, NotFoundError, ServiceUnavailableError],
)
def test_detail_is_kept_for_logs_but_not_sent_to_the_client(error_type: type[AppError]) -> None:
    error = error_type(INTERNAL_DETAIL, document_id=42, user_id=7)

    _, body = error_response(error)

    assert str(error) == INTERNAL_DETAIL
    assert error.context == {"document_id": 42, "user_id": 7}
    assert "Executive" not in json.dumps(body)
    assert "42" not in json.dumps(body)


def test_hidden_document_and_missing_document_look_the_same() -> None:
    hidden = error_response(NotFoundError(INTERNAL_DETAIL))
    missing = error_response(NotFoundError("no document with id 999"))

    assert hidden == missing


def test_authentication_failures_look_the_same() -> None:
    wrong_password = error_response(AuthenticationError("wrong password for alice"))
    unknown_user = error_response(AuthenticationError("no user named mallory"))
    inactive = error_response(AuthenticationError("user 3 is deactivated"))

    assert wrong_password == unknown_user == inactive


def test_invalid_input_tells_the_caller_what_was_wrong() -> None:
    _, body = error_response(InvalidInputError("question must not be empty"))

    assert body["error"]["message"] == "question must not be empty"


def test_invalid_input_without_detail_uses_the_default_message() -> None:
    _, body = error_response(InvalidInputError())

    assert body["error"]["message"] == InvalidInputError.public_message


def test_unexpected_error_is_reported_without_its_message() -> None:
    status, body = error_response(RuntimeError("connection to db-internal-01:5432 refused"))

    assert status == 500
    assert body == {"error": {"code": "internal_error", "message": "Something went wrong."}}


def test_request_id_is_included_when_given() -> None:
    _, body = error_response(NotFoundError(), request_id="req-1")

    assert body["error"]["request_id"] == "req-1"


def test_all_errors_can_be_caught_as_app_error() -> None:
    for error_type in (
        AuthenticationError,
        PermissionDeniedError,
        NotFoundError,
        InvalidInputError,
        ServiceUnavailableError,
    ):
        with pytest.raises(AppError):
            raise error_type()
