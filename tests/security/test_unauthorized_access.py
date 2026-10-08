"""Attempts to reach documents and actions without the right to.

Each test is something an attacker with a normal account, or no account,
might try over HTTP. Every one of them has to fail.
"""

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from acmecorp import COMPANY_DOCUMENTS, PERSONAS, World
from app.auth.tokens import create_access_token
from app.core.config import Settings
from app.core.database import utc_now
from app.models import RoleCode
from testapp import build_test_app

NON_READERS = ("admin", "reviewer", "nobody")
ALL_TITLES = {title for title, _, _ in COMPANY_DOCUMENTS.values()}
AUTH_FAILED = {"error": {"code": "authentication_failed", "message": "Authentication failed."}}
NOT_FOUND = {"error": {"code": "not_found", "message": "Not found."}}


@pytest.fixture
def client(engine: Engine, settings: Settings) -> TestClient:
    return TestClient(build_test_app(engine, settings))


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def login(company: World, settings: Settings) -> dict[str, dict[str, str]]:
    """Valid authorization headers for every account in the company."""
    return {
        name: bearer(create_access_token(user.id, settings)) for name, user in company.users.items()
    }


Login = dict[str, dict[str, str]]


def b64(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def claims_for(user_id: int, **extra: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {"sub": str(user_id), "iat": now, "exp": now + timedelta(minutes=5), **extra}


def titles_visible_to(client: TestClient, headers: dict[str, str]) -> set[str]:
    return {document["title"] for document in client.get("/documents", headers=headers).json()}


def allowed_titles(company: World, user: str) -> set[str]:
    return {COMPANY_DOCUMENTS[name][0] for name in company.readable(user)}


# Pretending to be someone else


def test_token_signed_with_the_attackers_own_key_is_rejected(
    client: TestClient, company: World
) -> None:
    forged = jwt.encode(
        claims_for(company.users["grace"].id), "attacker-key-" + "z" * 60, algorithm="HS256"
    )

    response = client.get("/documents", headers=bearer(forged))

    assert response.status_code == 401
    assert response.json() == AUTH_FAILED


def test_unsigned_token_claiming_to_be_the_executive_is_rejected(
    client: TestClient, company: World
) -> None:
    header = b64({"alg": "none", "typ": "JWT"})
    payload = b64({"sub": str(company.users["grace"].id), "iat": 0, "exp": 9999999999})

    for token in (f"{header}.{payload}.", f"{header}.{payload}"):
        assert client.get("/documents", headers=bearer(token)).status_code == 401


def test_swapping_the_user_id_inside_a_real_token_is_rejected(
    client: TestClient, company: World, settings: Settings
) -> None:
    header, _, signature = create_access_token(company.users["alice"].id, settings).split(".")
    payload = b64({"sub": str(company.users["grace"].id), "iat": 0, "exp": 9999999999})

    assert (
        client.get("/documents", headers=bearer(f"{header}.{payload}.{signature}")).status_code
        == 401
    )


def test_role_and_department_claims_inside_a_valid_token_are_ignored(
    client: TestClient, company: World, settings: Settings
) -> None:
    # Even a correctly signed token can't carry privileges: only its user ID is read.
    token = jwt.encode(
        claims_for(
            company.users["alice"].id,
            roles=["executive"],
            role="executive",
            department="executive",
            is_admin=True,
        ),
        settings.jwt_secret_key.get_secret_value(),
        algorithm="HS256",
    )

    assert client.get("/whoami", headers=bearer(token)).json() == {
        "id": company.users["alice"].id,
        "department": "engineering",
        "roles": ["employee"],
    }
    assert titles_visible_to(client, bearer(token)) == allowed_titles(company, "alice")


def test_identity_claimed_in_headers_or_parameters_is_ignored(
    client: TestClient, company: World, login: Login
) -> None:
    grace_id = company.users["grace"].id
    spoof_headers = {
        **login["alice"],
        "X-User-Id": str(grace_id),
        "X-Role": "executive",
        "X-Roles": "executive,knowledge_admin",
        "X-Department": "executive",
        "X-Forwarded-User": "grace@acmecorp.example",
    }
    spoof_params: dict[str, str | int] = {
        "user_id": grace_id,
        "role": "executive",
        "department": "executive",
    }
    hidden = company.documents["exec_comp"].id

    listed = client.get("/documents", headers=spoof_headers, params=spoof_params).json()

    assert {document["title"] for document in listed} == allowed_titles(company, "alice")
    assert (
        client.get(f"/documents/{hidden}", headers=spoof_headers, params=spoof_params).status_code
        == 404
    )
    assert client.post("/documents", headers=spoof_headers, params=spoof_params).status_code == 403


def test_token_is_only_accepted_in_the_authorization_header(
    client: TestClient, company: World, settings: Settings
) -> None:
    token = create_access_token(company.users["grace"].id, settings)

    assert (
        client.get("/documents", params={"token": token, "access_token": token}).status_code == 401
    )
    assert client.get("/documents", headers={"X-Auth-Token": token}).status_code == 401

    client.cookies.set("token", token)
    client.cookies.set("access_token", token)
    assert client.get("/documents").status_code == 401


@pytest.mark.parametrize("path", ["/whoami", "/documents", "/documents/1", "/audit"])
def test_nothing_is_served_without_authentication(
    client: TestClient, company: World, path: str
) -> None:
    response = client.get(path)

    assert response.status_code == 401
    assert response.json() == AUTH_FAILED


# Reaching documents that are not yours


def test_no_account_can_fetch_any_document_it_may_not_read(
    client: TestClient, company: World, login: Login
) -> None:
    """Every forbidden pair of account and document, tried one by one."""
    probes = exposed = 0

    for user in company.users:
        readable = company.readable(user)
        for name, document in company.documents.items():
            if name in readable:
                continue
            probes += 1
            response = client.get(f"/documents/{document.id}", headers=login[user])
            if response.status_code != 404 or response.json() != NOT_FOUND:
                exposed += 1

    # 76 forbidden pairs for the seven personas, plus 3 non-reader accounts x 17 documents.
    assert probes == 76 + 51
    assert exposed == 0


@pytest.mark.parametrize("user", [*PERSONAS, *NON_READERS])
def test_document_list_shows_only_what_the_account_may_read(
    client: TestClient, company: World, login: Login, user: str
) -> None:
    visible = titles_visible_to(client, login[user])

    assert visible == allowed_titles(company, user)
    assert visible.isdisjoint(ALL_TITLES - allowed_titles(company, user))


def test_walking_every_id_finds_only_the_accounts_own_documents(
    client: TestClient, company: World, login: Login
) -> None:
    highest = max(document.id for document in company.documents.values())
    found: set[str] = set()
    refusals: set[str] = set()

    for document_id in range(1, highest + 25):
        response = client.get(f"/documents/{document_id}", headers=login["alice"])
        if response.status_code == 200:
            found.add(response.json()["title"])
        else:
            refusals.add(f"{response.status_code} {response.text}")

    assert found == allowed_titles(company, "alice")
    # Hidden documents and IDs that don't exist give one and the same response,
    # so the walk can't count or locate the documents Alice isn't shown.
    assert len(refusals) == 1


def test_refusals_never_contain_a_hidden_title(
    client: TestClient, company: World, login: Login
) -> None:
    hidden_titles = ALL_TITLES - allowed_titles(company, "alice")
    everything_alice_received = ""

    for document in company.documents.values():
        everything_alice_received += client.get(
            f"/documents/{document.id}", headers=login["alice"]
        ).text
    everything_alice_received += client.get("/documents", headers=login["alice"]).text
    everything_alice_received += client.post("/documents", headers=login["alice"]).text
    everything_alice_received += client.get("/audit", headers=login["alice"]).text

    assert not [title for title in hidden_titles if title in everything_alice_received]


@pytest.mark.parametrize(
    "document_id",
    ["0", "-1", "999999999999", "1.0", "1e1", "abc", "1 OR 1=1", "1;--", "%2e%2e", "null"],
)
def test_odd_ids_never_return_a_hidden_document_or_crash(
    client: TestClient, company: World, login: Login, document_id: str
) -> None:
    response = client.get(f"/documents/{document_id}", headers=login["alice"])
    hidden_titles = ALL_TITLES - allowed_titles(company, "alice")

    # The framework reads "1.0" as the number 1, so an odd ID can be a real one.
    # What matters is that the usual check still applies to whatever it becomes.
    assert response.status_code in (200, 404, 422)
    assert not [title for title in hidden_titles if title in response.text]
    if response.status_code == 200:
        assert response.json()["title"] in allowed_titles(company, "alice")


def test_adding_a_document_id_parameter_does_not_redirect_the_lookup(
    client: TestClient, company: World, login: Login
) -> None:
    own, hidden = company.documents["runbook"].id, company.documents["exec_comp"].id

    response = client.get(
        f"/documents/{own}", headers=login["alice"], params={"document_id": hidden, "id": hidden}
    )

    assert response.json()["title"] == "Deployment Runbook"


# Holding a powerful role is not the same as being able to read


@pytest.mark.parametrize("user", NON_READERS)
def test_non_reader_accounts_get_no_document_by_any_route(
    client: TestClient, company: World, login: Login, user: str
) -> None:
    assert client.get("/documents", headers=login[user]).json() == []
    for document in company.documents.values():
        assert client.get(f"/documents/{document.id}", headers=login[user]).status_code == 404


def test_admin_who_can_ingest_still_cannot_read(
    client: TestClient, company: World, login: Login
) -> None:
    assert client.post("/documents", headers=login["admin"]).status_code == 200
    assert client.get("/documents", headers=login["admin"]).json() == []
    assert client.get("/audit", headers=login["admin"]).status_code == 403


def test_reviewer_who_can_read_the_audit_log_still_cannot_read_documents(
    client: TestClient, company: World, login: Login
) -> None:
    assert client.get("/audit", headers=login["reviewer"]).status_code == 200
    assert client.get("/documents", headers=login["reviewer"]).json() == []
    assert client.post("/documents", headers=login["reviewer"]).status_code == 403


def test_grant_given_to_an_admin_does_not_let_them_read(
    client: TestClient, company: World, login: Login
) -> None:
    company.grant("case_file", "admin")
    company.grant("exec_comp", "admin")

    assert client.get("/documents", headers=login["admin"]).json() == []
    assert (
        client.get(
            f"/documents/{company.documents['case_file'].id}", headers=login["admin"]
        ).status_code
        == 404
    )


@pytest.mark.parametrize("user", [*PERSONAS, "reviewer", "nobody"])
def test_only_the_admin_can_ingest(
    client: TestClient, company: World, login: Login, user: str
) -> None:
    assert client.post("/documents", headers=login[user]).status_code == 403


@pytest.mark.parametrize("user", [*PERSONAS, "admin", "nobody"])
def test_only_the_reviewer_can_read_the_audit_log(
    client: TestClient, company: World, login: Login, user: str
) -> None:
    assert client.get("/audit", headers=login[user]).status_code == 403


# Access that should have ended


def test_demoted_executive_loses_cross_department_access_at_once(
    client: TestClient, company: World, login: Login
) -> None:
    hidden_after = company.documents["salary_bands"].id
    assert client.get(f"/documents/{hidden_after}", headers=login["grace"]).status_code == 200

    employee = next(role for role in company.users["alice"].roles if role.code == RoleCode.EMPLOYEE)
    company.users["grace"].roles = [employee]
    company.db.commit()

    assert client.get(f"/documents/{hidden_after}", headers=login["grace"]).status_code == 404
    assert titles_visible_to(client, login["grace"]) == allowed_titles(company, "grace")
    assert "Salary Bands" not in titles_visible_to(client, login["grace"])


def test_employee_who_moves_department_loses_the_old_departments_documents(
    client: TestClient, company: World, login: Login
) -> None:
    runbook = company.documents["runbook"].id
    assert client.get(f"/documents/{runbook}", headers=login["alice"]).status_code == 200

    company.users["alice"].department = company.users["bob"].department
    company.db.commit()

    assert client.get(f"/documents/{runbook}", headers=login["alice"]).status_code == 404
    assert "Vendor Payment Approval Limits" in titles_visible_to(client, login["alice"])


def test_deactivated_user_is_locked_out_with_a_still_valid_token(
    client: TestClient, company: World, login: Login
) -> None:
    assert client.get("/documents", headers=login["grace"]).status_code == 200

    company.users["grace"].is_active = False
    company.db.commit()

    for path in ("/whoami", "/documents", f"/documents/{company.documents['exec_comp'].id}"):
        assert client.get(path, headers=login["grace"]).status_code == 401


def test_revoked_grant_stops_working_at_once(
    client: TestClient, company: World, login: Login
) -> None:
    url = f"/documents/{company.documents['acquisition'].id}"
    company.grant("acquisition", "grace")
    assert client.get(url, headers=login["grace"]).status_code == 200

    company.db.delete(company.documents["acquisition"].grants[0])
    company.db.commit()

    assert client.get(url, headers=login["grace"]).status_code == 404
    assert "Project Falcon Acquisition Paper" not in titles_visible_to(client, login["grace"])


def test_expired_grant_stops_working_without_anyone_removing_it(
    client: TestClient, company: World, login: Login
) -> None:
    url = f"/documents/{company.documents['runbook'].id}"
    company.grant("runbook", "bob", expires_at=utc_now() + timedelta(hours=1))
    assert client.get(url, headers=login["bob"]).status_code == 200

    company.documents["runbook"].grants[0].expires_at = utc_now() - timedelta(seconds=1)
    company.db.commit()

    assert client.get(url, headers=login["bob"]).status_code == 404
    assert "Deployment Runbook" not in titles_visible_to(client, login["bob"])


def test_reclassifying_a_document_as_restricted_hides_it_from_everyone(
    client: TestClient, company: World, login: Login
) -> None:
    url = f"/documents/{company.documents['exec_comp'].id}"
    assert client.get(url, headers=login["grace"]).status_code == 200

    company.documents["exec_comp"].classification = "restricted"
    company.db.commit()

    for user in company.users:
        assert client.get(url, headers=login[user]).status_code == 404, user


def test_one_users_grant_cannot_be_used_by_another(
    client: TestClient, company: World, login: Login
) -> None:
    company.grant("case_file", "carol")
    url = f"/documents/{company.documents['case_file'].id}"

    assert client.get(url, headers=login["carol"]).status_code == 200
    for user in company.users:
        if user != "carol":
            assert client.get(url, headers=login[user]).status_code == 404, user
