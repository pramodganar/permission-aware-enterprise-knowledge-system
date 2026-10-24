"""Generated requests for every route that takes input (threat T40).

Whatever a client sends, the service answers it or refuses it in its own
words: never a 500, every refusal in the one error shape, a refused change
leaves everything as it was, and a request ID that isn't safe to log is
replaced rather than kept. Hypothesis generates the bodies, query strings,
path segments, headers and uploaded files; runs are derandomized, so a
failure comes back the same way every time.
"""

import json
import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote, urlencode

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from apiclient import company_client, email, headers
from app.core.config import Settings
from app.models import Document, DocumentGrant
from fakes import FakeLLM

# One client for every example of a test: the company is seeded once per test.
SHARED = settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
ERROR_KEYS = {"code", "message", "request_id", "fields"}
SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9-]{1,64}")

JSON = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(-(2**70), 2**70)
    | st.floats(allow_nan=False)
    | st.text(max_size=30),
    lambda inner: (
        st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=12), inner, max_size=4)
    ),
    max_leaves=10,
)
QUESTIONS = st.one_of(
    st.text(max_size=300),
    st.text(alphabet=st.characters(codec="utf-8"), min_size=1, max_size=40),
    st.sampled_from(["How many days of annual leave do I get?", "", " ", "?" * 5, "a" * 1200]),
)
FILTERS = st.one_of(
    JSON,
    st.fixed_dictionaries(
        {},
        optional={
            "departments": st.lists(st.text(max_size=12), max_size=3),
            "classifications": st.lists(st.text(max_size=12), max_size=3),
            "document_ids": st.lists(st.integers(-5, 2**64), max_size=3),
        },
    ),
)
BODIES = st.one_of(
    JSON,
    st.fixed_dictionaries({"question": QUESTIONS}, optional={"filters": FILTERS, "extra": JSON}),
)


@pytest.fixture
def client(db: Session, engine: Engine, settings: Settings) -> Iterator[TestClient]:
    with company_client(db, engine, settings, FakeLLM()) as client:
        yield client


@pytest.fixture
def signed_in(client: TestClient) -> dict[str, dict[str, str]]:
    """A token for each person the tests use, made once for all of a test's examples."""
    return {name: headers(client, name) for name in ("david", "henry", "irene")}


def cleanly(response: Response, *allowed: int) -> None:
    """Answered, or refused in the service's own words."""
    assert response.status_code in allowed, (response.status_code, response.text[:300])
    if response.status_code >= 400:
        error = response.json()["error"]
        assert set(error) <= ERROR_KEYS
        assert {"code", "message", "request_id"} <= set(error)


def raw_json(body: Any) -> bytes:
    return json.dumps(body).encode()


@SHARED
@given(body=BODIES, route=st.sampled_from(["/search", "/ask"]))
def test_any_question_body_is_answered_or_refused_in_the_services_words(
    client: TestClient, signed_in: dict[str, dict[str, str]], body: Any, route: str
) -> None:
    response = client.post(
        route,
        headers={**signed_in["david"], "Content-Type": "application/json"},
        content=raw_json(body),
    )

    cleanly(response, 200, 422)


@SHARED
@given(body=st.one_of(JSON, st.fixed_dictionaries({"email": JSON, "password": JSON})))
def test_any_login_body_is_refused_in_the_services_words_and_never_echoed(
    client: TestClient, body: Any
) -> None:
    response = client.post(
        "/auth/login", headers={"Content-Type": "application/json"}, content=raw_json(body)
    )

    cleanly(response, 401, 422, 429)
    if (
        isinstance(body, dict)
        and isinstance(body.get("password"), str)
        and len(body["password"]) > 3
    ):
        assert body["password"] not in response.text


