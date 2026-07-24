"""Market-data stream supervision: latest-value cache, health, reconnect.

The supervisor is deliberately transport-agnostic. It owns the *policy* — a
latest-value cache, quote-freshness, connection health, feed identity, and a
bounded exponential-reconnect schedule — while the concrete Alpaca websocket is
injected as a client with ``subscribe``/``run``/``stop``. That seam lets the
whole reconnect-and-freshness contract be tested with a fake client, offline.

Hard rules (plan §6.5):
* callbacks NEVER write the registry — the supervisor only mutates in-memory
  state and hands throttled snapshots to an owner-supplied ``on_event`` sink;
* no per-tick persistence — only connection/stale transitions are eventful;
* a stale quote is visible and blocks execution — ``quotes_fresh`` is the gate.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

_VALID_FEEDS = ("iex", "sip", "delayed_sip")

# Connection/health states.
DISCONNECTED = "disconnected"
CONNECTING = "connecting"
LIVE = "live"
STALE = "stale"
RECONNECTING = "reconnecting"


@dataclass(frozen=True)
class QuoteSnapshot:
    symbol: str
    price: float
    ts: float          # seconds on the supervisor's clock
    feed: str

    def age(self, now: float) -> float:
        return max(0.0, now - self.ts)


def backoff_delays(
    *, base: float = 0.5, cap: float = 30.0, jitter: Callable[[int], float] | None = None,
) -> Iterable[float]:
    """Yield an unbounded bounded-exponential reconnect schedule.

    ``delay(n) = min(cap, base * 2**n) * (1 + jitter(n))``. ``jitter`` defaults
    to none (deterministic) so tests are reproducible; production injects a
    small random positive jitter to avoid thundering-herd reconnects.
    """
    attempt = 0
    while True:
        raw = min(cap, base * (2 ** attempt))
        extra = jitter(attempt) if jitter is not None else 0.0
        yield raw * (1.0 + max(0.0, extra))
        attempt += 1


class MarketStreamSupervisor:
    """Owns the latest-value cache, feed identity, and connection health."""

    def __init__(
        self,
        symbols: list[str],
        feed: str,
        *,
        stale_after_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        on_event: Callable[[dict], None] | None = None,
    ):
        feed = (feed or "").strip().lower()
        if feed not in _VALID_FEEDS:
            raise ValueError(f"invalid feed {feed!r}; expected one of {_VALID_FEEDS}")
        if not symbols:
            raise ValueError("stream requires at least one symbol")
        self.symbols = list(symbols)
        self.feed = feed
        self.stale_after_s = float(stale_after_s)
        self._clock = clock
        self._on_event = on_event
        self._lock = threading.RLock()
        self._latest: dict[str, QuoteSnapshot] = {}
        self._state = DISCONNECTED
        self._reconnects = 0
        self._last_error: str | None = None

    # -- connection lifecycle (driven by the client adapter) ----------------
    def mark_connecting(self) -> None:
        self._transition(CONNECTING)

    def mark_connected(self) -> None:
        self._transition(LIVE)

    def mark_reconnecting(self, error: str | None = None) -> None:
        with self._lock:
            self._reconnects += 1
            self._last_error = error
        self._transition(RECONNECTING)

    def mark_disconnected(self, error: str | None = None) -> None:
        with self._lock:
            self._last_error = error
        self._transition(DISCONNECTED)

    def _transition(self, state: str) -> None:
        with self._lock:
            if state == self._state:
                return
            self._state = state
            event = {"kind": "stream_state", "state": state, "feed": self.feed,
                     "reconnects": self._reconnects, "error": self._last_error}
        self._emit(event)

    # -- quote ingestion (callback; NEVER writes the registry) --------------
    def on_quote(self, symbol: str, price: float, ts: float | None = None) -> None:
        now = self._clock() if ts is None else float(ts)
        snap = QuoteSnapshot(symbol=symbol, price=float(price), ts=now, feed=self.feed)
        with self._lock:
            was_stale = self._is_stale_locked(self._clock())
            self._latest[symbol] = snap
            if self._state in (RECONNECTING, CONNECTING):
                self._state = LIVE
            became_fresh = was_stale and not self._is_stale_locked(self._clock())
        # A single event on the stale->fresh transition (first full fill or a
        # recovery); per-tick quotes at steady freshness emit nothing.
        if became_fresh:
            self._emit({"kind": "stream_fresh", "feed": self.feed})

    def latest(self, symbol: str) -> QuoteSnapshot | None:
        with self._lock:
            return self._latest.get(symbol)

    def snapshot(self) -> dict[str, QuoteSnapshot]:
        with self._lock:
            return dict(self._latest)

    # -- freshness & health --------------------------------------------------
    def _stale_symbols_locked(self, now: float) -> list[str]:
        stale = []
        for symbol in self.symbols:
            snap = self._latest.get(symbol)
            if snap is None or snap.age(now) > self.stale_after_s:
                stale.append(symbol)
        return stale

    def _is_stale_locked(self, now: float) -> bool:
        return bool(self._stale_symbols_locked(now))

    def quotes_fresh(self, now: float | None = None) -> bool:
        """True only when the stream is LIVE and every symbol has a fresh quote.

        This is the execution gate: a stale or missing quote for any subscribed
        symbol refuses execution rather than pricing an order on a stale book.
        """
        now = self._clock() if now is None else now
        with self._lock:
            return self._state == LIVE and not self._stale_symbols_locked(now)

    def health(self, now: float | None = None) -> dict:
        now = self._clock() if now is None else now
        with self._lock:
            stale = self._stale_symbols_locked(now)
            ages = {s: round(self._latest[s].age(now), 3)
                    for s in self._latest}
            state = self._state
            # A LIVE connection with stale quotes is surfaced as STALE.
            if state == LIVE and stale:
                state = STALE
            return {
                "state": state,
                "feed": self.feed,
                "connected": self._state in (LIVE, STALE),
                "symbols": list(self.symbols),
                "stale_symbols": stale,
                "quote_ages": ages,
                "reconnects": self._reconnects,
                "fresh": state == LIVE and not stale,
                "last_error": self._last_error,
            }

    def _emit(self, event: dict) -> None:
        if self._on_event is not None:
            self._on_event(event)


def build_alpaca_market_stream(
    symbols: list[str],
    feed: str,
    *,
    on_event: Callable[[dict], None] | None = None,
    stale_after_s: float = 5.0,
) -> MarketStreamSupervisor:
    """Construct a supervisor for a real Alpaca feed (the operational runtime).

    The concrete websocket is attached by :func:`run_alpaca_market_stream`; this
    only builds the policy object so the owner can hold it before the socket is
    live. IEX and SIP are distinct entitlements and never collapsed to "live".
    """
    return MarketStreamSupervisor(
        symbols, feed, stale_after_s=stale_after_s, on_event=on_event)


def run_alpaca_market_stream(  # pragma: no cover - requires a live Alpaca socket
    supervisor: MarketStreamSupervisor,
    *,
    key: str,
    secret: str,
    stop_event=None,
) -> None:
    """Drive ``supervisor`` from Alpaca's websocket with bounded reconnects.

    Not exercised by the offline suite (it needs a live socket); the freshness,
    health, and reconnect *policy* it relies on is fully tested through
    :class:`MarketStreamSupervisor` and :func:`backoff_delays` with a fake feed.
    """
    import threading as _threading

    try:
        from alpaca.data.enums import DataFeed
        from alpaca.data.live import StockDataStream
    except ImportError as exc:
        raise RuntimeError(
            "alpaca-py is required for the live market stream; install qlab[trader]"
        ) from exc

    stop_event = stop_event or _threading.Event()
    feed_enum = {
        "iex": DataFeed.IEX, "sip": DataFeed.SIP,
        "delayed_sip": getattr(DataFeed, "DELAYED_SIP", DataFeed.SIP),
    }[supervisor.feed]
    delays = backoff_delays(base=0.5, cap=30.0)

    async def _on_quote(quote) -> None:
        price = float(getattr(quote, "ask_price", 0.0) or getattr(quote, "bid_price", 0.0))
        supervisor.on_quote(str(quote.symbol), price)

    while not stop_event.is_set():
        supervisor.mark_connecting()
        client = StockDataStream(key, secret, feed=feed_enum)
        try:
            client.subscribe_quotes(_on_quote, *supervisor.symbols)
            supervisor.mark_connected()
            client.run()  # blocks until the socket drops
        except Exception as exc:  # reconnect with bounded backoff
            supervisor.mark_reconnecting(str(exc))
            if stop_event.wait(next(delays)):
                break
        else:
            supervisor.mark_disconnected()
            break
