"""The shared provider throttle: one lock around wait-and-stamp.

edgar and gdelt each held a bare module global read, compared and rewritten
without synchronization. The owner fetches a stack from its heartbeat while a
manual refresh runs on a handler thread, so two fetchers could read the same
stale stamp, both decide no wait was due, and burst straight through a
publisher's rate limit -- the SEC's 10/second, or GDELT's non-JSON error body.
"""

from __future__ import annotations

import threading

from qlab.news.providers._throttle import Throttle


class FakeClock:
    """A clock only sleeping advances: no wall time, no flake."""

    def __init__(self) -> None:
        self.now = 0.0
        self.lock = threading.Lock()

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        with self.lock:
            self.now += seconds


def test_two_threads_through_one_throttle_never_interleave_the_stamp():
    clock = FakeClock()
    throttle = Throttle(0.5, clock=clock.monotonic, sleep=clock.sleep)
    stamps: list[float] = []
    stamp_lock = threading.Lock()
    start = threading.Barrier(4)

    def call() -> None:
        start.wait()
        for _ in range(3):
            stamp = throttle.wait()
            with stamp_lock:
                stamps.append(stamp)

    threads = [threading.Thread(target=call) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(stamps) == 12
    ordered = sorted(stamps)
    assert ordered == stamps          # no thread stamped out of turn
    # The first call is free; every later one is spaced by the full interval.
    gaps = [b - a for a, b in zip(ordered, ordered[1:])]
    assert all(gap >= 0.5 - 1e-9 for gap in gaps), gaps


def test_the_first_call_does_not_wait():
    clock = FakeClock()
    throttle = Throttle(1.0, clock=clock.monotonic, sleep=clock.sleep)
    assert throttle.wait() == 0.0
    assert clock.now == 0.0


def test_each_provider_module_owns_one_throttle_at_its_own_interval():
    from qlab.news.providers import edgar, gdelt

    assert isinstance(edgar._THROTTLE, Throttle)
    assert isinstance(gdelt._THROTTLE, Throttle)
    assert edgar._THROTTLE.min_interval_s == edgar._MIN_INTERVAL_S
    assert gdelt._THROTTLE.min_interval_s == gdelt._MIN_INTERVAL_S
    # Separate publishers, separate budgets: one shared throttle would make the
    # SEC's 10/second wait behind GDELT's one-per-second.
    assert edgar._THROTTLE is not gdelt._THROTTLE
