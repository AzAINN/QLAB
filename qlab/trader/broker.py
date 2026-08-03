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
from qlab.trader.alpaca_auth import (
    AlpacaAuthError, AlpacaCredentials, refuse_partial_env_credentials,
    resolve_alpaca_credentials)

PriceProvider = Callable[[list[str]], dict[str, float]]


def default_price_provider(offline: bool = False, seed: int = 7) -> PriceProvider:
    """Latest available adjusted close per ticker (synthetic when offline)."""

    def provider(tickers: list[str]) -> dict[str, float]:
        px = market.get_prices(tickers, "2008-01-01", offline=offline, seed=seed)
        last = px.iloc[-1]
        return {t: float(last[t]) for t in tickers}

    # The provider carries which feed it prices from, because the high-water
    # mark it feeds is lane-scoped: a synthetic valuation of positions bought
    # at live prices reads roughly 2x and ratchets a peak the book never
    # reached, and the next live read turns that into a fabricated drawdown
    # that trips the kill switch. (Observed: a $10k live-lane book halted at
    # "59% drawdown" against a $24.6k synthetic-priced mark.)
    provider.synthetic = offline
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
        if not self.reg.get_account(self.name):
            self.reg.init_account(starting_cash, book=self.name)

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
        # Named explicitly: a high-water mark belongs to the venue that reached
        # it, and sharing one across books reads the difference as a drawdown.
        acct = self.reg.get_account(self.name)
        cash = float(acct.get("cash", 0.0))
        holdings = {}
        pos_value = 0.0
        for t, p in positions.items():
            price = px.get(t, p["avg_price"])
            value = p["qty"] * price
            pos_value += value
            holdings[t] = {"qty": p["qty"], "price": price, "value": value,
                           "unrealized_pl": (price - p["avg_price"]) * p["qty"]}
        equity = cash + pos_value
        weights = {t: (holdings[t]["value"] / equity if equity > 0 else 0.0)
                   for t in holdings}
        # The lane rides with the ratchet: this broker is priced from either
        # feed depending on the caller's offline flag, and a mark set by the
        # other feed is not a peak in this feed's units (see the registry's
        # lane rules). An unmarked provider is refused — defaulting it to
        # either lane would re-open the silent cross-feed ratchet.
        lane = getattr(self.price_provider, "synthetic", None)
        if lane is None:
            raise RuntimeError(
                "price provider does not declare its lane; the high-water "
                "mark it feeds is lane-scoped (build it with "
                "default_price_provider, or set provider.synthetic)")
        self.reg.update_high_water_mark(
            equity, book=self.name,
            lane="synthetic" if lane else "live")
        acct = self.reg.get_account(self.name)
        return {
            "cash": cash, "equity": equity, "positions": holdings,
            "weights": weights, "high_water_mark": float(acct.get("high_water_mark", equity)),
            "halted": bool(acct.get("halted", False)),
        }

    def submit_notional(self, client_order_id: str, ticker: str, side: str,
                        notional: float) -> dict:
        price = self.prices([ticker])[ticker]
        qty = notional / price
        if side == "sell":
            # Never sell more than is held: a sell notional fixed at plan time
            # can exceed the position if the price fell before execution, which
            # would open a short. Long-only is enforced at the fill boundary,
            # so a full liquidation lands at exactly flat, never short.
            held = float(self.reg.get_positions().get(ticker, {}).get("qty", 0.0))
            qty = min(qty, max(held, 0.0))
        filled_notional = qty * price
        dqty = qty if side == "buy" else -qty
        cash_delta = -filled_notional if side == "buy" else filled_notional
        self.reg.apply_fill(ticker, dqty, price, cash_delta, book=self.name)
        return {"client_order_id": client_order_id, "ticker": ticker, "side": side,
                "qty": qty, "price": price, "notional": filled_notional, "state": "filled"}


