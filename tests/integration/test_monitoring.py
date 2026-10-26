"""The monitor reading what the running service writes (docs/monitoring.md).

The unit tests hold each rule to its threshold on lines made for them; these
hold the rules to the lines the service really writes. Ordinary use fires
nothing, and each kind of misuse fires its rule and only its rule.
"""

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from apiclient import company_client, email, headers
from app.core.config import Settings
from app.core.logging_setup import configure_logging
from logcheck import logging_restored
from scripts import monitor

QUESTIONS = [
    "How many days of annual leave do I get?",
    "What is the hotel limit when travelling?",
    "How do I report a security incident?",
]
# The HR case file: restricted, readable by Carol, not by David.
RESTRICTED = 16


@pytest.fixture
def service(
    db: Session, engine: Engine, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> Iterator[tuple[TestClient, Callable[[], monitor.Report]]]:
    """The service, and the monitor's report on everything it has written so far.

    Logging is set up by ``ordinary_use``, in the test: the output pytest
    captures during setup is closed by then.
    """
    written: list[str] = []
    with logging_restored(), company_client(db, engine, settings) as client:

        def report() -> monitor.Report:
            written.extend(capsys.readouterr().out.splitlines())
            return monitor.check(monitor.read(written))

        yield client, report


def ordinary_use(client: TestClient) -> None:
    configure_logging("INFO")
    for name in ("david", "carol", "emma"):
        auth = headers(client, name)
        client.get("/documents", headers=auth)
        for question in QUESTIONS:
            client.post("/search", json={"question": question}, headers=auth)


def fired(report: monitor.Report) -> list[str]:
    return [checked.rule.name for checked in report.fired]


def test_ordinary_use_fires_nothing_and_every_request_is_counted(
    service: tuple[TestClient, Callable[[], monitor.Report]],
) -> None:
    client, report = service
    ordinary_use(client)

    found = report()
    rows = [row.split() for row in monitor.routes(found.window)]
    routes = {f"{row[0]} {row[1]}": int(row[2]) for row in rows}

    assert fired(found) == []
    assert found.window.not_json == 0
    assert routes == {"POST /search": 9, "POST /auth/login": 3, "GET /documents": 3}


def test_guessing_passwords_fires_its_rule(
    service: tuple[TestClient, Callable[[], monitor.Report]],
) -> None:
    client, report = service
    ordinary_use(client)
    # Past the login throttle as well: a refused attempt is still a failed login.
    for n in range(20):
        client.post("/auth/login", json={"email": email("frank"), "password": f"guess-{n}"})

    assert fired(report()) == ["logins-failing"]


def test_asking_for_a_restricted_document_again_and_again_fires_its_rule(
    service: tuple[TestClient, Callable[[], monitor.Report]],
) -> None:
    client, report = service
    ordinary_use(client)
    auth = headers(client, "david")
    for _ in range(10):
        assert client.get(f"/documents/{RESTRICTED}", headers=auth).status_code == 404
    # A document that doesn't exist is a typo, not a probe.
    for _ in range(10):
        client.get("/documents/999", headers=auth)

    found = report()
    probed = next(c for c in found.checked if c.rule.name == "documents-probed")

    assert fired(found) == ["documents-probed"]
    assert probed.value == 10


def test_trying_actions_without_the_permission_fires_its_rule(
    service: tuple[TestClient, Callable[[], monitor.Report]],
) -> None:
    client, report = service
    ordinary_use(client)
    auth = headers(client, "david")
    for _ in range(10):
        assert client.get("/admin/documents/1", headers=auth).status_code == 403

    assert fired(report()) == ["actions-denied"]
