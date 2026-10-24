"""Limiting how often one key can do something (threats T24 and T31).

    limiter = RateLimiter([Window(10, 60), Window(200, 86_400)])
    limiter.take(user.id)   # raises TooManyRequestsError over either limit

Each window is a sliding one: at most ``limit`` calls in any ``seconds``.
``take`` checks every window and, only if all of them have room, counts the
call in all of them at once. So a call refused by one window uses up
nothing in the others, and a refused call is never counted: retrying while
refused doesn't push the wait further away.

The refusal says how long until a call would be taken, rounded up, so a
client that waits that long is never refused again for the same reason.

What it doesn't do, stated plainly:

- It is in memory, per process, like the login throttle. Two copies of the
  service count separately and a restart forgets. A shared store such as
  Redis would fix both.
- It counts calls, not cost. A question that makes the model write a long
  answer counts the same as one answered from nothing.

At most ``max_tracked`` keys are remembered, the one not seen for longest
forgotten first, and no key holds more times than the longest window's
limit. So memory is bounded whatever the keys are.
"""

import math
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Hashable, Sequence
from typing import NamedTuple

from app.core.exceptions import TooManyRequestsError

MAX_TRACKED = 10_000


class Window(NamedTuple):
    limit: int
    seconds: float


class RateLimiter:
    def __init__(
        self,
        windows: Sequence[Window],
        *,
        clock: Callable[[], float] = time.monotonic,
        max_tracked: int = MAX_TRACKED,
    ) -> None:
        if not windows or any(w.limit < 1 or w.seconds <= 0 for w in windows):
            raise ValueError("every window needs a limit of at least 1 and a positive length")
        self.windows = tuple(windows)
        self._longest = max(w.seconds for w in self.windows)
        self._clock = clock
        self._max_tracked = max_tracked
        self._calls: OrderedDict[Hashable, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def take(self, key: Hashable) -> None:
        """Count one call for this key, or raise TooManyRequestsError and count nothing."""
        with self._lock:
            now = self._clock()
            calls = self._calls.pop(key, deque())
            while calls and calls[0] <= now - self._longest:
                calls.popleft()
            wait = max(self._wait(calls, window, now) for window in self.windows)
            if wait > 0:
                self._remember(key, calls)
                raise TooManyRequestsError("over a rate limit", retry_after=math.ceil(wait))
            calls.append(now)
            self._remember(key, calls)

    def _wait(self, calls: deque[float], window: Window, now: float) -> float:
        """Seconds until this window has room for one more call; 0 if it has now."""
        inside = [t for t in calls if t > now - window.seconds]
        if len(inside) < window.limit:
            return 0.0
        # The call whose expiry leaves limit - 1 inside.
        return inside[len(inside) - window.limit] + window.seconds - now

    def _remember(self, key: Hashable, calls: deque[float]) -> None:
        self._calls[key] = calls
        while len(self._calls) > self._max_tracked:
            self._calls.popitem(last=False)
