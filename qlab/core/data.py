"""Market data: fetch, cache, and point-in-time snapshots.

Resilience is a first-class concern (spec "Revisions" table). Yahoo Finance
intermittently 429-rate-limits and a hung fetch can stall a live demo, so:

* every network fetch has a hard timeout and falls back to cache;
* an ``offline=True`` flag refuses the network entirely and serves cache/
  synthetic data, so a live demo cannot be taken down by a rate limit;
* when no cache exists either, a **deterministic synthetic generator** produces
  correlated, fat-tailed, regime-switching cross-asset returns — enough to
  exercise the full higher-moment pipeline with zero external dependencies.

Nothing here imports MCP, agents, or a broker.
"""

from __future__ import annotations

import hashlib
import warnings
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from qlab.core.types import DataSnapshot

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CACHE_DIR = _REPO_ROOT / ".lab" / "cache"
_FETCH_TIMEOUT_S = 15  # hard timeout — a hung fetch must not stall the pipeline


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_prices(
    tickers: list[str],
    start: str | date = "2008-01-01",
    end: str | date | None = None,
    *,
    offline: bool = False,
    seed: int = 7,
    cache_dir: Path | None = None,
    refresh: bool = False,
) -> pd.DataFrame:
    """Return an adjusted-close panel (index = dates, columns = ``tickers``).

    Resolution order: parquet cache → yfinance (unless ``offline``) → synthetic.
    The result is always cached so the next call (and any demo) is instant.
    """
    end = end or date.today().isoformat()
    cache_dir = cache_dir or _CACHE_DIR
    key = _cache_key(tickers, start, end)
    cache_path = cache_dir / f"{key}.parquet"

    cached = _read_cache(cache_path)
    cached_is_synthetic = bool(
        cached is not None and cached.attrs.get("synthetic", False)
    )
    # Offline mode may consume any cache. Online mode may consume a real-data
    # cache, but a synthetic fallback must never masquerade as a live prewarm.
    if cached is not None and (
        offline or (not refresh and not cached_is_synthetic)
    ):
        return cached

    df: pd.DataFrame | None = None
    if not offline:
        df = _fetch_yfinance(tickers, str(start), str(end))

    if df is None or df.empty:
        if cached is not None:
            source = cached.attrs.get(
                "source", "synthetic" if cached_is_synthetic else "cache",
            )
            warnings.warn(
                f"market fetch unavailable - serving cached {source} data",
                stacklevel=2,
            )
            return cached
        if offline:
            warnings.warn(
                "offline=True and no cache - serving deterministic synthetic data",
                stacklevel=2,
            )
        else:
            warnings.warn(
                "market fetch unavailable - serving deterministic synthetic data",
                stacklevel=2,
            )
        df = synthetic_prices(tickers, start, end, seed=seed)
    else:
        df.attrs["source"] = "yfinance"
        df.attrs["synthetic"] = False

    _write_cache(cache_path, df)
    return df


def snapshot(
    tickers: list[str],
    as_of: str | date,
    *,
    lookback_days: int | None = None,
    start: str | date = "2008-01-01",
    offline: bool = False,
    seed: int = 7,
) -> DataSnapshot:
    """Build a point-in-time :class:`DataSnapshot` ending at ``as_of``.

    The snapshot truncates to ``as_of`` at construction, so downstream code
    cannot look ahead even by accident.
    """
    as_of_d = _as_date(as_of)
    prices = get_prices(tickers, start=start, end=as_of_d, offline=offline, seed=seed)
    source = prices.attrs.get(
        "source", "synthetic" if prices.attrs.get("synthetic") else "yfinance",
    )
    snap = DataSnapshot(tickers=list(tickers), prices=prices, as_of=as_of_d,
                        source=source)
    if lookback_days is not None:
        snap.prices = snap.window(lookback_days)
    return snap


# ---------------------------------------------------------------------------
# yfinance adapter (lazy import — optional dependency)
# ---------------------------------------------------------------------------
def _fetch_yfinance(tickers: list[str], start: str, end: str) -> pd.DataFrame | None:
    try:
        import yfinance as yf  # noqa: PLC0415  (optional, imported lazily)
    except ImportError:
        return None
    try:
        raw = yf.download(
            tickers,
            start=start,
            end=end,
            auto_adjust=True,
            progress=False,
            timeout=_FETCH_TIMEOUT_S,
        )
        if raw is None or raw.empty:
            return None
        # yfinance returns a column MultiIndex for >1 ticker.
        if isinstance(raw.columns, pd.MultiIndex):
            close = raw["Close"]
        else:
            close = raw[["Close"]].rename(columns={"Close": tickers[0]})
        close = close.reindex(columns=tickers)
        close.index = pd.to_datetime(close.index)
        close = close.dropna(how="all").ffill().dropna(how="any")
        close.attrs["source"] = "yfinance"
        close.attrs["synthetic"] = False
        return close
    except Exception as exc:  # network / parse / rate-limit — degrade gracefully
        warnings.warn(f"yfinance fetch failed ({exc!r})", stacklevel=2)
        return None


