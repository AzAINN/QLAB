"""One lock around wait-and-stamp, shared by the rate-limited providers.

A provider's minimum interval was a bare module global: read, compared against
the clock, and rewritten with no synchronization. The owner fetches its stack
from the heartbeat thread while a manual refresh can run on a handler thread,
so two fetchers could read the same stale stamp, both conclude no wait was due,
and burst past the publisher's limit -- the SEC's 10 requests/second, or the
non-JSON body GDELT answers a burst with.

Each provider keeps its own interval and its own instance: separate publishers
have separate budgets, and one shared throttle would make the SEC's 10/second
queue behind GDELT's one per second.
"""

from __future__ import annotations

import threading
import time
from typing import Callable


class Throttle:
    """Serializes callers to at most one call per ``min_interval_s``.

    The lock is held ACROSS the sleep, deliberately: releasing it to wait would
    let every queued caller compute its wait from the same stamp and then wake
    together, which is the burst this exists to prevent. The cost is that a
    provider's fetches are serialized, which is what a rate limit means anyway.

    ``clock`` and ``sleep`` are injected so the behaviour can be tested against
    a clock only sleeping advances, rather than against wall time.
    """

    def __init__(self, min_interval_s: float, *,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.min_interval_s = float(min_interval_s)
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last: float | None = None

    def wait(self) -> float:
        """Block until the next call is due, stamp it, and return the stamp."""
        with self._lock:
            if self._last is not None:
                due = self.min_interval_s - (self._clock() - self._last)
                if due > 0:
                    self._sleep(due)
            self._last = self._clock()
            return self._last
