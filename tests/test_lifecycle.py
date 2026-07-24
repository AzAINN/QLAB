"""Order lifecycle state machine: idempotent, accepted != filled (P3)."""

from __future__ import annotations

import pytest

from qlab.trader.lifecycle import (
    ACCEPTED,
    CANCELED,
    FILLED,
    PARTIALLY_FILLED,
    REJECTED,
    SUBMITTED,
    OrderUpdate,
    TradeUpdateSupervisor,
    marketable_limit_price,
)


def _sup():
    seen = []
    return TradeUpdateSupervisor(on_transition=seen.append), seen


def test_acceptance_does_not_mark_filled():
    sup, seen = _sup()
    sup.register("coid-1")
    t = sup.on_update(OrderUpdate("coid-1", "accepted"))
    assert t.state == ACCEPTED and not t.terminal
    assert sup.state("coid-1") == ACCEPTED


def test_partial_then_full_fill_accumulates_once():
    sup, seen = _sup()
    sup.on_update(OrderUpdate("coid-1", "new"))
    sup.on_update(OrderUpdate("coid-1", "partial_fill", filled_qty=3.0,
                              filled_avg_price=100.0, fee=0.01))
    # A duplicate partial with no new quantity is ignored.
    assert sup.on_update(OrderUpdate("coid-1", "partial_fill", filled_qty=3.0)) is None
    t = sup.on_update(OrderUpdate("coid-1", "fill", filled_qty=5.0,
                                  filled_avg_price=100.2, fee=0.01))
    assert t.state == FILLED and t.terminal
    assert t.filled_qty == 5.0
    assert t.fee == pytest.approx(0.02)


def test_terminal_state_is_sticky():
    sup, seen = _sup()
    sup.on_update(OrderUpdate("coid-1", "fill", filled_qty=5.0, filled_avg_price=100.0))
    # A late cancel/duplicate fill after terminal fill is a no-op.
    assert sup.on_update(OrderUpdate("coid-1", "canceled")) is None
    assert sup.on_update(OrderUpdate("coid-1", "fill", filled_qty=5.0)) is None
    assert sup.state("coid-1") == FILLED


def test_reject_and_cancel_and_expire_are_terminal():
    for event, expected in [("rejected", REJECTED), ("canceled", CANCELED),
                            ("expired", "expired")]:
        sup, _ = _sup()
        t = sup.on_update(OrderUpdate("c", event))
        assert t.terminal and t.state == expected


def test_late_accepted_after_partial_does_not_regress():
    sup, _ = _sup()
    sup.on_update(OrderUpdate("coid-1", "partial_fill", filled_qty=2.0))
    assert sup.state("coid-1") == PARTIALLY_FILLED
    assert sup.on_update(OrderUpdate("coid-1", "accepted")) is None
    assert sup.state("coid-1") == PARTIALLY_FILLED


def test_unknown_event_is_rejected():
    sup, _ = _sup()
    with pytest.raises(ValueError, match="unknown trade-update event"):
        sup.on_update(OrderUpdate("c", "teleported"))


def test_marketable_limit_prices_cross_the_spread():
    assert marketable_limit_price("buy", 99.9, 100.1) == pytest.approx(100.15, abs=0.01)
    assert marketable_limit_price("sell", 99.9, 100.1) == pytest.approx(99.85, abs=0.01)


def test_marketable_limit_refuses_a_crossed_or_empty_quote():
    with pytest.raises(ValueError, match="cannot price"):
        marketable_limit_price("buy", 100.2, 100.1)   # crossed
    with pytest.raises(ValueError, match="cannot price"):
        marketable_limit_price("buy", 0.0, 0.0)       # empty


def test_supervisor_transitions_persist_through_the_registry_writer():
    """The owner applies transitions as the single writer; the supervisor never
    touches DuckDB itself. Acknowledgement then fill must land as fill data."""
    from qlab.state.registry import Registry

    reg = Registry(":memory:")
    try:
        reg.add_order("coid-1", "plan-1", "ACWI", "buy", 1000.0, state=SUBMITTED)

        def sink(t):
            reg.apply_order_transition(
                t.client_order_id, t.state, filled_qty=t.filled_qty,
                avg_fill_price=t.filled_avg_price, fee=t.fee)

        sup = TradeUpdateSupervisor(on_transition=sink)
        sup.on_update(OrderUpdate("coid-1", "accepted"))
        assert reg.get_order("coid-1")["state"] == ACCEPTED
        assert reg.get_order("coid-1").get("filled_qty") in (None, 0.0)

        sup.on_update(OrderUpdate("coid-1", "fill", filled_qty=9.8,
                                  filled_avg_price=102.0, fee=0.02))
        row = reg.get_order("coid-1")
        assert row["state"] == FILLED
        assert row["filled_qty"] == pytest.approx(9.8)
        assert row["avg_fill_price"] == pytest.approx(102.0)
        assert row["fee"] == pytest.approx(0.02)
    finally:
        reg.close()
