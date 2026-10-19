"""Limiting failed logins, so passwords can't be guessed at speed (threat T10).

After ``max_failures`` failed logins for one email within the window, further
attempts for that email are refused with 429 until the oldest failure is
older than the window. A refused attempt doesn't check the password at all.

Attempts are counted per email as it was typed, normalised, whether or not
an account has that email. So the limit says nothing about which emails
exist: an unknown email is limited exactly like a real one.

What it doesn't do, stated plainly:

- It is in memory, per process. Two copies of the service count separately,
  and a restart forgets. A shared store such as Redis would fix both.
- It counts per email, not per client. One client trying one password on many
  emails isn't slowed. Per-client limits need the client's real address,
  which behind a proxy is a trust question of its own.
- Someone who knows a colleague's email can lock them out for the window by
  failing on purpose. The window is short for that reason.

At most ``max_tracked`` emails are remembered. When there are more, the one
not seen for longest is forgotten, so a flood of made-up emails can't use up
memory.
"""

import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable

from app.core.exceptions import TooManyRequestsError

MAX_TRACKED = 10_000


class LoginThrottle:
    def __init__(
        self,
        max_failures: int,
        window_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_tracked: int = MAX_TRACKED,
    ) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._clock = clock
        self._max_tracked = max_tracked
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def check(self, email: str) -> None:
        """Raise TooManyRequestsError if this email has failed too often lately."""
        with self._lock:
            recent = self._recent(_key(email))
            if len(recent) >= self.max_failures:
                wait = recent[0] + self.window_seconds - self._clock()
                raise TooManyRequestsError(
                    "too many failed logins for one email", retry_after=max(1, round(wait))
                )

    def failed(self, email: str) -> None:
        with self._lock:
            key = _key(email)
            recent = self._recent(key)
            recent.append(self._clock())
            self._failures[key] = recent
            self._failures.move_to_end(key)
            while len(self._failures) > self._max_tracked:
                self._failures.popitem(last=False)

    def succeeded(self, email: str) -> None:
        with self._lock:
            self._failures.pop(_key(email), None)

    def _recent(self, key: str) -> deque[float]:
        """The failures for this key still inside the window, oldest first."""
        recent = self._failures.get(key, deque())
        cutoff = self._clock() - self.window_seconds
        while recent and recent[0] <= cutoff:
            recent.popleft()
        if not recent:
            self._failures.pop(key, None)
        return recent


def _key(email: str) -> str:
    # The same normalising as the lookup in authenticate().
    return email.strip().lower()
