"""How often one person may ask and search, through the API (threat T24).

The ``settings`` fixture lifts the limits so other tests can ask as often as
they like. Here they are put back to the service's defaults, or set lower
where a test needs it.
"""

import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from pydantic import SecretStr
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from apiclient import company_client, headers
from app.core.config import Settings
from app.core.rate_limit import RateLimiter, Window
from app.models import AuditRecord
from fakes import FakeLLM
from logcheck import everything_logged

LEAVE = {"question": "How many days of annual leave do I get?"}
BONUSES = {"question": "How are executive bonuses calculated?"}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def defaults(settings: Settings) -> Settings:
    """The test settings with the limits the service ships with."""
    shipped = Settings(_env_file=None, jwt_secret_key=SecretStr("d" * 64))
    return settings.model_copy(
        update={
            "ask_limit_per_minute": shipped.ask_limit_per_minute,
            "ask_limit_per_day": shipped.ask_limit_per_day,
            "search_limit_per_minute": shipped.search_limit_per_minute,
        }
    )


@pytest.fixture
def client(db: Session, engine: Engine, defaults: Settings) -> Iterator[TestClient]:
    with company_client(db, engine, defaults, FakeLLM(["Twenty-five days [S1]."])) as client:
        yield client


def ask(client: TestClient, auth: dict[str, str], body: object = LEAVE) -> Response:
    return client.post("/ask", headers=auth, json=body)


def until_refused(client: TestClient, path: str, auth: dict[str, str], body: object) -> int:
    """How many calls are taken before the first 429; fails past 1000."""
    for taken in range(1000):
        if client.post(path, headers=auth, json=body).status_code == 429:
            return taken
    raise AssertionError("never limited")


def test_the_service_ships_with_ten_questions_a_minute_two_hundred_a_day_and_thirty_searches(
    defaults: Settings,
) -> None:
    assert defaults.ask_limit_per_minute == 10
    assert defaults.ask_limit_per_day == 200
    assert defaults.search_limit_per_minute == 30


def test_the_eleventh_question_in_a_minute_is_refused_with_when_to_try_again(
    client: TestClient,
) -> None:
    auth = headers(client, "david")
    for _ in range(10):
        assert ask(client, auth).status_code == 200

    refused = ask(client, auth)

    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "too_many_requests"
    assert refused.json()["error"]["message"] == "Too many attempts. Try again later."
    assert 1 <= int(refused.headers["Retry-After"]) <= 60


def test_the_thirty_first_search_in_a_minute_is_refused(client: TestClient) -> None:
    assert until_refused(client, "/search", headers(client, "david"), LEAVE) == 30


def test_asking_and_searching_are_counted_separately(client: TestClient) -> None:
    auth = headers(client, "david")
    until_refused(client, "/ask", auth, LEAVE)

    assert client.post("/search", headers=auth, json=LEAVE).status_code == 200


def test_one_persons_limit_is_their_own(client: TestClient) -> None:
    until_refused(client, "/ask", headers(client, "david"), LEAVE)

    assert ask(client, headers(client, "frank")).status_code == 200


def test_logging_in_again_does_not_start_a_new_count(client: TestClient) -> None:
    until_refused(client, "/ask", headers(client, "david"), LEAVE)

    assert ask(client, headers(client, "david")).status_code == 429


def test_a_question_about_what_you_may_not_read_counts_like_any_other(
    client: TestClient,
) -> None:
    # Alice may not read the executive documents: those questions get no
    # information without the model being asked. They are counted all the same,
    # so where the limit falls says nothing about what she may read.
    auth = headers(client, "alice")
    for n in range(10):
        assert ask(client, auth, BONUSES if n % 2 else LEAVE).status_code == 200

    assert ask(client, auth, BONUSES).status_code == 429
    assert ask(client, auth, LEAVE).status_code == 429