class AlpacaPaperBroker(Broker):
    """Alpaca paper-trading adapter. PAPER IS HARD-CODED; live is unimplemented.

    Requires ``alpaca-py`` and either an `alpaca profile login` session or
    ``ALPACA_API_KEY`` / ``ALPACA_API_SECRET``. Uses fractional/notional orders
    (whole shares of a $250 ETF give ~2.5% weight granularity — the weights
    would be fiction; research-plan §8.1).
    """

    name = "alpaca_paper"

    def __init__(self, registry: Registry,
                 credentials: AlpacaCredentials | None = None):
        self.reg = registry
        creds = credentials or resolve_alpaca_credentials()
        if creds is None:
            raise AlpacaAuthError(
                "no Alpaca credentials found — run `alpaca profile login` or "
                "set ALPACA_API_KEY / ALPACA_API_SECRET")
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.data.historical import StockHistoricalDataClient
        except ImportError as exc:
            raise RuntimeError(f"alpaca-py not installed ({exc}); pip install qlab[trader]")
        # paper=True is NOT configurable here — this class only ever paper-trades.
        # An OAuth token from `alpaca profile login` is paper-only at the source,
        # which is why that flow is the preferred credential.
        if creds.kind == "oauth":
            self.trading = TradingClient(oauth_token=creds.oauth_token, paper=True)
            self.data = StockHistoricalDataClient(oauth_token=creds.oauth_token)
        else:
            self.trading = TradingClient(creds.api_key, creds.secret_key, paper=True)
            self.data = StockHistoricalDataClient(creds.api_key, creds.secret_key)
        self._asset_cache: dict[str, dict] = {}

    def prices(self, tickers: list[str]) -> dict[str, float]:
        from alpaca.data.requests import StockLatestTradeRequest

        req = StockLatestTradeRequest(symbol_or_symbols=tickers)
        trades = self.data.get_stock_latest_trade(req)
        return {t: float(trades[t].price) for t in tickers}

    def quote(self, ticker: str) -> tuple[float, float]:
        """Current (bid, ask) for pricing a marketable limit."""
        from alpaca.data.requests import StockLatestQuoteRequest

        req = StockLatestQuoteRequest(symbol_or_symbols=[ticker])
        quotes = self.data.get_stock_latest_quote(req)
        quote = quotes[ticker]
        return float(quote.bid_price), float(quote.ask_price)

    def assert_tradable(self, ticker: str, *, fractional: bool = True) -> dict:
        """Refuse a symbol the venue will not trade the way we intend to.

        Checked BEFORE submission so an untradable or non-fractionable symbol
        fails loudly against the plan rather than as a broker reject mid-plan.
        Results are cached per broker instance — asset status is static within
        a session.
        """
        cached = self._asset_cache.get(ticker)
        if cached is None:
            asset = self.trading.get_asset(ticker)
            cached = {
                "tradable": bool(getattr(asset, "tradable", False)),
                "fractionable": bool(getattr(asset, "fractionable", False)),
                "status": str(getattr(asset, "status", "")),
                "shortable": bool(getattr(asset, "shortable", False)),
            }
            self._asset_cache[ticker] = cached
        if not cached["tradable"]:
            raise RuntimeError(
                f"{ticker} is not tradable at the venue "
                f"(status {cached['status']!r}); refusing to submit")
        if fractional and not cached["fractionable"]:
            raise RuntimeError(
                f"{ticker} is not fractionable; notional orders would be "
                "rounded to whole shares and the target weights would be "
                "fiction. Refusing rather than silently changing the plan.")
        return cached

    def open_orders(self) -> dict[str, dict]:
        """Live open orders keyed by client_order_id, for REST recovery.

        After a trade-update stream gap the owner re-reads broker truth here
        and replays each order through the lifecycle supervisor, rather than
        assuming what happened while it was disconnected.
        """
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        request = GetOrdersRequest(status=QueryOrderStatus.ALL)
        out: dict[str, dict] = {}
        for order in self.trading.get_orders(request):
            coid = str(getattr(order, "client_order_id", "") or "")
            if not coid:
                continue
            out[coid] = {
                "client_order_id": coid,
                "id": str(getattr(order, "id", "")),
                "symbol": str(getattr(order, "symbol", "")),
                "state": str(getattr(order, "status", "")),
                "filled_qty": float(getattr(order, "filled_qty", 0.0) or 0.0),
                "filled_avg_price": (
                    float(order.filled_avg_price)
                    if getattr(order, "filled_avg_price", None) else None),
            }
        return out

    def portfolio_state(self, tickers: list[str]) -> dict:
        acct = self.trading.get_account()
        positions = {p.symbol: {"qty": float(p.qty), "price": float(p.current_price),
                                "value": float(p.market_value),
                                "unrealized_pl": float(p.unrealized_pl)}
                     for p in self.trading.get_all_positions()}
        equity = float(acct.equity)
        weights = {t: (positions[t]["value"] / equity if equity > 0 else 0.0)
                   for t in positions}
        # Persist a monotone high-water mark: returning the current equity as
        # the HWM would make drawdown always zero and silently disable the
        # kill switch and drawdown tiers on the live account. Seed the account
        # row on first read so the monotone update has something to raise.
        if not self.reg.get_account(self.name):
            self.reg.init_account(equity, book=self.name)
        self.reg.update_high_water_mark(equity, book=self.name)
        hwm = float(self.reg.get_account(self.name).get("high_water_mark", equity)
                    or equity)
        return {"cash": float(acct.cash), "equity": equity, "positions": positions,
                "weights": weights, "high_water_mark": max(hwm, equity),
                "halted": bool(acct.trading_blocked)}

    def submit_notional(self, client_order_id: str, ticker: str, side: str,
                        notional: float) -> dict:
        """Submit one leg and return its ACKNOWLEDGED state — never a fill.

        Alpaca acknowledges first and fills later, so the returned ``state`` is
        whatever the venue said (``new``/``accepted``/…). The caller must not
        read that as a fill: the trade-update supervisor advances the leg to
        ``filled`` only when the venue reports a fill.

        Pricing: a notional market order gives no control over the price paid,
        so when a live quote is available the order is priced as a marketable
        limit that crosses the spread by a bounded band. If the quote is
        unusable (crossed, empty) the order is refused rather than sent
        unpriced — an unpriced order in a thin book is how a paper desk learns
        an expensive lesson.
        """
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

        from qlab.trader.lifecycle import marketable_limit_price

        self.assert_tradable(ticker, fractional=True)
        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
        limit_price = None
        try:
            bid, ask = self.quote(ticker)
            limit_price = marketable_limit_price(side, bid, ask)
        except ValueError:
            # A crossed or empty quote is a refusal, not a reason to fall back
            # to an unpriced market order.
            raise
        except Exception:
            # No quote service available (e.g. outside data entitlement): fall
            # back to a notional market order, which is the documented paper
            # behavior, rather than blocking the desk entirely.
            limit_price = None

        if limit_price is not None:
            qty = round(notional / limit_price, 6)
            request = LimitOrderRequest(
                symbol=ticker, qty=qty, side=order_side,
                time_in_force=TimeInForce.DAY, limit_price=limit_price,
                client_order_id=client_order_id)
        else:
            request = MarketOrderRequest(
                symbol=ticker, notional=round(notional, 2), side=order_side,
                time_in_force=TimeInForce.DAY, client_order_id=client_order_id)

        order = self.trading.submit_order(request)
        return {"client_order_id": client_order_id, "ticker": ticker,
                "side": side, "notional": notional, "limit_price": limit_price,
                "state": str(order.status), "id": str(order.id),
                "acknowledged": True}

    def portfolio_history(self, period: str = "1M",
                          timeframe: str = "1D") -> list[dict]:
        """Account equity history from Alpaca, oldest first, UTC ISO stamps."""
        from datetime import datetime, timezone

        from alpaca.trading.requests import GetPortfolioHistoryRequest

        history = self.trading.get_portfolio_history(
            GetPortfolioHistoryRequest(period=period, timeframe=timeframe))
        rows = []
        for stamp, equity in zip(history.timestamp, history.equity):
            if equity is None:
                continue
            rows.append({
                "ts": datetime.fromtimestamp(int(stamp), tz=timezone.utc).isoformat(),
                "equity": float(equity),
            })
        return rows


