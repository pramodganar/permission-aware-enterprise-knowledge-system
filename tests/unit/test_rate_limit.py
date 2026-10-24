"""Limiting how often one key can do something, in sliding windows."""

import contextlib
import sys
import threading
from collections.abc import Iterator

import pytest

from app.core.exceptions import TooManyRequestsError
from app.core.rate_limit import RateLimiter, Window


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def limiter(clock: Clock) -> RateLimiter:
    return RateLimiter([Window(3, 60)], clock=clock)


def take(limiter: RateLimiter, key: object, times: int) -> None:
    for _ in range(times):
        limiter.take(key)


def refused(limiter: RateLimiter, key: object) -> int:
    """The Retry-After a refused call gets; fails if the call is taken."""
    with pytest.raises(TooManyRequestsError) as error:
        limiter.take(key)
    return error.value.retry_after


def test_calls_up_to_the_limit_are_taken_and_the_next_is_refused(limiter: RateLimiter) -> None:
    take(limiter, "a", 3)

    refused(limiter, "a")


def test_the_refusal_says_how_long_to_wait_and_waiting_that_long_is_enough(
    limiter: RateLimiter, clock: Clock
) -> None:
    take(limiter, "a", 1)
    clock.now += 10.5
    take(limiter, "a", 2)

    wait = refused(limiter, "a")
    # The first call leaves the window 49.5 s from now: rounded up, never down.
    assert wait == 50
    clock.now += wait - 1
    refused(limiter, "a")
    clock.now += 1
    limiter.take("a")


def test_the_window_slides_one_call_at_a_time(limiter: RateLimiter, clock: Clock) -> None:
    for _ in range(3):
        limiter.take("a")
        clock.now += 10
    refused(limiter, "a")

    clock.now += 30  # the first call, at 1000, is now outside
    limiter.take("a")
    refused(limiter, "a")


def test_a_refused_call_is_not_counted(limiter: RateLimiter, clock: Clock) -> None:
    take(limiter, "a", 3)
    for _ in range(50):
        clock.now += 1
        refused(limiter, "a")

    # Hammering while refused didn't push the wait further away.
    clock.now += 10
    limiter.take("a")


def test_a_call_refused_by_one_window_uses_nothing_in_the_others(clock: Clock) -> None:
    limiter = RateLimiter([Window(1, 60), Window(3, 1000)], clock=clock)

    limiter.take("a")
    for _ in range(5):
        clock.now += 1
        refused(limiter, "a")  # by the first window
    clock.now += 60
    limiter.take("a")
    clock.now += 60
    # The third call in the long window: only taken if the refusals weren't counted there.
    limiter.take("a")
    clock.now += 60
    refused(limiter, "a")


def test_the_wait_is_for_every_window_to_have_room(clock: Clock) -> None:
    limiter = RateLimiter([Window(1, 60), Window(2, 1000)], clock=clock)
    limiter.take("a")
    clock.now += 100
    limiter.take("a")

    # The short window has room in 60 s, the long one in 900 s.
    assert refused(limiter, "a") == 900
    clock.now += 899
    refused(limiter, "a")
    clock.now += 1
    limiter.take("a")


def test_keys_are_counted_separately(limiter: RateLimiter) -> None:
    take(limiter, 1, 3)
    refused(limiter, 1)

    take(limiter, 2, 3)


def test_at_most_max_tracked_keys_are_remembered_the_least_recent_forgotten(
    clock: Clock,
) -> None:
    limiter = RateLimiter([Window(1, 60)], clock=clock, max_tracked=2)
    limiter.take("first")
    limiter.take("second")
    refused(limiter, "first")  # first is now the most recent
    limiter.take("third")  # so second is forgotten

    refused(limiter, "first")
    refused(limiter, "third")
    limiter.take("second")
    assert len(limiter._calls) == 2


def test_no_key_holds_more_times_than_the_longest_windows_limit(clock: Clock) -> None:
    limiter = RateLimiter([Window(5, 10), Window(20, 3600)], clock=clock)

    for _ in range(1000):
        with contextlib.suppress(TooManyRequestsError):
            limiter.take("a")
        clock.now += 1

    assert len(limiter._calls["a"]) <= 20


def test_a_key_whose_calls_have_all_expired_takes_a_full_window(
    limiter: RateLimiter, clock: Clock
) -> None:
    take(limiter, "a", 3)
    clock.now += 60

    take(limiter, "a", 3)
    refused(limiter, "a")


def test_a_call_exactly_one_short_window_old_no_longer_counts_in_it(clock: Clock) -> None:
    limiter = RateLimiter([Window(1, 60), Window(5, 1000)], clock=clock)
    limiter.take("a")
    clock.now += 60

    limiter.take("a")


@pytest.fixture
def switching_often() -> Iterator[None]:
    # Threads switch every microsecond instead of every 5 ms, so a take that
    # isn't atomic is interrupted part way.
    before = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    yield
    sys.setswitchinterval(before)


@pytest.mark.usefixtures("switching_often")
@pytest.mark.parametrize("attempt", range(20))
def test_calls_at_the_same_moment_from_many_threads_take_exactly_the_limit(attempt: int) -> None:
    limiter = RateLimiter([Window(10, 3600), Window(10, 7200)])
    start = threading.Barrier(50)
    taken: list[bool] = []

    def call() -> None:
        start.wait()
        try:
            limiter.take("a")
            taken.append(True)
        except TooManyRequestsError:
            taken.append(False)

    threads = [threading.Thread(target=call) for _ in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert taken.count(True) == 10
    assert taken.count(False) == 40


@pytest.mark.parametrize("windows", [[], [Window(0, 60)], [Window(1, 0)], [Window(1, -5)]])
def test_a_window_that_could_never_take_a_call_is_refused(windows: list[Window]) -> None:
    with pytest.raises(ValueError):
        RateLimiter(windows)