def test_a_call_without_a_valid_token_is_refused_first_and_counts_for_nobody(
    client: TestClient,
) -> None:
    for _ in range(20):
        no_token = client.post("/ask", json=LEAVE)
        assert no_token.status_code == 401
        bad_token = ask(client, {"Authorization": "Bearer not-a-token"})
        assert bad_token.status_code == 401

    assert until_refused(client, "/ask", headers(client, "david"), LEAVE) == 10


def test_the_limit_is_checked_before_the_body_and_a_bad_body_counts_too(
    client: TestClient,
) -> None:
    auth = headers(client, "david")
    for _ in range(10):
        assert ask(client, auth, {"question": ""}).status_code == 422

    assert ask(client, auth, {"question": ""}).status_code == 429
    assert ask(client, auth).status_code == 429


def test_waiting_as_long_as_the_refusal_says_is_enough(client: TestClient) -> None:
    clock = Clock()
    client.app.state.ask_limiter = RateLimiter(  # type: ignore[attr-defined]
        [Window(2, 60), Window(200, 86_400)], clock=clock
    )
    auth = headers(client, "david")
    ask(client, auth)
    clock.now += 15
    ask(client, auth)

    wait = int(ask(client, auth).headers["Retry-After"])
    assert wait == 45
    clock.now += wait - 1
    assert ask(client, auth).status_code == 429
    clock.now += 1
    assert ask(client, auth).status_code == 200


def test_the_daily_limit_holds_however_the_questions_are_spread(
    db: Session, engine: Engine, defaults: Settings
) -> None:
    limited = defaults.model_copy(update={"ask_limit_per_minute": 3, "ask_limit_per_day": 7})
    clock = Clock()
    with company_client(db, engine, limited, FakeLLM(["Twenty-five days [S1]."])) as client:
        client.app.state.ask_limiter = RateLimiter(  # type: ignore[attr-defined]
            [Window(3, 60), Window(7, 86_400)], clock=clock
        )
        auth = headers(client, "david")
        answered = 0
        for _ in range(48):  # one question every half hour for a day
            answered += ask(client, auth).status_code == 200
            clock.now += 1800

    assert answered == 7


def test_the_limits_come_from_the_settings(db: Session, engine: Engine, defaults: Settings) -> None:
    lower = defaults.model_copy(update={"ask_limit_per_minute": 2, "search_limit_per_minute": 4})
    with company_client(db, engine, lower, FakeLLM(["Twenty-five days [S1]."])) as client:
        auth = headers(client, "david")

        assert until_refused(client, "/ask", auth, LEAVE) == 2
        assert until_refused(client, "/search", auth, LEAVE) == 4


def test_the_daily_limit_comes_from_the_settings(
    db: Session, engine: Engine, defaults: Settings
) -> None:
    lower = defaults.model_copy(update={"ask_limit_per_minute": 10, "ask_limit_per_day": 4})
    with company_client(db, engine, lower, FakeLLM(["Twenty-five days [S1]."])) as client:
        auth = headers(client, "david")

        assert until_refused(client, "/ask", auth, LEAVE) == 4
        # Refused by the day, not the minute: the wait is close to a day.
        assert int(ask(client, auth).headers["Retry-After"]) > 86_000


def test_a_limited_call_is_logged_with_who_and_where_and_not_audited(
    client: TestClient, db: Session, caplog: pytest.LogCaptureFixture
) -> None:
    auth = headers(client, "david")
    until_refused(client, "/ask", auth, LEAVE)
    before = db.scalar(select(func.count()).select_from(AuditRecord))
    secret = {"question": "What does the Zanzibar contract say?"}
    caplog.clear()

    with caplog.at_level(logging.DEBUG, logger="app"):
        assert ask(client, auth, secret).status_code == 429

    assert db.scalar(select(func.count()).select_from(AuditRecord)) == before
    limited = [r for r in caplog.records if r.getMessage() == "request limited"]
    assert len(limited) == 1
    assert limited[0].route == "/ask"  # type: ignore[attr-defined]
    assert isinstance(limited[0].user_id, int)  # type: ignore[attr-defined]
    assert "Zanzibar" not in everything_logged(caplog)
