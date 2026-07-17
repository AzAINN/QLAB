"""Walk-forward backtest engine.

The central design insight (research-plan §2): stepped-mode backtest and live
trading are **the same loop with a different clock**. Here the engine advances a
historical ``as_of``; live, the clock is real. Either way a ``policy`` maps a
point-in-time :class:`DataSnapshot` to target weights, look-ahead is
structurally impossible, and the same decision log is written.

The engine is solver-agnostic: benchmarks, classical, quantum and mock arms all
plug in as a ``policy`` callable, so the ablation holds *everything* constant and
varies exactly one thing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable

import numpy as np
import pandas as pd

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

    @property
    def total_turnover(self) -> float:
        return float(sum(self.turnover_history.values()))

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


def run_backtest(
    prices: pd.DataFrame,
    policy: Policy,
    *,
    arm_id: str = "arm",
    cadence: str = "quarterly",
    lookback_days: int = 756,
    cost_bps: float = 5.0,
    n_trials: int = 1,
) -> BacktestResult:
    """Run a walk-forward backtest of ``policy`` over ``prices``.

    Weights are set at each rebalance from a truncated snapshot, then **drift**
    with returns until the next rebalance (no phantom daily rebalancing).
    Turnover is charged against the drifted book at ``cost_bps`` per unit of
    ``Σ|Δw|``.
    """
    prices = prices.sort_index()
    R = prices.pct_change().dropna(how="all")
    tickers = list(prices.columns)

    rdates = rebalance_dates(prices.index, cadence)
    # need enough history for the first estimation window
    rdates = [d for d in rdates if len(prices.loc[:d]) >= min(lookback_days, 60)]
    if not rdates:
        raise ValueError("no rebalance dates with sufficient lookback")

    daily_returns: dict[pd.Timestamp, float] = {}
    weights_history: dict[date, dict[str, float]] = {}
    turnover_history: dict[date, float] = {}
    held_w: pd.Series | None = None

    for k, d_k in enumerate(rdates):
        end = rdates[k + 1] if k + 1 < len(rdates) else prices.index[-1]

        snap = DataSnapshot(tickers=tickers, prices=prices, as_of=d_k.date())
        target = policy(snap).as_series().reindex(tickers).fillna(0.0)

        if held_w is None:
            turnover = float(target.abs().sum())          # initial deployment
        else:
            turnover = float((target - held_w.reindex(tickers).fillna(0.0)).abs().sum())
        cost = turnover * cost_bps / 1e4

        weights_history[d_k.date()] = {t: float(target[t]) for t in tickers}
        turnover_history[d_k.date()] = turnover

        w = target.copy()
        period_days = R.index[(R.index > d_k) & (R.index <= end)]
        for i, day in enumerate(period_days):
            r_day = R.loc[day, tickers].fillna(0.0)
            gross = float((w.values * r_day.values).sum())
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
        held_w = w

    ret_series = pd.Series(daily_returns).sort_index()
    metrics = compute_metrics(
        ret_series, turnover=sum(turnover_history.values()), n_trials=n_trials
    )
    return BacktestResult(
        arm_id=arm_id,
        returns=ret_series,
        weights_history=weights_history,
        turnover_history=turnover_history,
        metrics=metrics,
        diagnostics={"cadence": cadence, "lookback_days": lookback_days,
                     "cost_bps": cost_bps, "n_rebalances": len(rdates)},
    )
