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


def fred_series(series_id: str, start: str = "2008-01-01", *,
                cache_dir=".lab/cache", offline: bool = False,
                seed: int = 7) -> pd.Series:
    cache = Path(cache_dir) / f"fred_{series_id}.parquet"
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
    """
    rets = snapshot.log_returns().dropna(how="any")
    if len(rets) < 300:
        return {"regime": "calm", "regime_lambda": 0.0,
                "components": {}, "method": "insufficient_data"}
    turb = turbulence(rets, lookback=252)
    absr = absorption_ratio(rets, window=min(500, len(rets) - 5))
    vol = rets.mean(axis=1).rolling(63).std().dropna() * np.sqrt(_TRADING_DAYS)
    comp = {
        "turbulence_pct": float((turb <= turb.iloc[-1]).mean()),
        "absorption_pct": float((absr <= absr.iloc[-1]).mean()),
        "vol_pct": float((vol <= vol.iloc[-1]).mean()),
    }
    lam = float(np.clip(np.mean(list(comp.values())), 0.0, 1.0))
    return {"regime": "stress" if lam > 0.6 else "calm",
            "regime_lambda": lam, "components": comp, "method": "composite_v1"}
