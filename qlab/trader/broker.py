"""Broker gateway — a simulated paper broker and an Alpaca paper adapter.

The gateway wraps the broker so the *only* exposed operations are the mandated
ones. We deliberately do **not** mount a raw ``place_order`` tool: that is why
the autopilot cannot be prompt-injected into an arbitrary trade (research-plan
§8.1). ``SimulatedPaperBroker`` needs nothing external — it books against the
DuckDB registry and marks to the data feed, so the whole trader loop runs
offline. ``AlpacaPaperBroker`` is the real paper account; live is unimplemented.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Callable

from qlab.core import data as market
from qlab.state.registry import Registry

PriceProvider = Callable[[list[str]], dict[str, float]]


def default_price_provider(offline: bool = False, seed: int = 7) -> PriceProvider:
    """Latest available adjusted close per ticker (synthetic when offline)."""

    def provider(tickers: list[str]) -> dict[str, float]:
        px = market.get_prices(tickers, "2008-01-01", offline=offline, seed=seed)
        last = px.iloc[-1]
        return {t: float(last[t]) for t in tickers}

    return provider


class Broker(ABC):
    name: str = "broker"

    @abstractmethod
    def prices(self, tickers: list[str]) -> dict[str, float]: ...

    @abstractmethod
    def portfolio_state(self, tickers: list[str]) -> dict: ...

    @abstractmethod
    def submit_notional(self, client_order_id: str, ticker: str, side: str,
                        notional: float) -> dict: ...


class SimulatedPaperBroker(Broker):
    """In-process paper broker booked against the registry. Fractional fills."""

    name = "simulated_paper"

    def __init__(self, registry: Registry, price_provider: PriceProvider | None = None,
                 starting_cash: float = 10000.0, universe: list[str] | None = None):
        self.reg = registry
        self.price_provider = price_provider or default_price_provider()
        self.universe = list(universe) if universe else None
        # Marks are cached per broker instance so an order and its mark-to-market
        # are priced from ONE panel (buy price == mark price at the same instant).
        # This also sidesteps the synthetic feed's set-dependent RNG stream.
        self._marks: dict[str, float] | None = None
        if not self.reg.get_account():
            self.reg.init_account(starting_cash)

    def prices(self, tickers: list[str]) -> dict[str, float]:
        if self._marks is None:
            base = self.universe or list(tickers)
            self._marks = self.price_provider(base)
        missing = [t for t in tickers if t not in self._marks]
        if missing:
            base = sorted(set((self.universe or []) + list(tickers)))
            self._marks.update(self.price_provider(base))
        return {t: self._marks[t] for t in tickers}

    def portfolio_state(self, tickers: list[str]) -> dict:
        px = self.prices(tickers)
        positions = self.reg.get_positions()
        acct = self.reg.get_account()
        cash = float(acct.get("cash", 0.0))
        holdings = {}
        pos_value = 0.0
        for t, p in positions.items():
            price = px.get(t, p["avg_price"])
            value = p["qty"] * price
            pos_value += value
            holdings[t] = {"qty": p["qty"], "price": price, "value": value}
        equity = cash + pos_value
        weights = {t: (holdings[t]["value"] / equity if equity > 0 else 0.0)
                   for t in holdings}
        self.reg.update_high_water_mark(equity)
        return {
            "cash": cash, "equity": equity, "positions": holdings,
            "weights": weights, "high_water_mark": float(acct.get("high_water_mark", equity)),
            "halted": bool(acct.get("halted", False)),
        }

    def submit_notional(self, client_order_id: str, ticker: str, side: str,
                        notional: float) -> dict:
        price = self.prices([ticker])[ticker]
        qty = notional / price
        dqty = qty if side == "buy" else -qty
        cash_delta = -notional if side == "buy" else notional
        self.reg.apply_fill(ticker, dqty, price, cash_delta)
        return {"client_order_id": client_order_id, "ticker": ticker, "side": side,
                "qty": qty, "price": price, "notional": notional, "state": "filled"}


class AlpacaPaperBroker(Broker):
    """Alpaca paper-trading adapter. PAPER IS HARD-CODED; live is unimplemented.

    Requires ``alpaca-py`` and ``ALPACA_API_KEY`` / ``ALPACA_API_SECRET``. Uses
    fractional/notional orders (whole shares of a $250 ETF give ~2.5% weight
    granularity — the weights would be fiction; research-plan §8.1).
    """

    name = "alpaca_paper"

    def __init__(self, registry: Registry):
        self.reg = registry
        key = os.environ.get("ALPACA_API_KEY")
        secret = os.environ.get("ALPACA_API_SECRET")
        if not (key and secret):
            raise RuntimeError("ALPACA_API_KEY / ALPACA_API_SECRET not set")
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.data.historical import StockHistoricalDataClient
        except ImportError as exc:
            raise RuntimeError(f"alpaca-py not installed ({exc}); pip install qlab[trader]")
        # paper=True is NOT configurable here — this class only ever paper-trades
        self.trading = TradingClient(key, secret, paper=True)
        self.data = StockHistoricalDataClient(key, secret)

    def prices(self, tickers: list[str]) -> dict[str, float]:
        from alpaca.data.requests import StockLatestTradeRequest

        req = StockLatestTradeRequest(symbol_or_symbols=tickers)
        trades = self.data.get_stock_latest_trade(req)
        return {t: float(trades[t].price) for t in tickers}

    def portfolio_state(self, tickers: list[str]) -> dict:
        acct = self.trading.get_account()
        positions = {p.symbol: {"qty": float(p.qty), "price": float(p.current_price),
                                "value": float(p.market_value)}
                     for p in self.trading.get_all_positions()}
        equity = float(acct.equity)
        weights = {t: (positions[t]["value"] / equity if equity > 0 else 0.0)
                   for t in positions}
        # Persist a monotone high-water mark: returning the current equity as
        # the HWM would make drawdown always zero and silently disable the
        # kill switch and drawdown tiers on the live account.
        self.registry.update_high_water_mark(equity)
        hwm = float(self.registry.get_account().get("high_water_mark", equity)
                    or equity)
        return {"cash": float(acct.cash), "equity": equity, "positions": positions,
                "weights": weights, "high_water_mark": max(hwm, equity),
                "halted": bool(acct.trading_blocked)}

    def submit_notional(self, client_order_id: str, ticker: str, side: str,
                        notional: float) -> dict:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        req = MarketOrderRequest(
            symbol=ticker, notional=round(notional, 2),
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY, client_order_id=client_order_id)
        order = self.trading.submit_order(req)
        return {"client_order_id": client_order_id, "ticker": ticker, "side": side,
                "notional": notional, "state": str(order.status), "id": str(order.id)}


def get_broker(registry: Registry, *, offline: bool = False,
               starting_cash: float = 10000.0, seed: int = 7,
               universe: list[str] | None = None) -> Broker:
    """Return the Alpaca paper broker if credentials exist, else the simulator."""
    if os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_API_SECRET"):
        # Credentials present means the operator asked for Alpaca: a failure to
        # build it must be loud, never a silent downgrade to simulation (which
        # would book against the wrong venue without telling anyone).
        try:
            return AlpacaPaperBroker(registry)
        except Exception as exc:
            raise RuntimeError(
                "Alpaca credentials are set but the Alpaca paper broker could "
                f"not be initialized ({exc}); refusing to silently fall back to "
                "the simulator. Unset ALPACA_API_KEY/SECRET to use the "
                "simulator deliberately."
            ) from exc
    return SimulatedPaperBroker(
        registry, default_price_provider(offline=offline, seed=seed),
        starting_cash, universe=universe)
