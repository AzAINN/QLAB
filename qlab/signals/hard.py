"""Deterministic market-stress signals - the injection-immune half of the
signals layer (roadmap Amendment A / §3). Everything here is computed from
prices or fetched from fixed public index series; no text, no LLM.
"""
from __future__ import annotations

import hashlib
from math import ceil
from pathlib import Path

import numpy as np
import pandas as pd

from qlab.core.types import DataSnapshot
from qlab.paths import state_path

_TRADING_DAYS = 252


def turbulence(returns: pd.DataFrame, lookback: int = 252) -> pd.Series:
    """Chow-Kritzman turbulence: d_t = (r_t - mu)' Sigma^-1 (r_t - mu)."""
    out = {}
    X = returns.to_numpy(dtype=float)
    for t in range(lookback, len(returns)):
        W = X[t - lookback:t]
        mu = W.mean(axis=0)
        inv = np.linalg.pinv(np.cov(W, rowvar=False))
        d = X[t] - mu
        out[returns.index[t]] = float(d @ inv @ d)
    return pd.Series(out)


def absorption_ratio(returns: pd.DataFrame, window: int = 500,
                     n_components: int | None = None, step: int = 5) -> pd.Series:
    """Kritzman et al.: share of variance absorbed by the top eigenvectors."""
    n = returns.shape[1]
    k = n_components or max(1, ceil(n / 5))
    out = {}
    X = returns.to_numpy(dtype=float)
    for t in range(window, len(returns), step):
        vals = np.linalg.eigvalsh(np.cov(X[t - window:t], rowvar=False))
        out[returns.index[t]] = float(vals[-k:].sum() / max(vals.sum(), 1e-18))
    return pd.Series(out)


def volatility_term_structure(returns: pd.DataFrame, short: int = 21,
                              long: int = 126) -> pd.Series:
    """Ratio of short- to long-horizon annualised vol of the equal-weight book.

    Above 1 means recent variance runs hot relative to its own longer baseline
    (a vol shock building); below 1 means it is calming. A scale-free read on
    whether variance is *accelerating* — orthogonal to its absolute level, which
    the turbulence and trailing-vol signals already cover.
    """
    port = returns.mean(axis=1)
    short_vol = port.rolling(short).std() * np.sqrt(_TRADING_DAYS)
    long_vol = port.rolling(long).std() * np.sqrt(_TRADING_DAYS)
    return (short_vol / long_vol).replace([np.inf, -np.inf], np.nan).dropna()


def drawdown_pressure(returns: pd.DataFrame) -> pd.Series:
    """Equal-weight peak-to-trough drawdown depth (>= 0) at each date.

    The directional axis the symmetric variance measures miss: a market can
    grind steadily lower at low volatility, or spike violently and recover.
    Depth below the trailing high is what separates the two.
    """
    equity = np.exp(returns.mean(axis=1).cumsum())
    return (1.0 - equity / equity.cummax()).clip(lower=0.0)


def downside_tail(returns: pd.DataFrame, window: int = 63) -> pd.Series:
    """Rolling ratio of downside to upside semi-deviation of the equal-weight book.

    Above 1 means losses are more dispersed than gains — a fat, asymmetric left
    tail the second moment alone cannot see. Windows without at least two up- and
    two down-days are dropped rather than divided toward a spurious value.
    """
    port = returns.mean(axis=1)

    def _ratio(x: np.ndarray) -> float:
        down, up = x[x < 0.0], x[x > 0.0]
        if down.size < 2 or up.size < 2:
            return np.nan
        upper = up.std()
        return float(down.std() / upper) if upper > 1e-12 else np.nan

    return (port.rolling(window).apply(_ratio, raw=True)
            .replace([np.inf, -np.inf], np.nan).dropna())


def fred_series(series_id: str, start: str = "2008-01-01", *,
                cache_dir: str | Path | None = None, offline: bool = False,
                seed: int = 7) -> pd.Series:
    root = Path(cache_dir) if cache_dir else state_path("cache")
    cache = root / f"fred_{series_id}.parquet"
    if cache.exists():
        return pd.read_parquet(cache)["value"]
    if offline:
        # deterministic synthetic stand-in (positive, VIX-like mean level)
        h = int(hashlib.md5(f"{series_id}:{seed}".encode()).hexdigest()[:8], 16)
        rng = np.random.default_rng(h)
        idx = pd.bdate_range(start, periods=1500)
        level = 18.0 * np.exp(np.cumsum(rng.normal(0, 0.03, len(idx))
                                        - 0.0005))            # mean-ish reverting
        return pd.Series(level, index=idx, name="value")
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    df = pd.read_csv(url, index_col=0, parse_dates=True, na_values=".")
    s = df.iloc[:, 0].dropna().astype(float).rename("value")
    s = s[s.index >= pd.Timestamp(start)]
    cache.parent.mkdir(parents=True, exist_ok=True)
    s.to_frame().to_parquet(cache)
    return s


def composite_regime(snapshot: DataSnapshot, *, offline: bool = False) -> dict:
    """Blend price-based stress signals into one lambda in [0, 1].

    lambda = mean of (turbulence percentile, absorption percentile, trailing-vol
    percentile), each computed against the snapshot's own history. VIX-family
    series can sharpen this when cached; the price-only version is always
    available and is the referee-auditable floor.

    ``absorption_pct`` is only included (and thus only enters the mean) when
    its underlying series has at least 10 points — with fewer points the
    ``(absr <= absr.iloc[-1]).mean()`` percentile degenerates (often to a
    trivial 1.0), which would pin lambda high regardless of actual factor
    concentration.
    """
    rets = snapshot.log_returns().dropna(how="any")
    if len(rets) < 300:
        return {"regime": "calm", "regime_lambda": 0.0,
                "components": {}, "method": "insufficient_data"}
    turb = turbulence(rets, lookback=252)
    window = min(500, max(150, len(rets) // 2))
    absr = absorption_ratio(rets, window=window)
    vol = rets.mean(axis=1).rolling(63).std().dropna() * np.sqrt(_TRADING_DAYS)
    comp = {
        "turbulence_pct": float((turb <= turb.iloc[-1]).mean()),
        "vol_pct": float((vol <= vol.iloc[-1]).mean()),
    }
    if len(absr) >= 10:
        comp["absorption_pct"] = float((absr <= absr.iloc[-1]).mean())
    lam = float(np.clip(np.mean(list(comp.values())), 0.0, 1.0))
    return {"regime": "stress" if lam > 0.6 else "calm",
            "regime_lambda": lam, "components": comp, "method": "composite_v1"}
