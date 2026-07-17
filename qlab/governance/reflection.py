"""Resolve logged judgments against point-in-time realized outcomes.

The reflection loop scores what an agent actually chose: its target portfolio,
volatility estimate, and regime call. Numbers and classifications are computed
deterministically here; an LLM may later summarize the stored evidence, but it
does not get to manufacture the outcome.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

_TRADING_DAYS = 252
_REGIME_QUANTILE = 0.80


def resolve_pending(registry, prices: pd.DataFrame, horizon_days: int = 63) -> int:
    """Resolve eligible pending decisions exactly once.

    A decision becomes eligible after ``horizon_days`` trading observations are
    available strictly after its ``as_of`` date. Realized volatility uses the
    fixed target weights recorded with the decision. The realized regime uses
    the same trailing-volatility idea as ``detect_regime``: compare the future
    horizon with the 80th percentile of historical rolling volatility that was
    available at decision time.
    """
    if horizon_days < 2:
        raise ValueError("horizon_days must be at least 2")
    if prices.empty:
        return 0

    panel = prices.sort_index()
    index = pd.DatetimeIndex(panel.index)
    resolved = 0

    for decision in registry.pending_decisions():
        as_of = pd.Timestamp(decision["as_of"])
        future_positions = np.flatnonzero(index > as_of)
        if len(future_positions) < horizon_days:
            continue

        choice = decision.get("choice") or {}
        targets = choice.get("targets") or {}
        columns = list(targets)
        if not columns or any(column not in panel.columns for column in columns):
            continue

        weights = np.asarray([targets[column] for column in columns], dtype=float)
        first_future = int(future_positions[0])
        if first_future == 0:
            continue

        # Include the last price known before the first future observation so
        # the first realized return is not silently discarded.
        realized_positions = np.concatenate(
            ([first_future - 1], future_positions[:horizon_days])
        )
        realized_prices = panel.iloc[realized_positions][columns]
        realized_returns = np.log(
            realized_prices / realized_prices.shift(1)
        ).dropna(how="any")
        if len(realized_returns) < horizon_days:
            continue

        portfolio_returns = realized_returns.to_numpy(dtype=float) @ weights
        realized_vol = float(
            np.std(portfolio_returns, ddof=1) * np.sqrt(_TRADING_DAYS)
        )

        historical_prices = panel.loc[index <= as_of, columns]
        historical_returns = np.log(
            historical_prices / historical_prices.shift(1)
        ).dropna(how="any")
        historical_portfolio = historical_returns.to_numpy(dtype=float) @ weights
        rolling_vol = (
            pd.Series(historical_portfolio)
            .rolling(horizon_days)
            .std(ddof=1)
            .dropna()
            * np.sqrt(_TRADING_DAYS)
        )
        regime_threshold = (
            float(rolling_vol.quantile(_REGIME_QUANTILE))
            if not rolling_vol.empty
            else 0.18
        )

        estimated_vol = float(choice.get("est_vol") or 0.0)
        regime_call = str(choice.get("regime", "unknown"))
        regime_realized = "stress" if realized_vol > regime_threshold else "calm"
        vol_ratio = realized_vol / estimated_vol if estimated_vol > 0 else None
        regime_consistent = regime_call == regime_realized

        outcome = {
            "realized_vol": realized_vol,
            "est_vol": estimated_vol,
            "vol_ratio": vol_ratio,
            "horizon_days": horizon_days,
            "window_start": str(realized_returns.index[0].date()),
            "window_end": str(realized_returns.index[-1].date()),
            "regime_call": regime_call,
            "regime_realized": regime_realized,
            "regime_threshold": regime_threshold,
            "regime_consistent": regime_consistent,
        }

        if vol_ratio is None:
            estimate_assessment = "No positive volatility estimate was recorded."
        elif vol_ratio > 1.5:
            estimate_assessment = (
                "The volatility estimate was materially low; revisit the "
                "window and shrinkage choice."
            )
        elif vol_ratio < (1.0 / 1.5):
            estimate_assessment = (
                "The volatility estimate was materially high; revisit the "
                "window and shrinkage choice."
            )
        else:
            estimate_assessment = "The volatility estimate held up over this horizon."

        ratio_text = "n/a" if vol_ratio is None else f"{vol_ratio:.2f}"
        reflection = (
            f"Realized annualized vol {realized_vol:.1%} versus estimated "
            f"{estimated_vol:.1%} (ratio {ratio_text}). Regime call "
            f"'{regime_call}' was "
            f"{'consistent with' if regime_consistent else 'contradicted by'} "
            f"the realized '{regime_realized}' regime. {estimate_assessment}"
        )
        registry.update_reflection(decision["decision_id"], outcome, reflection)
        registry.record_event(
            "reflection_resolved",
            {
                "decision_id": decision["decision_id"],
                "realized_vol": realized_vol,
                "regime_consistent": regime_consistent,
            },
        )
        resolved += 1

    return resolved
