"""The service's description of itself is true (docs/api.md, docs/openapi.json).

What a client reads at /docs, or in docs/openapi.json when /docs is off, is
what it will build against. So every route says which errors it can give,
each described with the body the service really sends, and here every route
is made to give every error it declares: a status that never comes, or one
that comes and isn't declared, fails. The file in the repository must be
what the service says, so it can't fall behind a change to a route.
"""

import json
import re
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Any

import pytest
from docrefs import ROOT
from fastapi.testclient import TestClient
from httpx2 import Response
from routes import INTERACTIVE_DOCS, routes_of
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from apiclient import MISSING, company_client, email, headers, public_path
from app.api.responses import ErrorBody
from app.core.config import Settings
from app.core.exceptions import error_response
from app.main import create_app
from fakes import FakeLLM
from scripts import openapi

MakeClient = Callable[..., TestClient]

# Every route and the errors it declares, besides those any route can give.
DECLARED: dict[tuple[str, str], set[int]] = {
    ("GET", "/health"): {503},
    ("POST", "/auth/login"): {401, 422, 429, 503},
    ("GET", "/auth/me"): {401},
    ("POST", "/auth/logout"): {401, 503},
    ("GET", "/documents"): {401, 422},
    ("GET", "/documents/{document_id}"): {401, 404, 422, 503},
    ("POST", "/search"): {401, 422, 429, 503},
    ("POST", "/ask"): {401, 422, 429, 503},
    ("POST", "/admin/documents"): {401, 403, 422, 503},
    ("GET", "/admin/documents"): {401, 403, 422, 503},
    ("GET", "/admin/documents/{document_id}"): {401, 403, 404, 422, 503},
    ("POST", "/admin/documents/{document_id}/versions"): {401, 403, 404, 422, 503},
    ("PATCH", "/admin/documents/{document_id}"): {401, 403, 404, 422, 503},
    ("DELETE", "/admin/documents/{document_id}"): {401, 403, 404, 422, 503},
    ("PUT", "/admin/documents/{document_id}/grants"): {401, 403, 404, 422, 503},
    ("DELETE", "/admin/documents/{document_id}/grants/{email}"): {401, 403, 404, 422, 503},
    ("GET", "/audit"): {401, 403, 422, 503},
}
CODES = {
    401: "authentication_failed",
    403: "permission_denied",
    404: "not_found",
    422: "invalid_input",
    429: "too_many_requests",
    503: "service_unavailable",
}
QUESTION = {"question": "How many days of annual leave do I get?"}
WRONG_PASSWORD = {"email": email("david"), "password": "wrong"}


def a_file() -> dict[str, Any]:
    return {"file": ("note.txt", b"Booth crates ship from dock 4.", "text/plain")}


@dataclass(frozen=True)
class Call:
    """A request to a route that would succeed for the right person. <n> is document n."""

    path: str
    json: Any = None
    upload: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    def on(self, path: str) -> "Call":
        return Call(path, self.json, self.upload, self.data)

    def send(self, client: TestClient, method: str, auth: dict[str, str] | None) -> Response:
        path = public_path(client, self.path)
        if self.upload:
            return client.request(method, path, headers=auth, files=a_file(), data=self.data)
        if self.json is not None:
            return client.request(method, path, headers=auth, json=self.json)
        return client.request(method, path, headers=auth)


