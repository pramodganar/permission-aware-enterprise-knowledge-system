"""Signing in and staying signed in, against a fake service."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from ui.client import Api, Login, Me
from ui.session import (
    ASK,
    HOME,
    LOGIN,
    ME,
    NOTICE,
    SESSION_ENDED,
    SIGN_IN_FAILED,
    SIGNED_OUT,
    UNREACHABLE,
    current_user,
    pages_for,
    sign_in,
    sign_out,
    token,
    what_they_can_do,
)

PASSWORD = "correct horse battery staple"
DAVID = {
    "id": 4,
    "email": "david@acmecorp.example",
    "full_name": "David Okafor",
    "department": {"code": "sales", "name": "Sales"},
    "roles": ["employee"],
    "permissions": ["documents:read:general", "documents:read:own_department"],
}


def refusal(status: int, code: str, **headers: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": "No."}}, headers=headers)


class Service:
    """A fake service: login and who-am-I, with whatever answers a test sets."""

    def __init__(self) -> None:
        self.login: Callable[[], httpx.Response] = lambda: httpx.Response(
            200, json={"access_token": "tok-1", "token_type": "bearer", "expires_in": 1800}
        )
        self.me: Callable[[], httpx.Response] = lambda: httpx.Response(200, json=DAVID)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.login() if request.url.path == "/auth/login" else self.me()


@pytest.fixture
def service() -> Service:
    return Service()


@pytest.fixture
def api(service: Service) -> Api:
    return Api(
        http=httpx.Client(base_url="http://api.test", transport=httpx.MockTransport(service))
    )


def signed_in(state: dict[str, Any], expires_at: datetime | None = None) -> dict[str, Any]:
    state[LOGIN] = Login("tok-1", expires_at or datetime.now(UTC) + timedelta(minutes=30))
    state[ME] = Me.model_validate(DAVID)
    return state


# --- signing in -------------------------------------------------------------------------


def test_signing_in_keeps_the_token_and_who_they_are_and_not_the_password(api: Api) -> None:
    state: dict[str, Any] = {}

    assert sign_in(api, state, " david@acmecorp.example ", PASSWORD) is None

    assert set(state) == {LOGIN, ME}
    assert token(state) == "tok-1"
    assert state[ME].full_name == "David Okafor"
    assert PASSWORD not in repr(state)


@pytest.mark.parametrize(
    "reply",
    [
        refusal(401, "authentication_failed"),
        refusal(422, "invalid_input"),
        refusal(500, "internal_error"),
    ],
    ids=["refused", "not an email", "service error"],
)
def test_every_failed_sign_in_says_the_same(
    api: Api, service: Service, reply: httpx.Response
) -> None:
    service.login = lambda: reply
    state: dict[str, Any] = {}

    assert sign_in(api, state, "david@acmecorp.example", "wrong") == SIGN_IN_FAILED
    assert state == {}


def test_too_many_attempts_says_how_long_to_wait(api: Api, service: Service) -> None:
    service.login = lambda: refusal(429, "too_many_requests", **{"Retry-After": "900"})

    assert sign_in(api, {}, "frank@acmecorp.example", PASSWORD) == (
        "Too many attempts. Try again in 15 minutes."
    )


def test_a_service_that_can_not_be_reached_says_so(service: Service) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    api = Api(http=httpx.Client(base_url="http://api.test", transport=httpx.MockTransport(down)))

    assert sign_in(api, {}, "david@acmecorp.example", PASSWORD) == UNREACHABLE


@pytest.mark.parametrize(("address", "password"), [("", PASSWORD), ("  ", PASSWORD), ("d@x", "")])
def test_an_empty_field_is_caught_before_asking_the_service(
    api: Api, service: Service, address: str, password: str
) -> None:
    assert sign_in(api, {}, address, password) == "Enter your email and password."
    assert service.requests == []


def test_signing_in_again_forgets_the_last_person_first(api: Api, service: Service) -> None:
    state = signed_in({})
    service.login = lambda: refusal(401, "authentication_failed")

    sign_in(api, state, "someone@acmecorp.example", "wrong")

    assert token(state) == ""
    assert ME not in state


# --- staying signed in ------------------------------------------------------------------


def test_nobody_is_signed_in_until_someone_signs_in(api: Api, service: Service) -> None:
    assert current_user(api, {}) is None
    assert service.requests == []


def test_who_they_are_is_asked_again_every_time(api: Api, service: Service) -> None:
    state = signed_in({})
    promoted = {**DAVID, "roles": ["employee", "executive"]}
    service.me = lambda: httpx.Response(200, json=promoted)

    me = current_user(api, state)

    assert me is not None
    assert me.roles == ["employee", "executive"]
    assert state[ME].roles == ["employee", "executive"]
    assert service.requests[-1].headers["Authorization"] == "Bearer tok-1"


def test_an_expired_token_signs_them_out_without_asking_the_service(
    api: Api, service: Service
) -> None:
    state = signed_in({}, expires_at=datetime.now(UTC) - timedelta(seconds=1))

    assert current_user(api, state) is None
    assert state == {NOTICE: SESSION_ENDED}
    assert service.requests == []


def test_a_token_the_service_no_longer_accepts_signs_them_out(api: Api, service: Service) -> None:
    # A deactivated account, for example: the token hasn't expired, the service refuses it.
    service.me = lambda: refusal(401, "authentication_failed")
    state = signed_in({})

    assert current_user(api, state) is None
    assert state == {NOTICE: SESSION_ENDED}


def test_a_service_that_is_down_does_not_sign_them_out(service: Service) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    api = Api(http=httpx.Client(base_url="http://api.test", transport=httpx.MockTransport(down)))
    state = signed_in({})

    me = current_user(api, state)

    assert me is not None
    assert me.full_name == "David Okafor"
    assert state[NOTICE] == UNREACHABLE
    assert token(state) == "tok-1"


def test_the_notice_goes_once_the_service_answers_again(api: Api) -> None:
    state = signed_in({})
    state[NOTICE] = UNREACHABLE

    current_user(api, state)

    assert NOTICE not in state


def test_signing_out_forgets_everything_and_says_so() -> None:
    state = signed_in({})

    sign_out(state)

    assert state == {NOTICE: SIGNED_OUT}
    assert token(state) == ""


# --- what to offer ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("permissions", "expected"),
    [
        (["documents:read:general"], ["Ask questions"]),
        (["documents:manage"], ["Upload and manage documents"]),
        (["audit:read"], ["Review the audit trail"]),
        ([], ["Nothing yet"]),
        (["documents:read:general", "documents:manage"], ["Ask questions", "Upload and manage"]),
    ],
)
def test_what_the_page_offers_follows_the_permissions_the_service_gave(
    permissions: list[str], expected: list[str]
) -> None:
    me = Me.model_validate({**DAVID, "permissions": permissions})

    lines = what_they_can_do(me)

    assert len(lines) == len(expected)
    assert all(line.startswith(start) for line, start in zip(lines, expected, strict=True))


@pytest.mark.parametrize(
    ("permissions", "pages"),
    [
        (["documents:read:general"], [HOME, ASK]),
        (["documents:read:all_departments"], [HOME, ASK]),
        (["documents:manage"], [HOME]),
        (["audit:read"], [HOME]),
        ([], [HOME]),
    ],
)
def test_asking_is_offered_only_to_someone_who_may_read(
    permissions: list[str], pages: list[str]
) -> None:
    me = Me.model_validate({**DAVID, "permissions": permissions})

    assert pages_for(me) == pages
