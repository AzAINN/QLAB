"""Walk-forward backtest engine.

The central design insight (research-plan §2): stepped-mode backtest and live
trading are **the same loop with a different clock**. Here the engine advances a
historical ``as_of``; live, the clock is real. Either way a ``policy`` maps a
point-in-time :class:`DataSnapshot` to target weights, look-ahead is
structurally impossible, and the same decision log is written.

The engine is solver-agnostic: benchmarks, staged algorithms, and mock arms all
plug in as a ``policy`` callable, so the ablation holds *everything* constant and
varies exactly one thing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

import numpy as np
import pandas as pd

from qlab.core.costs import (
    DEFAULT_ADV_NOTIONAL,
    DEFAULT_COMMISSION_BPS,
    DEFAULT_IMPACT_K,
    DEFAULT_SPREAD_BPS,
    cost_model as estimate_trade_cost,
)
from qlab.core.metrics import compute_metrics
from qlab.core.types import DataSnapshot, Weights

# A policy decides target weights from information available at as_of.
Policy = Callable[[DataSnapshot], Weights]


@dataclass
class BacktestResult:
    """Outcome of one arm's walk-forward run."""

    arm_id: str
    returns: pd.Series                       # net per-day portfolio returns
    weights_history: dict[date, dict[str, float]] = field(default_factory=dict)
    turnover_history: dict[date, float] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)
    gross_returns: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    cost_history: dict[date, float] = field(default_factory=dict)
    cost_breakdown_history: dict[date, dict] = field(default_factory=dict)

    @property
    def total_turnover(self) -> float:
        return float(sum(self.turnover_history.values()))

    @property
    def net_returns(self) -> pd.Series:
        """Net series retained under the historical ``returns`` name."""
        return self.returns

    @property
    def total_cost_drag(self) -> float:
        return float(sum(self.cost_history.values()))

    def equity_curve(self) -> pd.Series:
        return (1 + self.returns).cumprod()


def rebalance_dates(index: pd.DatetimeIndex, cadence: str = "quarterly") -> list[pd.Timestamp]:
    """Pick rebalance dates from a price index at the requested cadence."""
    freq = {"quarterly": "QE", "monthly": "ME", "annual": "YE", "weekly": "W"}.get(cadence)
    if freq is None:
        raise ValueError(f"unknown cadence: {cadence!r}")
    marks = pd.Series(index=index, data=1).resample(freq).last().dropna().index
    # snap each mark back to the last actual trading day on/before it
    out = []
    for m in marks:
        prior = index[index <= m]
        if len(prior):
            out.append(prior[-1])
    return sorted(set(out))


def _configured_value(
    configured: float | Mapping[str, float],
    ticker: str,
    *,
    name: str,
    default: float | None,
    positive: bool,
) -> float:
    if isinstance(configured, Mapping):
        if ticker in configured:
            raw = configured[ticker]
        else:
            sentinel = next(
                (key for key in ("default", "*") if key in configured),
                None,
            )
            if sentinel is None:
                if default is None:
                    raise ValueError(f"{name} has no value for {ticker}")
                raw = default
            else:
                raw = configured[sentinel]
    else:
        raw = configured
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} for {ticker} must be numeric") from exc
    if not np.isfinite(value):
        raise ValueError(f"{name} for {ticker} must be finite")
    if value < 0 or (positive and value <= 0):
        condition = "positive" if positive else "non-negative"
        raise ValueError(f"{name} for {ticker} must be {condition}")
    return value


def _volume_data(
    prices: pd.DataFrame,
    volumes: pd.DataFrame | None,
    tickers: list[str],
) -> pd.DataFrame | None:
    supplied = volumes
    if supplied is None:
        for key in ("volumes", "volume"):
            candidate = prices.attrs.get(key)
            if isinstance(candidate, pd.DataFrame):
                supplied = candidate
                break
    if supplied is None:
        return None
    if not isinstance(supplied, pd.DataFrame):
        raise ValueError("volumes must be a pandas DataFrame")
    missing = [ticker for ticker in tickers if ticker not in supplied.columns]
    if missing:
        raise ValueError(f"volumes missing ticker columns: {missing}")
    panel = supplied.sort_index().reindex(index=prices.index, columns=tickers)
    finite = panel.to_numpy(dtype=float)
    if np.any(np.isinf(finite)):
        raise ValueError("volumes must not contain infinite values")
    if np.any(finite[np.isfinite(finite)] < 0):
        raise ValueError("volumes must be non-negative")
    return panel