UPLOAD = {"title": "Booth Note", "department": "sales", "classification": "general"}
# The request each route gets, and who may make it.
CALLS: dict[tuple[str, str], tuple[str | None, Call]] = {
    ("GET", "/health"): (None, Call("/health")),
    ("POST", "/auth/login"): (None, Call("/auth/login", {"email": email("david")})),
    ("GET", "/auth/me"): ("david", Call("/auth/me")),
    ("POST", "/auth/logout"): ("david", Call("/auth/logout")),
    ("GET", "/documents"): ("david", Call("/documents")),
    ("GET", "/documents/{document_id}"): ("david", Call("/documents/<1>")),
    ("POST", "/search"): ("david", Call("/search", QUESTION)),
    ("POST", "/ask"): ("david", Call("/ask", QUESTION)),
    ("POST", "/admin/documents"): ("henry", Call("/admin/documents", upload=True, data=UPLOAD)),
    ("GET", "/admin/documents"): ("henry", Call("/admin/documents")),
    ("GET", "/admin/documents/{document_id}"): ("henry", Call("/admin/documents/<1>")),
    ("POST", "/admin/documents/{document_id}/versions"): (
        "henry",
        Call("/admin/documents/<1>/versions", upload=True),
    ),
    ("PATCH", "/admin/documents/{document_id}"): (
        "henry",
        Call("/admin/documents/<1>", {"classification": "departmental"}),
    ),
    ("DELETE", "/admin/documents/{document_id}"): ("henry", Call("/admin/documents/<1>")),
    ("PUT", "/admin/documents/{document_id}/grants"): (
        "henry",
        Call("/admin/documents/<1>/grants", {"email": email("david")}),
    ),
    ("DELETE", "/admin/documents/{document_id}/grants/{email}"): (
        "henry",
        Call("/admin/documents/<1>/grants/" + email("david")),
    ),
    ("GET", "/audit"): ("irene", Call("/audit")),
}
# The same request made wrong, for each route that declares 422.
INVALID: dict[tuple[str, str], Call] = {
    ("POST", "/auth/login"): Call("/auth/login", {"email": email("david")}),
    ("GET", "/documents"): Call("/documents?limit=0"),
    ("GET", "/documents/{document_id}"): Call("/documents/not-an-id"),
    ("POST", "/search"): Call("/search", {"question": "   "}),
    ("POST", "/ask"): Call("/ask", {"question": QUESTION["question"], "user_id": 7}),
    ("POST", "/admin/documents"): Call(
        "/admin/documents", upload=True, data={"title": "Booth Note", "department": "sales"}
    ),
    ("GET", "/admin/documents"): Call("/admin/documents?limit=0"),
    ("GET", "/admin/documents/{document_id}"): Call("/admin/documents/not-an-id"),
    ("POST", "/admin/documents/{document_id}/versions"): Call(
        "/admin/documents/not-an-id/versions", upload=True
    ),
    ("PATCH", "/admin/documents/{document_id}"): Call(
        "/admin/documents/<1>", {"classification": "secret"}
    ),
    ("DELETE", "/admin/documents/{document_id}"): Call("/admin/documents/not-an-id"),
    ("PUT", "/admin/documents/{document_id}/grants"): Call(
        "/admin/documents/<1>/grants", {"email": email("nobody")}
    ),
    ("DELETE", "/admin/documents/{document_id}/grants/{email}"): Call(
        "/admin/documents/not-an-id/grants/" + email("david")
    ),
    ("GET", "/audit"): Call("/audit?limit=0"),
}
# Who may make the call, for the routes they can be refused: someone else.
REFUSED = {"henry": "david", "irene": "david"}


@pytest.fixture
def make_client(db: Session, engine: Engine, settings: Settings) -> Iterator[MakeClient]:
    with ExitStack() as stack:

        def make(**changes: Any) -> TestClient:
            changed = settings.model_copy(update={"llm_max_retries": 0, **changes})
            provider = FakeLLM(["Twenty-five days [S1]."])
            return stack.enter_context(company_client(db, engine, changed, provider))

        yield make


def spec() -> dict[str, Any]:
    return openapi.description()


def declared_in(document: dict[str, Any]) -> dict[tuple[str, str], set[int]]:
    found = {}
    for path, operations in document["paths"].items():
        for method, operation in operations.items():
            found[(method.upper(), path)] = {
                int(status) for status in operation["responses"] if not status.startswith("2")
            }
    return found


