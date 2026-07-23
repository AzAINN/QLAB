"""Agent-facing regime indicators — the moments-analyst's judgment inputs.

Each function reads a point-in-time :class:`DataSnapshot` and returns ONE regime
reading in a shared schema, so the analyst can call several, weigh them on the
same axes (``signal`` · its own historical ``threshold`` · ``percentile``), and
defend a single regime call before the optimizer touches a number. Every
indicator is deterministic and price-only — no return forecast, no text, no LLM
— consistent with the rest of :mod:`qlab.signals`.

The five deliberately cover different faces of market variability:

    turbulence                 how statistically unusual the latest joint move is
    absorption                 how tightly assets are coupled (systemic fragility)
    volatility_term_structure  whether variance is accelerating or mean-reverting
    drawdown                   directional stress: depth below the trailing peak
    tail_risk                  downside asymmetry: how fat the recent left tail is

An indicator classifies the current reading against the tail of its OWN trailing
history (an 80th-percentile default), so "stress" always means *unusual for this
market*, never a hard-coded level. Too little history to judge is raised, not
silently defaulted (invariant 4, fail-loud).

The shared shape lets the analyst compare unlike indicators directly and record
one auditable regime decision:

    {"indicator", "method", "regime": "calm"|"stress", "signal", "threshold",
     "percentile", "window", "reasoning", ...indicator-specific extras}
"""
from __future__ import annotations

from math import ceil

import numpy as np

from qlab.core.types import DataSnapshot
from qlab.signals import hard

# A percentile taken over fewer points than this is too coarse to classify
# against — the tail quantile degenerates — so we refuse rather than guess.
_MIN_POINTS = 20