@SHARED
@given(
    segment=st.text(min_size=1, max_size=40),
    params=st.dictionaries(
        st.sampled_from(["department", "classification", "limit", "offset", "x"]),
        st.text(max_size=20),
        max_size=4,
    ),
)
def test_any_path_or_query_for_documents_is_answered_or_refused(
    client: TestClient,
    signed_in: dict[str, dict[str, str]],
    segment: str,
    params: dict[str, str],
) -> None:
    david = signed_in["david"]

    cleanly(client.get(f"/documents/{quote(segment, safe='')}", headers=david), 200, 404, 422)
    cleanly(client.get(f"/documents?{urlencode(params)}", headers=david), 200, 422)


@SHARED
# Printable ASCII: what a header can carry from this test client.
@given(sent=st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=80))
def test_a_request_id_is_kept_only_when_it_is_safe_to_log(client: TestClient, sent: str) -> None:
    response = client.get("/health", headers={"X-Request-ID": sent})

    returned = response.headers["X-Request-ID"]
    assert SAFE_REQUEST_ID.fullmatch(returned)
    assert (returned == sent) == bool(SAFE_REQUEST_ID.fullmatch(sent))


def labels(db: Session) -> list[tuple[Any, ...]]:
    db.expire_all()
    documents = db.execute(
        select(Document.id, Document.status, Document.classification, Document.department_id)
    ).all()
    grants = db.execute(select(DocumentGrant.document_id, DocumentGrant.user_id)).all()
    return sorted(documents) + sorted(grants)


@SHARED
@given(
    change=st.one_of(
        JSON,
        st.fixed_dictionaries(
            {}, optional={"classification": JSON, "department": JSON, "status": JSON}
        ),
    ),
    grant=st.one_of(JSON, st.fixed_dictionaries({"email": JSON}, optional={"expires_at": JSON})),
)
def test_any_change_an_admin_sends_is_made_or_refused_and_a_refusal_changes_nothing(
    client: TestClient,
    signed_in: dict[str, dict[str, str]],
    db: Session,
    change: Any,
    grant: Any,
) -> None:
    henry = {**signed_in["henry"], "Content-Type": "application/json"}
    document = db.scalars(select(Document.id).where(Document.title == "Salary Bands 2026")).one()

    for method, path, body in (
        ("PATCH", f"/admin/documents/{document}", change),
        ("PUT", f"/admin/documents/{document}/grants", grant),
    ):
        before = labels(db)
        response = client.request(method, path, headers=henry, content=raw_json(body))
        cleanly(response, 200, 403, 404, 422)
        if response.status_code != 200:
            assert labels(db) == before


@SHARED
@given(
    params=st.dictionaries(
        st.sampled_from(["actor", "action", "document_id", "since", "until", "limit", "offset"]),
        st.one_of(st.text(max_size=30), st.integers(-(2**70), 2**70).map(str)),
        max_size=5,
    )
)
def test_any_query_of_the_audit_trail_is_answered_or_refused(
    client: TestClient, signed_in: dict[str, dict[str, str]], params: dict[str, str]
) -> None:
    response = client.get(f"/audit?{urlencode(params)}", headers=signed_in["irene"])

    cleanly(response, 200, 422)


@settings(parent=SHARED, max_examples=40)
@given(
    name=st.text(min_size=1, max_size=30),
    data=st.binary(max_size=3000),
    form=st.fixed_dictionaries(
        {
            "title": st.text(max_size=40),
            "department": st.one_of(st.sampled_from(["sales", "hr"]), st.text(max_size=12)),
            "classification": st.one_of(
                st.sampled_from(["general", "restricted"]), st.text(max_size=12)
            ),
        },
        optional={
            "readers": st.lists(
                st.one_of(st.just(email("david")), st.text(max_size=20)), max_size=3
            )
        },
    ),
)
def test_any_upload_is_stored_or_refused_and_a_refusal_stores_nothing(
    client: TestClient,
    signed_in: dict[str, dict[str, str]],
    db: Session,
    name: str,
    data: bytes,
    form: dict[str, Any],
) -> None:
    before = labels(db)

    response = client.post(
        "/admin/documents",
        headers=signed_in["henry"],
        files={"file": (name, data, "application/octet-stream")},
        data=form,
    )

    cleanly(response, 201, 403, 422)
    if response.status_code != 201:
        assert labels(db) == before
