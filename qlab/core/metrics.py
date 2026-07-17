"""Performance metrics for a realized portfolio return series.

Metric set per research-plan §6.2. Two are load-bearing for the thesis:

* **realized skew / kurtosis of the portfolio's own return series** — the direct
  test of the MVSK claim (did optimizing tail shape actually deliver tail shape
  out of sample?);
* **deflated Sharpe** using the registry's trial count — because ~70 quarterly
  rebalance points from 2008 is a *small sample*, so we report the multiple-
  testing-adjusted number, not a naive Sharpe.

Where a point estimate would overclaim on this sample size, prefer the
stationary block-bootstrap confidence interval.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

_TRADING_DAYS = 252


def compute_metrics(
    returns: pd.Series,
    *,
    periods_per_year: int = _TRADING_DAYS,
    turnover: float | None = None,
    n_trials: int = 1,
) -> dict[str, float]:
    """Compute the standard metric bundle for a return series.

    ``returns`` are simple per-period returns of the portfolio.
    """
    r = pd.Series(returns).dropna()
    if len(r) < 3:
        return {"n_obs": int(len(r))}

    ann_return = float((1 + r).prod() ** (periods_per_year / len(r)) - 1)
    ann_vol = float(r.std(ddof=1) * np.sqrt(periods_per_year))
    sharpe = float(ann_return / ann_vol) if ann_vol > 0 else 0.0

    downside = r[r < 0]
    downside_vol = float(downside.std(ddof=1) * np.sqrt(periods_per_year)) if len(downside) > 1 else 0.0
    sortino = float(ann_return / downside_vol) if downside_vol > 0 else 0.0

    out = {
        "n_obs": int(len(r)),
        "ann_return": ann_return,
        "ann_vol": ann_vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_drawdown(r),
        "cvar_95": cvar(r, 0.95),
        "realized_skew": float(stats.skew(r, bias=False)) if len(r) > 3 else 0.0,
        "realized_kurtosis": float(stats.kurtosis(r, fisher=True, bias=False)) if len(r) > 3 else 0.0,
        "deflated_sharpe": deflated_sharpe(r, sharpe_periodic=_periodic_sharpe(r), n_trials=n_trials),
    }
    if turnover is not None:
        out["turnover"] = float(turnover)
    return out


def max_drawdown(returns: pd.Series) -> float:
    """Maximum peak-to-trough drawdown (returned as a negative number)."""
    curve = (1 + returns).cumprod()
    peak = curve.cummax()
    dd = curve / peak - 1.0
    return float(dd.min())


def cvar(returns: pd.Series, level: float = 0.95) -> float:
    """Conditional Value-at-Risk (expected shortfall) at ``level`` (negative)."""
    q = np.quantile(returns, 1 - level)
    tail = returns[returns <= q]
    return float(tail.mean()) if len(tail) else float(q)


def _periodic_sharpe(r: pd.Series) -> float:
    sd = r.std(ddof=1)
    return float(r.mean() / sd) if sd > 0 else 0.0


def deflated_sharpe(
    returns: pd.Series, sharpe_periodic: float, n_trials: int = 1
) -> float:
    """Deflated Sharpe ratio (Bailey & López de Prado).

    Adjusts the observed Sharpe for the number of trials that were run (the
    registry's trial count), the sample length, and the return distribution's
    own skew/kurtosis. Reported as a probability the true Sharpe exceeds 0.
    """
    n = len(returns)
    if n < 4 or sharpe_periodic == 0:
        return 0.0
    g3 = float(stats.skew(returns, bias=False))
    g4 = float(stats.kurtosis(returns, fisher=False, bias=False))  # non-excess

    # Expected maximum Sharpe from N independent trials of pure noise.
    if n_trials > 1:
        e_max = (1 - np.euler_gamma) * stats.norm.ppf(1 - 1.0 / n_trials) + \
            np.euler_gamma * stats.norm.ppf(1 - 1.0 / (n_trials * np.e))
    else:
        e_max = 0.0
    sr0 = e_max  # benchmark Sharpe under the null after trial adjustment

    denom = np.sqrt(
        max(1e-12, 1 - g3 * sharpe_periodic + (g4 - 1) / 4.0 * sharpe_periodic ** 2)
    )
    dsr_stat = (sharpe_periodic - sr0) * np.sqrt(n - 1) / denom
    return float(stats.norm.cdf(dsr_stat))


def block_bootstrap_ci(
    returns: pd.Series,
    stat_fn,
    *,
    block_size: int = 20,
    n_boot: int = 500,
    alpha: float = 0.05,
    seed: int = 7,
) -> tuple[float, float]:
    """Stationary block-bootstrap confidence interval for ``stat_fn(returns)``.

    Preserves serial dependence, which matters on this small, autocorrelated
    sample. Report intervals, not point estimates (research-plan §6.2).
    """
    r = np.asarray(returns.dropna(), dtype=float)
    n = len(r)
    if n < block_size + 1:
        v = float(stat_fn(pd.Series(r)))
        return v, v
    rng = np.random.default_rng(seed)
    stats_out = []
    n_blocks = int(np.ceil(n / block_size))
    for _ in range(n_boot):
        starts = rng.integers(0, n - block_size, size=n_blocks)
        sample = np.concatenate([r[s : s + block_size] for s in starts])[:n]
        stats_out.append(stat_fn(pd.Series(sample)))
    lo = float(np.quantile(stats_out, alpha / 2))
    hi = float(np.quantile(stats_out, 1 - alpha / 2))
    return lo, hi