def _classify(series, current: float, quantile: float, *,
              higher_is_stress: bool = True) -> tuple[str, float, float]:
    """Label ``current`` against the tail of its own history.

    Returns ``(regime, threshold, percentile)``. ``higher_is_stress`` flips the
    tail for indicators where a *low* reading is the stressed one.
    """
    arr = np.asarray(series, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size < _MIN_POINTS or not np.isfinite(current):
        raise ValueError(
            "insufficient history to classify this regime indicator; widen "
            "lookback_days")
    edge = quantile if higher_is_stress else 1.0 - quantile
    threshold = float(np.quantile(arr, edge))
    percentile = float((arr <= current).mean())
    stressed = current > threshold if higher_is_stress else current < threshold
    return ("stress" if stressed else "calm"), threshold, percentile


def _reading(indicator: str, method: str, regime: str, signal: float,
             threshold: float, percentile: float, window: int,
             reasoning: str, **extra) -> dict:
    out = {
        "indicator": indicator,
        "method": method,
        "regime": regime,
        "signal": round(float(signal), 6),
        "threshold": round(float(threshold), 6),
        "percentile": round(float(percentile), 4),
        "window": int(window),
        "reasoning": " ".join(reasoning.split()),
    }
    out.update(extra)
    return out


def _returns(snapshot: DataSnapshot):
    return snapshot.log_returns().dropna(how="any")


def turbulence_regime(snapshot: DataSnapshot, *, lookback: int = 252,
                      recent: int = 5, quantile: float = 0.80) -> dict:
    """Chow–Kritzman turbulence: is the latest cross-asset move statistically
    unusual relative to the trailing joint distribution of returns?"""
    rets = _returns(snapshot)
    n = rets.shape[1]
    turb = hard.turbulence(rets, lookback=lookback) / n   # per degree of freedom
    current = float(turb.tail(recent).mean()) if len(turb) else float("nan")
    regime, threshold, pct = _classify(turb, current, quantile)
    reasoning = (
        f"The last {recent} sessions' cross-asset move sits at the {pct:.0%} "
        f"percentile of {lookback}-day turbulence: "
        + ("a statistically unusual joint move — stress." if regime == "stress"
           else "within the historical joint-move norm — calm."))
    return _reading("turbulence", "chow_kritzman_turbulence", regime, current,
                    threshold, pct, lookback, reasoning,
                    recent_days=recent, n_assets=n)


def absorption_regime(snapshot: DataSnapshot, *, window: int = 252,
                      step: int = 5, quantile: float = 0.80) -> dict:
    """Kritzman absorption ratio: what share of variance the top eigenvectors
    absorb — high means tightly coupled assets and fragile diversification."""
    rets = _returns(snapshot)
    absr = hard.absorption_ratio(rets, window=window, step=step)
    current = float(absr.iloc[-1]) if len(absr) else float("nan")
    regime, threshold, pct = _classify(absr, current, quantile)
    k = max(1, ceil(rets.shape[1] / 5))
    reasoning = (
        f"The top {k} eigenvector(s) absorb {current:.0%} of variance "
        f"({pct:.0%} percentile): "
        + ("assets are tightly coupled, so diversification is fragile — stress."
           if regime == "stress" else
           "risk is spread across factors, so the market is loosely coupled — "
           "calm."))
    return _reading("absorption", "kritzman_absorption_ratio", regime, current,
                    threshold, pct, window, reasoning, top_eigenvectors=k)


def volatility_term_structure(snapshot: DataSnapshot, *, short: int = 21,
                              long: int = 126, quantile: float = 0.80) -> dict:
    """Short- over long-horizon realised vol: is variance accelerating (a shock
    building) or stable to mean-reverting?"""
    rets = _returns(snapshot)
    ratio = hard.volatility_term_structure(rets, short=short, long=long)
    current = float(ratio.iloc[-1]) if len(ratio) else float("nan")
    regime, threshold, pct = _classify(ratio, current, quantile)
    reasoning = (
        f"{short}-day vol is {current:.2f}x the {long}-day baseline "
        f"({pct:.0%} percentile): "
        + ("variance is accelerating, a vol shock is building — stress."
           if regime == "stress" else
           "variance is stable to mean-reverting — calm."))
    return _reading("volatility_term_structure", "short_over_long_realized_vol",
                    regime, current, threshold, pct, short, reasoning,
                    long_window=long)


def drawdown_regime(snapshot: DataSnapshot, *, trend_window: int = 200,
                    quantile: float = 0.80) -> dict:
    """Equal-weight peak-to-trough depth plus a trend filter: the directional
    stress axis the symmetric variance measures cannot see."""
    rets = _returns(snapshot)
    depth = hard.drawdown_pressure(rets)
    current = float(depth.iloc[-1]) if len(depth) else float("nan")
    regime, threshold, pct = _classify(depth, current, quantile)
    equity = np.exp(rets.mean(axis=1).cumsum())
    trend_window = min(trend_window, len(equity))
    below_trend = bool(
        equity.iloc[-1] < float(equity.rolling(trend_window).mean().iloc[-1]))
    reasoning = (
        f"The equal-weight book is {current:.1%} below its trailing peak "
        f"({pct:.0%} percentile) and {'below' if below_trend else 'above'} its "
        f"{trend_window}-day trend: "
        + ("a directional drawdown episode — stress." if regime == "stress"
           else "no material drawdown pressure — calm."))
    return _reading("drawdown", "peak_to_trough_with_trend", regime, current,
                    threshold, pct, trend_window, reasoning,
                    below_trend=below_trend)


def tail_risk_regime(snapshot: DataSnapshot, *, window: int = 63,
                     quantile: float = 0.80) -> dict:
    """Downside-over-upside semi-deviation with recent realised skew: how fat
    and asymmetric the left tail has become."""
    rets = _returns(snapshot)
    ratio = hard.downside_tail(rets, window=window)
    current = float(ratio.iloc[-1]) if len(ratio) else float("nan")
    regime, threshold, pct = _classify(ratio, current, quantile)
    recent_skew = float(rets.mean(axis=1).tail(window).skew())
    reasoning = (
        f"Downside vol is {current:.2f}x upside over {window} days "
        f"(skew {recent_skew:+.2f}, {pct:.0%} percentile): "
        + ("a fat, asymmetric left tail — stress." if regime == "stress"
           else "returns are roughly symmetric — calm."))
    return _reading("tail_risk", "downside_upside_semideviation_ratio", regime,
                    current, threshold, pct, window, reasoning,
                    recent_skew=round(recent_skew, 4))


# Registry the tool layer and tests iterate over — one entry per exposed tool.
INDICATORS = {
    "turbulence": turbulence_regime,
    "absorption": absorption_regime,
    "volatility_term_structure": volatility_term_structure,
    "drawdown": drawdown_regime,
    "tail_risk": tail_risk_regime,
}
