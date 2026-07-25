"""Alpaca paper submission: tradability, limit pricing, REST recovery.

The real venue is never contacted. A fake trading client stands in for it, so
the submission contract — refuse untradable/non-fractionable symbols, price a
marketable limit from the quote, never report acknowledgement as a fill — is
verified fully offline. Live-account behavior is covered by the opt-in
integration tests in test_alpaca_integration.py.
"""

from __future__ import annotations

import sys
import types

import pytest

from qlab.state.registry import Registry
from qlab.trader.lifecycle import FILLED, TradeUpdateSupervisor, recover_from_broker


class _Asset:
    def __init__(self, tradable=True, fractionable=True, status="active"):
        self.tradable = tradable
        self.fractionable = fractionable
        self.status = status
        self.shortable = True


class _Order:
    def __init__(self, status="accepted"):
        self.status = status
        self.id = "order-1"


class _FakeTrading:
    """Minimal stand-in for alpaca TradingClient."""

    def __init__(self, assets=None):
        self.assets = assets or {}
        self.submitted = []

    def get_asset(self, ticker):
        return self.assets.get(ticker, _Asset())

    def submit_order(self, request):
        self.submitted.append(request)
        return _Order()


@pytest.fixture
def alpaca_modules(monkeypatch):
    """Install fake alpaca modules so broker imports resolve offline."""
    created = []

    def module(name, **attrs):
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        monkeypatch.setitem(sys.modules, name, mod)
        created.append(name)
        return mod

    class _Side:
        BUY = "buy"
        SELL = "sell"

    class _TIF:
        DAY = "day"

    class _LimitReq:
        def __init__(self, **kw):
            self.__dict__.update(kw)
            self.kind = "limit"

    class _MarketReq:
        def __init__(self, **kw):
            self.__dict__.update(kw)
            self.kind = "market"

    module("alpaca")
    module("alpaca.trading")
    module("alpaca.trading.enums", OrderSide=_Side, TimeInForce=_TIF,
           QueryOrderStatus=types.SimpleNamespace(ALL="all"))
    module("alpaca.trading.requests", LimitOrderRequest=_LimitReq,
           MarketOrderRequest=_MarketReq,
           GetOrdersRequest=lambda **kw: types.SimpleNamespace(**kw))
    return {"limit": _LimitReq, "market": _MarketReq}


def _broker(monkeypatch, trading, *, quote=(99.9, 100.1)):
    """Build an AlpacaPaperBroker with its clients replaced by fakes."""
    from qlab.trader.broker import AlpacaPaperBroker

    broker = AlpacaPaperBroker.__new__(AlpacaPaperBroker)
    broker.reg = Registry(":memory:")
    broker.trading = trading
    broker.data = None
    broker._asset_cache = {}
    if quote is None:
        def _raise(_ticker):
            raise RuntimeError("no quote entitlement")
        broker.quote = _raise
    else:
        broker.quote = lambda _ticker: quote
    return broker


# --- tradability -------------------------------------------------------------


def test_untradable_symbol_is_refused_before_submission(alpaca_modules, monkeypatch):
    trading = _FakeTrading({"XYZ": _Asset(tradable=False, status="inactive")})
    broker = _broker(monkeypatch, trading)
    with pytest.raises(RuntimeError, match="not tradable"):
        broker.submit_notional("coid-1", "XYZ", "buy", 1000.0)
    assert trading.submitted == []


def test_non_fractionable_symbol_is_refused_rather_than_rounded(
        alpaca_modules, monkeypatch):
    trading = _FakeTrading({"BRK.A": _Asset(fractionable=False)})
    broker = _broker(monkeypatch, trading)
    with pytest.raises(RuntimeError, match="not fractionable"):
        broker.submit_notional("coid-1", "BRK.A", "buy", 1000.0)
    assert trading.submitted == []


def test_asset_status_is_cached_per_session(alpaca_modules, monkeypatch):
    calls = []

    class _Counting(_FakeTrading):
        def get_asset(self, ticker):
            calls.append(ticker)
            return super().get_asset(ticker)

    broker = _broker(monkeypatch, _Counting())
    broker.assert_tradable("ACWI")
    broker.assert_tradable("ACWI")
    assert calls == ["ACWI"]


# --- pricing -----------------------------------------------------------------


def test_buy_is_priced_as_a_marketable_limit(alpaca_modules, monkeypatch):
    trading = _FakeTrading()
    broker = _broker(monkeypatch, trading, quote=(99.9, 100.1))
    result = broker.submit_notional("coid-1", "ACWI", "buy", 1000.0)
    request = trading.submitted[0]
    assert request.kind == "limit"
    # Crosses the ask by the bounded band.
    assert request.limit_price == pytest.approx(100.15, abs=0.01)
    assert request.qty == pytest.approx(1000.0 / request.limit_price, rel=1e-6)
    assert result["limit_price"] == request.limit_price


def test_a_crossed_quote_refuses_rather_than_sending_unpriced(
        alpaca_modules, monkeypatch):
    trading = _FakeTrading()
    broker = _broker(monkeypatch, trading, quote=(100.2, 100.1))  # crossed
    with pytest.raises(ValueError, match="cannot price"):
        broker.submit_notional("coid-1", "ACWI", "buy", 1000.0)
    assert trading.submitted == []


def test_missing_quote_service_falls_back_to_notional_market(
        alpaca_modules, monkeypatch):
    trading = _FakeTrading()
    broker = _broker(monkeypatch, trading, quote=None)
    result = broker.submit_notional("coid-1", "ACWI", "buy", 1000.0)
    request = trading.submitted[0]
    assert request.kind == "market"
    assert request.notional == 1000.0
    assert result["limit_price"] is None


# --- acknowledgement is not a fill -------------------------------------------


def test_submission_returns_acknowledgement_not_a_fill(alpaca_modules, monkeypatch):
    broker = _broker(monkeypatch, _FakeTrading())
    result = broker.submit_notional("coid-1", "ACWI", "buy", 1000.0)
    assert result["acknowledged"] is True
    assert result["state"] == "accepted"
    assert result["state"] != "filled"


# --- REST recovery -----------------------------------------------------------


def test_recovery_replays_broker_truth_through_the_state_machine():
    seen = []
    sup = TradeUpdateSupervisor(on_transition=seen.append)
    sup.register("coid-1")
    open_orders = {
        "coid-1": {"client_order_id": "coid-1", "state": "filled",
                   "filled_qty": 9.5, "filled_avg_price": 105.0},
    }
    transitions = recover_from_broker(sup, open_orders)
    assert len(transitions) == 1
    assert transitions[0].state == FILLED
    assert transitions[0].filled_qty == 9.5
    # Replaying the same broker state is idempotent — no double-book.
    assert recover_from_broker(sup, open_orders) == []


def test_recovery_ignores_orders_the_venue_does_not_know():
    sup = TradeUpdateSupervisor()
    sup.register("coid-1")
    # Absence from broker truth is not evidence of a fill.
    assert recover_from_broker(sup, {}, client_order_ids=["coid-1"]) == []
    assert sup.state("coid-1") == "submitted"


def test_recovery_maps_terminal_broker_statuses():
    for status, expected in (("rejected", "rejected"), ("expired", "expired"),
                             ("canceled", "canceled")):
        sup = TradeUpdateSupervisor()
        transitions = recover_from_broker(
            sup, {"c": {"client_order_id": "c", "state": status}})
        assert transitions[0].state == expected
        assert transitions[0].terminal is True