def _env_credential_signal() -> bool:
    """Whether the environment carries an Alpaca key/secret at all.

    Truthiness only — the values are never bound to a name or logged. A partial
    pair counts as a signal so ``resolve_alpaca_credentials`` gets to refuse it
    by name instead of this function quietly choosing the simulator.
    """
    return any(os.environ.get(name, "").strip()
               for name in ("ALPACA_API_KEY", "ALPACA_API_SECRET"))


def get_broker(registry: Registry, *, offline: bool = False,
               starting_cash: float = 10000.0, seed: int = 7,
               universe: list[str] | None = None,
               book: str | None = None) -> Broker:
    """Return the broker for the chosen ``book``.

    ``book`` is the operator's explicit decision (``"simulated"`` or
    ``"alpaca"``). ``None`` keeps the historical behaviour for callers that have
    no desk mode yet, and that behaviour is inferred from
    ``ALPACA_API_KEY``/``ALPACA_API_SECRET`` **alone**: an `alpaca profile login`
    session discovered on disk is reachable only when a caller explicitly asks
    for the Alpaca book. Otherwise logging in with the Alpaca CLI would move
    every default caller onto the real paper account without anyone choosing it.
    """
    if book not in (None, "simulated", "alpaca"):
        raise ValueError(f"unknown book {book!r}; choose 'simulated' or 'alpaca'")
    if book == "simulated" or (book is None and not _env_credential_signal()):
        # This refusal predates the explicit book and was unconditional then.
        # The simulated book needs no credential, but half a pair is a broken
        # setup the operator has to see rather than one this lane steps over.
        # (``book is None`` here means neither half is set, so it is a no-op.)
        refuse_partial_env_credentials()
        return SimulatedPaperBroker(
            registry, default_price_provider(offline=offline, seed=seed),
            starting_cash, universe=universe)

    # Reached only by an explicit "alpaca", or by env credentials that already
    # signalled Alpaca intent. The resolver prefers those env credentials and
    # refuses a partial pair by name.
    creds = resolve_alpaca_credentials()
    if creds is None:
        raise RuntimeError(
            "the Alpaca book was selected but no credentials were found — run "
            "`alpaca profile login`, or choose the simulated book")
    # A failure here must be loud, never a silent downgrade to simulation
    # (which would book against the wrong venue without telling anyone).
    try:
        return AlpacaPaperBroker(registry, credentials=creds)
    except Exception as exc:
        raise RuntimeError(
            "the Alpaca paper broker could not be initialized "
            f"({exc}); refusing to silently fall back to the simulator"
        ) from exc
