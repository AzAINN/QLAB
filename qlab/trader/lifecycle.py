"""Order lifecycle: an explicit, idempotent broker-order state machine.

A paper order is not "done" because the REST call returned — Alpaca acknowledges
first (``new``/``accepted``) and only later fills, partially fills, cancels,
rejects, or expires. Conflating acknowledgement with a fill would let the book
diverge from broker truth, so this module models the lifecycle explicitly:

    submitted -> accepted -> partially_filled -> filled        (terminal)
                          -> canceled | rejected | expired     (terminal)

The :class:`TradeUpdateSupervisor` is transport-agnostic (a fake feed drives it
in tests, the real Alpaca trade-update websocket in the operational runtime). It
NEVER writes the registry from a callback; it maps each update to a state
transition and hands it to an owner-supplied sink, preserving the one-writer
rule. Terminal states are sticky and duplicate/late updates are idempotent.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from qlab.data.stream import backoff_delays

# Order states (broker truth). ``submitted`` is our pre-ack state.
SUBMITTED = "submitted"
ACCEPTED = "accepted"
PARTIALLY_FILLED = "partially_filled"
FILLED = "filled"
CANCELED = "canceled"
REJECTED = "rejected"
EXPIRED = "expired"

TERMINAL = frozenset({FILLED, CANCELED, REJECTED, EXPIRED})

# Alpaca trade-update event -> our state.
_EVENT_TO_STATE = {
    "new": SUBMITTED,
    "accepted": ACCEPTED,
    "pending_new": SUBMITTED,
    "partial_fill": PARTIALLY_FILLED,
    "fill": FILLED,
    "canceled": CANCELED,
    "rejected": REJECTED,
    "expired": EXPIRED,
    "done_for_day": CANCELED,
}

# Rank orders states so a later, lower-rank update never regresses a higher one.
_RANK = {
    SUBMITTED: 0, ACCEPTED: 1, PARTIALLY_FILLED: 2,
    FILLED: 3, CANCELED: 3, REJECTED: 3, EXPIRED: 3,
}


@dataclass(frozen=True)
class OrderUpdate:
    client_order_id: str
    event: str
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    fee: float = 0.0


@dataclass
class OrderTransition:
    client_order_id: str
    state: str
    filled_qty: float
    filled_avg_price: float | None
    fee: float
    terminal: bool


@dataclass
class _LegState:
    state: str = SUBMITTED
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    fee: float = 0.0


class TradeUpdateSupervisor:
    """Maps broker order updates to idempotent plan-leg state transitions."""

    def __init__(self, on_transition: Callable[[OrderTransition], None] | None = None):
        self._on_transition = on_transition
        self._legs: dict[str, _LegState] = {}

    def register(self, client_order_id: str) -> None:
        """Track a submitted leg so late/duplicate updates resolve against it."""
        self._legs.setdefault(client_order_id, _LegState())

    def state(self, client_order_id: str) -> str | None:
        leg = self._legs.get(client_order_id)
        return leg.state if leg else None

    def on_update(self, update: OrderUpdate) -> OrderTransition | None:
        """Apply one update. Returns the transition, or None if it was a no-op.

        Idempotent: a terminal state is never regressed, and a repeated or
        out-of-order update that would not advance the state is ignored (so a
        duplicated ``fill`` never double-books).
        """
        target = _EVENT_TO_STATE.get(update.event)
        if target is None:
            raise ValueError(f"unknown trade-update event {update.event!r}")
        leg = self._legs.setdefault(update.client_order_id, _LegState())

        # A terminal state is sticky; nothing regresses or re-fires it.
        if leg.state in TERMINAL:
            return None
        # Never regress to an earlier lifecycle rank (late ACCEPTED after a
        # PARTIAL, say) — but always allow advancing to a terminal state.
        if _RANK[target] < _RANK[leg.state]:
            return None
        # A partial fill that does not advance the cumulative filled quantity is
        # a duplicate; ignore it so fills accumulate exactly once.
        if (target == PARTIALLY_FILLED and leg.state == PARTIALLY_FILLED
                and update.filled_qty <= leg.filled_qty + 1e-12):
            return None

        leg.state = target
        if update.filled_qty:
            leg.filled_qty = max(leg.filled_qty, float(update.filled_qty))
        if update.filled_avg_price is not None:
            leg.filled_avg_price = float(update.filled_avg_price)
        if update.fee:
            leg.fee += float(update.fee)

        transition = OrderTransition(
            client_order_id=update.client_order_id,
            state=leg.state,
            filled_qty=leg.filled_qty,
            filled_avg_price=leg.filled_avg_price,
            fee=leg.fee,
            terminal=leg.state in TERMINAL,
        )
        if self._on_transition is not None:
            self._on_transition(transition)
        return transition


def recover_from_broker(
    supervisor: TradeUpdateSupervisor,
    open_orders: dict,
    *,
    client_order_ids: list[str] | None = None,
) -> list[OrderTransition]:
    """Reconcile local order state against broker truth after a stream gap.

    While disconnected, a leg may have filled, been rejected, or expired
    without us hearing about it. This replays the venue's current state for
    each order through the same idempotent state machine, so recovery cannot
    double-book and cannot regress a terminal state. Orders the venue does not
    know about are left alone — absence is not evidence of a fill.
    """
    wanted = set(client_order_ids or open_orders)
    transitions: list[OrderTransition] = []
    for coid in sorted(wanted):
        broker_order = open_orders.get(coid)
        if broker_order is None:
            continue
        event = _BROKER_STATE_TO_EVENT.get(
            str(broker_order.get("state", "")).lower().split(".")[-1])
        if event is None:
            continue
        transition = supervisor.on_update(OrderUpdate(
            client_order_id=coid,
            event=event,
            filled_qty=float(broker_order.get("filled_qty") or 0.0),
            filled_avg_price=broker_order.get("filled_avg_price"),
        ))
        if transition is not None:
            transitions.append(transition)
    return transitions


# Alpaca order STATUS values (as opposed to trade-update EVENT names) mapped to
# the event vocabulary the state machine speaks.
_BROKER_STATE_TO_EVENT = {
    "new": "new",
    "accepted": "accepted",
    "pending_new": "pending_new",
    "partially_filled": "partial_fill",
    "filled": "fill",
    "canceled": "canceled",
    "cancelled": "canceled",
    "rejected": "rejected",
    "expired": "expired",
    "done_for_day": "done_for_day",
}


def run_alpaca_trade_updates(  # pragma: no cover - requires a live Alpaca socket
    supervisor: TradeUpdateSupervisor,
    *,
    key: str,
    secret: str,
    stop_event=None,
) -> None:
    """Drive ``supervisor`` from Alpaca's trade-update websocket (paper only).

    REST recovery after a gap is the owner's job (re-query open orders and
    replay them through ``on_update``); this loop only maps live updates. The
    lifecycle semantics it relies on are fully tested through the supervisor.
    """
    import threading as _threading

    try:
        from alpaca.trading.stream import TradingStream
    except ImportError as exc:
        raise RuntimeError(
            "alpaca-py is required for trade updates; install qlab[trader]") from exc

    stop_event = stop_event or _threading.Event()

    async def _on_update(data) -> None:
        order = getattr(data, "order", None)
        coid = str(getattr(order, "client_order_id", "") or "")
        if not coid:
            return
        supervisor.on_update(OrderUpdate(
            client_order_id=coid,
            event=str(getattr(data, "event", "")),
            filled_qty=float(getattr(order, "filled_qty", 0.0) or 0.0),
            filled_avg_price=(
                float(order.filled_avg_price)
                if getattr(order, "filled_avg_price", None) else None),
        ))

    delays = backoff_delays(base=0.5, cap=30.0)
    while not stop_event.is_set():
        stream = TradingStream(key, secret, paper=True)  # paper is not configurable
        stream.subscribe_trade_updates(_on_update)
        try:
            stream.run()
        except Exception:
            if stop_event.wait(next(delays)):
                break
        else:
            break


def marketable_limit_price(
    side: str,
    quote_bid: float,
    quote_ask: float,
    *,
    crossing_bps: float = 5.0,
) -> float:
    """Deterministic marketable-limit price from the current quote.

    A buy is priced at the ask plus a small crossing band, a sell at the bid
    minus it, so the order is marketable without paying an unbounded market
    order. Refuses to price against a crossed or non-positive quote rather than
    emitting a nonsensical limit.
    """
    bid, ask = float(quote_bid), float(quote_ask)
    if not (bid > 0 and ask > 0) or ask < bid:
        raise ValueError(
            f"cannot price a marketable limit against quote bid={bid} ask={ask}")
    band = crossing_bps / 1e4
    if side == "buy":
        return round(ask * (1.0 + band), 2)
    if side == "sell":
        return round(bid * (1.0 - band), 2)
    raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")