# ---------------------------------------------------------------------------
# Deterministic synthetic generator
# ---------------------------------------------------------------------------
def synthetic_prices(
    tickers: list[str],
    start: str | date = "2008-01-01",
    end: str | date | None = None,
    *,
    seed: int = 7,
) -> pd.DataFrame:
    """Generate correlated, fat-tailed, regime-switching cross-asset prices.

    The model is intentionally rich enough to make the higher-moment machinery
    meaningful offline:

    * a small factor structure with **mixed loading signs** → genuinely mixed
      correlation signs (the cross-asset "frustrated landscape");
    * a 2-state regime (calm / stress) with elevated vol and **negative jumps**
      in stress → real coskewness and cokurtosis;
    * Student-t idiosyncratic shocks → fat tails.

    Deterministic in ``seed`` so backtests and tests reproduce bit-for-bit.
    """
    end = end or date.today().isoformat()
    dates = pd.bdate_range(start=str(start), end=str(end))
    n_days = len(dates)
    n = len(tickers)
    rng = np.random.default_rng(seed)

    # --- per-asset base parameters (stable across a given ticker set) --------
    # NB: use a STABLE hash (hashlib), not Python's built-in hash(), which is
    # salted per process (PYTHONHASHSEED) and would make the synthetic feed —
    # and therefore the whole ablation — irreproducible across runs.
    aseed = np.array([int(hashlib.md5(t.encode()).hexdigest(), 16) % 10_000
                      for t in tickers])
    ann_vol = 0.07 + 0.13 * ((aseed % 97) / 97.0)          # 7%–20% annualised
    daily_vol = ann_vol / np.sqrt(252.0)
    drift = 0.035 + 0.055 * ((aseed % 53) / 53.0)          # 3.5%–9% annualised
    daily_drift = drift / 252.0

    # --- factor structure with mixed signs ----------------------------------
    n_factors = 3
    loadings = rng.normal(0.0, 0.6, size=(n, n_factors))
    # bias one factor to be a "risk-off" factor: bonds/gold load negatively
    loadings[:, 0] += rng.uniform(-0.5, 0.5, size=n)
    factor_vol = 0.004                                     # ~6% annualised/factor

    # --- regime process (calm=0, stress=1) ----------------------------------
    p_stay_calm, p_stay_stress = 0.985, 0.94
    regime = np.zeros(n_days, dtype=int)
    for t in range(1, n_days):
        stay = p_stay_calm if regime[t - 1] == 0 else p_stay_stress
        regime[t] = regime[t - 1] if rng.random() < stay else 1 - regime[t - 1]

    # --- simulate returns ----------------------------------------------------
    returns = np.zeros((n_days, n))
    t_df = 6  # Student-t degrees of freedom → fat tails
    for t in range(n_days):
        stress = regime[t] == 1
        vol_mult = 1.8 if stress else 1.0
        factor = rng.standard_t(t_df, size=n_factors) * (factor_vol * vol_mult)
        idio = rng.standard_t(t_df, size=n) * daily_vol * vol_mult
        r = daily_drift + loadings @ factor + idio
        if stress and rng.random() < 0.15:  # occasional downside jump → neg skew
            r -= np.abs(rng.standard_normal(n)) * daily_vol * 1.2
        returns[t] = r

    prices = 100.0 * np.cumprod(1.0 + returns, axis=0)
    df = pd.DataFrame(prices, index=dates, columns=list(tickers))
    df.attrs["synthetic"] = True
    df.attrs["source"] = "synthetic"
    return df


# ---------------------------------------------------------------------------
# cache helpers
# ---------------------------------------------------------------------------
def _cache_key(tickers: list[str], start, end) -> str:
    raw = f"{'-'.join(sorted(tickers))}|{start}|{end}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _read_cache(path: Path) -> pd.DataFrame | None:
    # parquet is preferred, but when pyarrow is absent we fall back to a pickle
    # sidecar on write — so the read path must check BOTH.
    if path.exists():
        try:
            return pd.read_parquet(path)
        except Exception:
            pass
    pkl = path.with_suffix(".pkl")
    if pkl.exists():
        try:
            return pd.read_pickle(pkl)
        except Exception:
            pass
    return None


def _write_cache(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        df.to_parquet(path)
    except Exception:
        # pyarrow not installed — pickle sidecar keeps caching working
        df.to_pickle(path.with_suffix(".pkl"))


def _as_date(d: str | date) -> date:
    return d if isinstance(d, date) else pd.Timestamp(d).date()