def run_backtest(
    prices: pd.DataFrame,
    policy: Policy,
    *,
    arm_id: str = "arm",
    cadence: str = "quarterly",
    lookback_days: int = 756,
    cost_bps: float = 5.0,
    n_trials: int = 1,
    cost_model: str = "flat",
    volumes: pd.DataFrame | None = None,
    portfolio_notional: float = 10_000.0,
    adv_notional: float | Mapping[str, float] = DEFAULT_ADV_NOTIONAL,
    daily_vol: float | Mapping[str, float] | None = None,
    spread_bps: float = DEFAULT_SPREAD_BPS,
    commission_bps: float = DEFAULT_COMMISSION_BPS,
    impact_k: float = DEFAULT_IMPACT_K,
) -> BacktestResult:
    """Run a walk-forward backtest of ``policy`` over ``prices``.

    Weights are set at each rebalance from a truncated snapshot, then **drift**
    with returns until the next rebalance (no phantom daily rebalancing).
    The default ``flat`` model preserves the historical ``cost_bps`` charge per
    unit of ``Σ|Δw|``. ``cost_model="realistic"`` instead sums per-leg
    commission, half-spread, and square-root impact. It uses a point-in-time
    60-day median dollar-volume ADV when ``volumes`` are supplied, otherwise
    the configured ``adv_notional`` fallback.
    """
    if cost_model not in {"flat", "realistic"}:
        raise ValueError(f"unknown cost_model: {cost_model!r}")
    if cost_model == "realistic":
        try:
            portfolio_notional = float(portfolio_notional)
        except (TypeError, ValueError) as exc:
            raise ValueError("portfolio_notional must be numeric") from exc
        if not np.isfinite(portfolio_notional) or portfolio_notional <= 0:
            raise ValueError("portfolio_notional must be positive and finite")

    prices = prices.sort_index()
    R = prices.pct_change().dropna(how="all")
    tickers = list(prices.columns)
    volume_panel = _volume_data(prices, volumes, tickers) if cost_model == "realistic" else None

    rdates = rebalance_dates(prices.index, cadence)
    # need enough history for the first estimation window
    rdates = [d for d in rdates if len(prices.loc[:d]) >= min(lookback_days, 60)]
    if not rdates:
        raise ValueError("no rebalance dates with sufficient lookback")

    daily_returns: dict[pd.Timestamp, float] = {}
    daily_gross_returns: dict[pd.Timestamp, float] = {}
    weights_history: dict[date, dict[str, float]] = {}
    turnover_history: dict[date, float] = {}
    cost_history: dict[date, float] = {}
    cost_breakdown_history: dict[date, dict] = {}
    held_w: pd.Series | None = None

    for k, d_k in enumerate(rdates):
        end = rdates[k + 1] if k + 1 < len(rdates) else prices.index[-1]

        snap = DataSnapshot(tickers=tickers, prices=prices, as_of=d_k.date())
        target = policy(snap).as_series().reindex(tickers).fillna(0.0)

        if held_w is None:
            turnover = float(target.abs().sum())          # initial deployment
        else:
            turnover = float((target - held_w.reindex(tickers).fillna(0.0)).abs().sum())
        delta = target if held_w is None else target - held_w.reindex(tickers).fillna(0.0)

        if cost_model == "flat":
            cost = turnover * cost_bps / 1e4
            cost_breakdown = {
                "model": "flat",
                "return_cost": cost,
                "total": cost,
            }
        else:
            trailing_returns = R.loc[R.index <= d_k, tickers].tail(60)
            if daily_vol is None and len(trailing_returns) < 2:
                raise ValueError(f"insufficient return history for costs at {d_k.date()}")

            totals = {
                "commission": 0.0,
                "half_spread": 0.0,
                "impact": 0.0,
                "minimum_adjustment": 0.0,
                "total": 0.0,
            }
            leg_costs = []
            for ticker in tickers:
                trade_notional = abs(float(delta[ticker])) * portfolio_notional
                if trade_notional == 0:
                    continue
                price = float(prices.loc[d_k, ticker])
                if volume_panel is None:
                    adv = _configured_value(
                        adv_notional,
                        ticker,
                        name="adv_notional",
                        default=DEFAULT_ADV_NOTIONAL,
                        positive=True,
                    )
                    adv_source = "configured"
                else:
                    dollar_volume = (
                        prices.loc[:d_k, ticker] * volume_panel.loc[:d_k, ticker]
                    ).dropna().tail(60)
                    if dollar_volume.empty:
                        raise ValueError(
                            f"no usable volume history for {ticker} at {d_k.date()}"
                        )
                    adv = float(dollar_volume.median())
                    if not np.isfinite(adv) or adv <= 0:
                        raise ValueError(
                            f"trailing ADV for {ticker} at {d_k.date()} must be positive"
                        )
                    adv_source = "rolling_60d_median_dollar_volume"

                if daily_vol is None:
                    vol = float(trailing_returns[ticker].dropna().std(ddof=1))
                    if not np.isfinite(vol) or vol < 0:
                        raise ValueError(
                            f"daily volatility for {ticker} at {d_k.date()} is invalid"
                        )
                else:
                    vol = _configured_value(
                        daily_vol,
                        ticker,
                        name="daily_vol",
                        default=None,
                        positive=False,
                    )

                breakdown = estimate_trade_cost(
                    trade_notional,
                    price,
                    adv,
                    vol,
                    spread_bps=spread_bps,
                    commission_bps=commission_bps,
                    impact_k=impact_k,
                )
                scalar_breakdown = {
                    component: float(value)
                    for component, value in breakdown.items()
                }
                for component in totals:
                    totals[component] += scalar_breakdown[component]
                leg_costs.append({
                    "ticker": ticker,
                    "trade_notional": trade_notional,
                    "price": price,
                    "adv_notional": adv,
                    "adv_source": adv_source,
                    "daily_vol": vol,
                    **scalar_breakdown,
                })

            cost = totals["total"] / portfolio_notional
            cost_breakdown = {
                "model": "realistic",
                "portfolio_notional": portfolio_notional,
                "return_cost": cost,
                **totals,
                "legs": leg_costs,
            }

        weights_history[d_k.date()] = {t: float(target[t]) for t in tickers}
        turnover_history[d_k.date()] = turnover

        w = target.copy()
        period_days = R.index[(R.index > d_k) & (R.index <= end)]
        for i, day in enumerate(period_days):
            r_day = R.loc[day, tickers].fillna(0.0)
            gross = float((w.values * r_day.values).sum())
            daily_gross_returns[day] = gross
            daily_returns[day] = gross - (cost if i == 0 else 0.0)
            # Grow by the *portfolio's* growth factor (1 + gross), not by
            # renormalizing the drifted weights to sum back to 1.0. Cash
            # earns 0 and is already reflected in `gross` as drag, so this
            # divisor preserves the un-invested (cash) share 1 - sum(w)
            # through the period instead of silently re-levering a
            # sub-1 (cash-carrying) weight vector back to full investment
            # after one day. When sum(w) == 1 exactly, denom == grown.sum(),
            # so fully-invested arms are bit-identical to the old behavior.
            denom = 1.0 + gross
            grown = w.values * (1.0 + r_day.values)
            w = pd.Series(grown / denom if denom > 0 else grown, index=tickers)
        if len(period_days):
            cost_history[d_k.date()] = cost
            cost_breakdown_history[d_k.date()] = cost_breakdown
        held_w = w

    ret_series = pd.Series(daily_returns).sort_index()
    gross_ret_series = pd.Series(daily_gross_returns).sort_index()
    metrics = compute_metrics(
        ret_series, turnover=sum(turnover_history.values()), n_trials=n_trials
    )
    gross_metrics = compute_metrics(
        gross_ret_series, turnover=sum(turnover_history.values()), n_trials=n_trials
    )
    metrics.update({f"net_{key}": value for key, value in metrics.items()})
    metrics.update({f"gross_{key}": value for key, value in gross_metrics.items()})
    metrics["total_cost_drag"] = float(sum(cost_history.values()))
    return BacktestResult(
        arm_id=arm_id,
        returns=ret_series,
        weights_history=weights_history,
        turnover_history=turnover_history,
        metrics=metrics,
        diagnostics={"cadence": cadence, "lookback_days": lookback_days,
                     "cost_bps": cost_bps, "cost_model": cost_model,
                     "n_rebalances": len(rdates)},
        gross_returns=gross_ret_series,
        cost_history=cost_history,
        cost_breakdown_history=cost_breakdown_history,
    )
