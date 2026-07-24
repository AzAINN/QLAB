"""Market-stream supervision: latest-value cache, freshness, reconnect (P2)."""

from __future__ import annotations

import pytest

from qlab.data.stream import (
    DISCONNECTED,
    LIVE,
    MarketStreamSupervisor,
    RECONNECTING,
    STALE,
    backoff_delays,
)


class _Clock:
    """A manually advanced monotonic clock for deterministic freshness tests."""

    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _sup(**kw):
    events = []
    clock = _Clock()
    sup = MarketStreamSupervisor(
        ["ACWI", "BNDW"], "iex", stale_after_s=5.0, clock=clock,
        on_event=events.append, **kw)
    return sup, clock, events


def test_rejects_invalid_feed_and_empty_symbols():
    with pytest.raises(ValueError, match="invalid feed"):
        MarketStreamSupervisor(["ACWI"], "nasdaq")
    with pytest.raises(ValueError, match="at least one symbol"):
        MarketStreamSupervisor([], "iex")


def test_latest_value_cache_updates_on_quote():
    sup, clock, _ = _sup()
    sup.mark_connected()
    sup.on_quote("ACWI", 101.5)
    snap = sup.latest("ACWI")
    assert snap.price == 101.5 and snap.feed == "iex"
    assert sup.latest("BNDW") is None


def test_quotes_fresh_requires_live_and_all_symbols_fresh():
    sup, clock, _ = _sup()
    assert not sup.quotes_fresh()          # disconnected
    sup.mark_connected()
    sup.on_quote("ACWI", 100.0)
    assert not sup.quotes_fresh()          # BNDW still missing
    sup.on_quote("BNDW", 50.0)
    assert sup.quotes_fresh()
    clock.advance(6.0)                     # both quotes age past stale_after_s
    assert not sup.quotes_fresh()


def test_health_reports_stale_when_live_but_quotes_aged():
    sup, clock, _ = _sup()
    sup.mark_connected()
    sup.on_quote("ACWI", 100.0)
    sup.on_quote("BNDW", 50.0)
    assert sup.health()["state"] == LIVE
    clock.advance(6.0)
    h = sup.health()
    assert h["state"] == STALE
    assert set(h["stale_symbols"]) == {"ACWI", "BNDW"}
    assert h["feed"] == "iex" and h["fresh"] is False


def test_state_transitions_emit_events_but_quotes_do_not_flood():
    sup, clock, events = _sup()
    sup.mark_connecting()
    sup.mark_connected()
    # First full fill is one stale->fresh transition; then a 20-quote burst at
    # steady freshness must emit nothing (the anti-flood contract).
    sup.on_quote("ACWI", 100.0)
    sup.on_quote("BNDW", 50.0)
    baseline = len(events)
    for i in range(20):
        sup.on_quote("ACWI", 100.0 + i)
        sup.on_quote("BNDW", 50.0 + i)
    assert len(events) == baseline
    assert [e["kind"] for e in events] == [
        "stream_state", "stream_state", "stream_fresh"]


def test_reconnect_increments_and_recovery_emits_event():
    sup, clock, events = _sup()
    sup.mark_connected()
    sup.on_quote("ACWI", 100.0)
    sup.on_quote("BNDW", 50.0)
    clock.advance(6.0)                       # go stale
    sup.mark_reconnecting("socket closed")
    assert sup.health()["reconnects"] == 1
    assert sup.health()["state"] == RECONNECTING
    # A fresh quote after staleness emits a single recovery event and relives.
    events.clear()
    sup.on_quote("ACWI", 101.0)
    sup.on_quote("BNDW", 51.0)
    assert any(e["kind"] == "stream_fresh" for e in events)
    assert sup.quotes_fresh()


def test_backoff_schedule_is_bounded_and_deterministic():
    delays = []
    it = backoff_delays(base=0.5, cap=8.0)
    for _ in range(8):
        delays.append(next(it))
    assert delays[:5] == [0.5, 1.0, 2.0, 4.0, 8.0]
    assert all(d <= 8.0 for d in delays)     # capped

    # Jitter only ever lengthens a delay, never shortens it below the base.
    jittered = next(backoff_delays(base=1.0, cap=10.0, jitter=lambda n: 0.25))
    assert jittered == pytest.approx(1.25)
