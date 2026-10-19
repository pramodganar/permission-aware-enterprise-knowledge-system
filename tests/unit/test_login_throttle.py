"""Limiting failed logins per email."""

import pytest

from app.auth.throttle import LoginThrottle
from app.core.exceptions import TooManyRequestsError


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def throttle(clock: Clock) -> LoginThrottle:
    return LoginThrottle(max_failures=3, window_seconds=60, clock=clock)


def fail(throttle: LoginThrottle, email: str, times: int) -> None:
    for _ in range(times):
        throttle.check(email)
        throttle.failed(email)


def test_attempts_up_to_the_limit_are_allowed(throttle: LoginThrottle) -> None:
    fail(throttle, "a@x", 2)

    throttle.check("a@x")


def test_the_attempt_after_the_limit_is_refused(throttle: LoginThrottle) -> None:
    fail(throttle, "a@x", 3)

    with pytest.raises(TooManyRequestsError):
        throttle.check("a@x")


def test_the_refusal_says_when_to_try_again(throttle: LoginThrottle, clock: Clock) -> None:
    fail(throttle, "a@x", 1)
    clock.now += 20
    fail(throttle, "a@x", 2)

    with pytest.raises(TooManyRequestsError) as refused:
        throttle.check("a@x")

    # The oldest failure leaves the window 40 seconds from now.
    assert refused.value.retry_after == 40


def test_failures_older_than_the_window_stop_counting(
    throttle: LoginThrottle, clock: Clock
) -> None:
    fail(throttle, "a@x", 3)
    clock.now += 60

    throttle.check("a@x")


def test_the_window_slides_one_failure_at_a_time(throttle: LoginThrottle, clock: Clock) -> None:
    fail(throttle, "a@x", 1)
    clock.now += 30
    fail(throttle, "a@x", 2)
    clock.now += 30  # the first failure is now outside

    throttle.check("a@x")
    throttle.failed("a@x")
    with pytest.raises(TooManyRequestsError):
        throttle.check("a@x")


def test_a_success_clears_the_failures(throttle: LoginThrottle) -> None:
    fail(throttle, "a@x", 2)
    throttle.succeeded("a@x")
    fail(throttle, "a@x", 2)

    throttle.check("a@x")


def test_emails_are_counted_separately(throttle: LoginThrottle) -> None:
    fail(throttle, "a@x", 3)

    throttle.check("b@x")


def test_an_email_counts_the_same_however_it_is_typed(throttle: LoginThrottle) -> None:
    throttle.failed("Alice@AcmeCorp.example")
    throttle.failed("  alice@acmecorp.example ")
    throttle.failed("ALICE@ACMECORP.EXAMPLE")

    with pytest.raises(TooManyRequestsError):
        throttle.check("alice@acmecorp.example")


def test_the_number_of_emails_remembered_is_capped(clock: Clock) -> None:
    throttle = LoginThrottle(max_failures=1, window_seconds=60, clock=clock, max_tracked=2)

    throttle.failed("first@x")
    throttle.failed("second@x")
    throttle.failed("third@x")  # first is forgotten

    throttle.check("first@x")
    with pytest.raises(TooManyRequestsError):
        throttle.check("third@x")
    with pytest.raises(TooManyRequestsError):
        throttle.check("second@x")


def test_a_failure_makes_its_email_the_most_recent(clock: Clock) -> None:
    throttle = LoginThrottle(max_failures=2, window_seconds=60, clock=clock, max_tracked=2)

    throttle.failed("first@x")
    throttle.failed("second@x")
    throttle.failed("first@x")  # first is now the most recent
    throttle.failed("third@x")  # so second is forgotten

    with pytest.raises(TooManyRequestsError):
        throttle.check("first@x")
    throttle.check("second@x")


def test_an_email_with_no_recent_failures_is_not_kept(
    throttle: LoginThrottle, clock: Clock
) -> None:
    fail(throttle, "a@x", 1)
    clock.now += 61

    throttle.check("a@x")

    assert throttle._failures == {}