def break_the_audit_store(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE audit_seals"))
        connection.execute(text("DROP TABLE audit_records"))


def break_the_database(client: TestClient) -> None:
    class Broken:
        def __enter__(self) -> "Broken":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def close(self) -> None:
            return None

        def execute(self, *_: object) -> None:
            raise OSError("disk gone")

    client.app.state.session_factory = lambda: Broken()  # type: ignore[attr-defined]


# --- what the description says ----------------------------------------------------------


def test_the_file_in_the_repository_is_what_the_service_says() -> None:
    assert openapi.main(["--check"]) == 0, "run python -m scripts.openapi"


def test_every_route_is_in_the_table_and_the_table_is_what_is_declared(
    settings: Settings,
) -> None:
    served = set(routes_of(create_app(settings))) - INTERACTIVE_DOCS

    assert set(DECLARED) == served
    assert declared_in(spec()) == DECLARED


def test_every_error_is_described_with_the_body_the_service_sends() -> None:
    document = spec()

    assert "HTTPValidationError" not in json.dumps(document)
    for path, operations in document["paths"].items():
        for method, operation in operations.items():
            for status, response in operation["responses"].items():
                if status.startswith("2"):
                    continue
                schema = response["content"]["application/json"]["schema"]
                assert schema == {"$ref": "#/components/schemas/ErrorBody"}, (method, path, status)
                assert response["description"], (method, path, status)


def test_every_route_has_a_summary_and_a_description() -> None:
    for path, operations in spec()["paths"].items():
        for method, operation in operations.items():
            assert operation.get("summary"), (method, path)
            assert len(operation.get("description", "")) > 40, (method, path)


def test_every_route_with_a_token_says_so() -> None:
    for (method, path), statuses in declared_in(spec()).items():
        operation = spec()["paths"][path][method.lower()]
        assert ("security" in operation) == (401 in statuses and path != "/auth/login"), path


def test_the_error_body_is_the_one_errors_are_made_with() -> None:
    _, body = error_response(KeyError("anything"), "6f1c2a9e4b7d4e0c9a1b3d5f7e2c4a68")

    assert ErrorBody.model_validate(body).error.code == "internal_error"


# --- and every route gives every error it declares, and no other ----------------------
#
# One way to bring about each status, tried on every route. On a route that declares
# the status, it must bring it about. On one that doesn't, whatever comes back must be
# success or an error the route declares: so a route can't give an error it leaves out.


def provoke(
    make_client: MakeClient, engine: Engine, route: tuple[str, str], status: int
) -> Response:
    method = route[0]
    who, call = CALLS[route]
    login = route == ("POST", "/auth/login")

    if status == 401:
        client = make_client()
        if login:
            return client.post("/auth/login", json=WRONG_PASSWORD)
        return call.send(client, method, None)

    if status == 429:
        client = make_client(
            login_max_failures=1, ask_limit_per_minute=1, search_limit_per_minute=1
        )
        if login:
            client.post("/auth/login", json=WRONG_PASSWORD)
            return client.post("/auth/login", json=WRONG_PASSWORD)
        auth = headers(client, who) if who else None
        call.send(client, method, auth)
        return call.send(client, method, auth)

    client = make_client()
    if status == 503:
        if route == ("GET", "/health"):
            break_the_database(client)
            return call.send(client, method, None)
        if login:
            break_the_audit_store(engine)
            return client.post("/auth/login", json=WRONG_PASSWORD)
        # Signed in before the store goes. Where the route needs a permission, someone
        # refused: recording the refusal is what fails.
        auth = headers(client, REFUSED.get(who, who)) if who else None
        break_the_audit_store(engine)
        return call.send(client, method, auth)

    if who is None:
        return (INVALID.get(route, call) if status == 422 else call).send(client, method, None)
    if status == 403:
        # Someone without the permission, or for routes everyone signed in may use,
        # someone who may read nothing.
        return call.send(client, method, headers(client, REFUSED.get(who, "henry")))
    if status == 404:
        missing = call.on(call.path.replace("<1>", str(MISSING)))
        return missing.send(client, method, headers(client, who))
    assert status == 422
    return INVALID.get(route, call).send(client, method, headers(client, who))


def ids(value: object) -> str:
    return " ".join(value) if isinstance(value, tuple) else str(value)


@pytest.mark.parametrize(
    ("route", "status"),
    [(route, status) for route, statuses in DECLARED.items() for status in sorted(statuses)],
    ids=ids,
)
def test_each_route_gives_each_error_it_declares(
    make_client: MakeClient, engine: Engine, route: tuple[str, str], status: int
) -> None:
    response = provoke(make_client, engine, route, status)

    assert response.status_code == status, response.text
    body = ErrorBody.model_validate(response.json())
    assert body.error.code == CODES[status]
    assert body.error.request_id == response.headers["X-Request-ID"]
    if status == 429:
        assert int(response.headers["Retry-After"]) > 0


@pytest.mark.parametrize(
    ("route", "status"),
    [
        (route, status)
        for route, statuses in DECLARED.items()
        for status in sorted(set(CODES) - statuses)
    ],
    ids=ids,
)
def test_no_route_gives_an_error_it_does_not_declare(
    make_client: MakeClient, engine: Engine, route: tuple[str, str], status: int
) -> None:
    response = provoke(make_client, engine, route, status)

    assert response.status_code < 300 or response.status_code in DECLARED[route], response.text


# --- and docs/api.md says the same ------------------------------------------------------


def api_page_routes() -> dict[tuple[str, str], tuple[int, set[int]]]:
    page = (ROOT / "docs" / "api.md").read_text(encoding="utf-8")
    section = page.split("## Every route\n", 1)[1].split("\n## ", 1)[0]
    found = {}
    for match in re.finditer(
        r"^\| `(\w+) (\S+)` \| [^|]+ \| (\d{3}) \| ([\d, ]+) \|$", section, re.M
    ):
        method, path, success, errors = match.groups()
        found[(method, path)] = (int(success), {int(s) for s in errors.split(", ")})
    return found


def test_the_api_pages_table_is_what_the_service_declares() -> None:
    document = spec()
    expected = {}
    for (method, path), statuses in declared_in(document).items():
        responses = document["paths"][path][method.lower()]["responses"]
        (success,) = [int(status) for status in responses if status.startswith("2")]
        expected[(method, path)] = (success, statuses)

    assert api_page_routes() == expected


def test_the_api_page_counts_the_routes_there_are() -> None:
    page = (ROOT / "docs" / "api.md").read_text(encoding="utf-8")

    assert f"The HTTP service (FR-15): {len(DECLARED)} routes" in page
