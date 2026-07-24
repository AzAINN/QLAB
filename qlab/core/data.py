"""Market data: fetch, cache, and point-in-time snapshots.

Resilience is a first-class concern (spec "Revisions" table). Yahoo Finance
intermittently 429-rate-limits and a hung fetch can stall a live demo, so:

* online fetches run through an explicit provider seam and fall back to cache;
* an ``offline=True`` flag refuses the network entirely and serves cache/
  synthetic data, so a live demo cannot be taken down by a rate limit;
* when no cache exists either, a **deterministic synthetic generator** produces
  correlated, fat-tailed, regime-switching cross-asset returns — enough to
  exercise the full higher-moment pipeline with zero external dependencies.

Nothing here imports MCP, agents, or a broker.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
import warnings
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Literal

import numpy as np
import pandas as pd

from qlab.core.types import DataSnapshot
from qlab.paths import state_path

# Test/integration override kept as a module seam; normal runtime resolution is
# dynamic so QLAB_STATE_DIR and an installed command's working directory work.
_CACHE_DIR: Path | None = None
_FETCH_TIMEOUT_S = 15  # hard timeout — a hung fetch must not stall the pipeline
ProviderFetch = Callable[[list[str], str, str], pd.DataFrame | None]
PROVIDERS: dict[str, ProviderFetch] = {}
_CACHE_METADATA_VERSION = 2
_CACHE_LOCK = threading.RLock()


class _InvalidCacheError(RuntimeError):
    """A cache payload exists but its identity cannot be trusted."""


class DataUnavailable(RuntimeError):
    """Required real data could not be served and policy forbids a substitute.

    Raised in place of a silent synthetic/other-provider fallback when the
    active :class:`DataPolicy` forbids one (e.g. ``alpaca_operational``). The
    caller must surface the outage and block, never proceed on fabricated data.
    """


@dataclass(frozen=True)
class DataPolicy:
    """Explicit, immutable description of how market data may be sourced.

    Replaces the single boolean ``offline`` flag, which could not distinguish
    "Alpaca required, no fallback" from "any cache is fine" from "deterministic
    synthetic demo". Only ``provider`` and the ``allow_*`` flags drive
    :func:`get_prices` resolution; ``mode``, ``feed``, ``require_fresh``, and
    ``execution_eligible`` are metadata consumed by the freshness gate, data
    permits, and execution eligibility (later phases).

    The legacy ``offline`` boolean is translated into a permissive demo-grade
    policy (synthetic fallback allowed) at the boundary; new call sites that
    need integrity guarantees pass an explicit operational/historical policy.
    """

    mode: Literal["operational", "historical", "demo", "test"]
    provider: Literal["alpaca", "yfinance", "synthetic"]
    feed: Literal["iex", "sip", "delayed_sip"] | None
    allow_network: bool
    allow_cache: bool
    allow_synthetic: bool
    require_fresh: bool
    execution_eligible: bool

    @classmethod
    def alpaca_operational(cls, feed: str = "iex") -> "DataPolicy":
        """Live paper operations: Alpaca required, no synthetic/other fallback."""
        return cls(mode="operational", provider="alpaca", feed=_check_feed(feed),
                   allow_network=True, allow_cache=True, allow_synthetic=False,
                   require_fresh=True, execution_eligible=True)

    @classmethod
    def alpaca_historical(cls, feed: str = "iex") -> "DataPolicy":
        """Cached/historical Alpaca backtests: real data only, freshness relaxed."""
        return cls(mode="historical", provider="alpaca", feed=_check_feed(feed),
                   allow_network=True, allow_cache=True, allow_synthetic=False,
                   require_fresh=False, execution_eligible=False)

    @classmethod
    def demo(cls, seed: int = 7) -> "DataPolicy":
        """Deterministic synthetic demo: no network, never execution-eligible."""
        return cls(mode="demo", provider="synthetic", feed=None,
                   allow_network=False, allow_cache=True, allow_synthetic=True,
                   require_fresh=False, execution_eligible=False)

    @classmethod
    def test(cls, seed: int = 7) -> "DataPolicy":
        """Test fixtures: synthetic, offline, never execution-eligible."""
        return cls(mode="test", provider="synthetic", feed=None,
                   allow_network=False, allow_cache=True, allow_synthetic=True,
                   require_fresh=False, execution_eligible=False)


_VALID_FEEDS = ("iex", "sip", "delayed_sip")


def _check_feed(feed: str) -> str:
    f = (feed or "").strip().lower()
    if f not in _VALID_FEEDS:
        raise ValueError(
            f"invalid Alpaca feed {feed!r}; expected one of {_VALID_FEEDS}")
    return f


def policy_for(offline: bool, *, provider: str | None = None,
               seed: int = 7) -> DataPolicy:
    """Resolve the runtime's :class:`DataPolicy`, honoring the legacy flags.

    This is the single boundary translation of the CLI/env ``offline`` flag
    into an explicit policy for the owner runtime's new data-plane surfaces:

    - ``offline`` → deterministic synthetic demo (no network, research-grade);
    - online + Alpaca provider → ``alpaca_operational`` (real data, no fallback,
      execution-eligible), feed from ``ALPACA_FEED`` (default ``iex``);
    - online + any other provider → a research-grade policy that permits the
      synthetic fallback but is never execution-eligible.
    """
    if offline:
        return DataPolicy.demo(seed)
    prov = _provider_name(provider)
    if prov == "alpaca":
        return DataPolicy.alpaca_operational(
            os.environ.get("ALPACA_FEED", "iex").strip().lower() or "iex")
    return DataPolicy(
        mode="historical", provider=prov,  # type: ignore[arg-type]
        feed=None, allow_network=True, allow_cache=True, allow_synthetic=True,
        require_fresh=False, execution_eligible=False)


def _effective_policy(
    offline: bool, provider: str | None, policy: DataPolicy | None,
) -> DataPolicy:
    """Resolve the policy that governs one fetch.

    An explicit ``policy`` wins. Otherwise the legacy ``offline`` boolean is
    translated into a demo-grade policy that preserves today's behavior: no
    network when offline, synthetic fallback always permitted, and the
    requested provider kept only as the cache namespace.
    """
    if policy is not None:
        return policy
    return DataPolicy(
        mode="demo",
        provider=_provider_name(provider),  # type: ignore[arg-type]  (namespace only)
        feed=None,
        allow_network=not offline,
        allow_cache=True,
        allow_synthetic=True,
        require_fresh=False,
        execution_eligible=False,
    )


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
    provider: str | None = None,
    policy: DataPolicy | None = None,
) -> pd.DataFrame:
    """Return an adjusted-close panel (index = dates, columns = ``tickers``).

    Resolution is governed by a :class:`DataPolicy`. When ``policy`` is omitted
    the legacy ``offline``/``provider`` arguments are translated into a
    permissive demo-grade policy so existing callers behave exactly as before:
    matching provider-specific cache → selected provider (if network allowed) →
    synthetic. A policy that forbids the synthetic fallback
    (``allow_synthetic=False``) raises :class:`DataUnavailable` instead of
    fabricating data.
    """
    end = end or date.today().isoformat()
    cache_dir = Path(cache_dir) if cache_dir else (_CACHE_DIR or state_path("cache"))

    policy = _effective_policy(offline, provider, policy)
    provider_name = policy.provider
    fetch: ProviderFetch | None = None
    if policy.allow_network:
        provider_name, fetch = _resolve_provider(provider_name)
        # Provider setup is part of selecting an online source, not merely the
        # network fetch. A warm cache must not conceal a broken Alpaca setup.
        _validate_provider_setup(provider_name)

    key = _cache_key(tickers, start, end, provider_name)
    cache_path = cache_dir / f"{key}.parquet"
    legacy_path = cache_dir / f"{_legacy_cache_key(tickers, start, end)}.parquet"

    try:
        cached = _read_cache(
            cache_path,
            provider_name,
            legacy_path=legacy_path if provider_name == "yfinance" else None,
        )
    except _InvalidCacheError as exc:
        if not policy.allow_network:
            raise RuntimeError(
                f"offline cache for provider {provider_name!r} is invalid: {exc}"
            ) from exc
        warnings.warn(
            f"ignoring invalid {provider_name} market cache ({exc}); refetching",
            stacklevel=2,
        )
        cached = None
    if cached is not None and not policy.allow_cache:
        cached = None  # policy forbids consuming any cache
    cached_is_synthetic = bool(
        cached is not None and cached.attrs.get("synthetic", False)
    )
    cached_source = _recorded_source(cached) if cached is not None else None
    # Without network we serve any available cache. With network only a fresh,
    # matching real-provider cache short-circuits the fetch — another provider's
    # cache (or a synthetic fallback) must never masquerade as the live source.
    if cached is not None and (
        not policy.allow_network
        or (
            not refresh
            and not cached_is_synthetic
            and cached_source == provider_name
        )
    ):
        return cached

    df: pd.DataFrame | None = None
    if fetch is not None:
        df = fetch(tickers, str(start), str(end))

    if df is None or df.empty:
        if cached is not None and (
            cached_source == provider_name or cached_is_synthetic
        ):
            warnings.warn(
                f"market fetch unavailable - serving cached {cached_source} data",
                stacklevel=2,
            )
            return cached
        if not policy.allow_synthetic:
            # Fail loud: the policy requires real data and none is available.
            # Never fabricate, never substitute another provider (invariant 4).
            raise DataUnavailable(
                f"no {provider_name} data available for {list(tickers)} "
                f"[{start}..{end}] and the {policy.mode} data policy forbids a "
                "synthetic or cross-provider fallback; refusing to fabricate"
            )
        if not policy.allow_network:
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
        df.attrs["source"] = provider_name
        df.attrs["synthetic"] = False

    df = _normalize_daily_prices(df)
    _write_cache(cache_path, df, provider_name)
    return df


def snapshot(
    tickers: list[str],
    as_of: str | date,
    *,
    lookback_days: int | None = None,
    start: str | date = "2008-01-01",
    offline: bool = False,
    seed: int = 7,
    provider: str | None = None,
    policy: DataPolicy | None = None,
) -> DataSnapshot:
    """Build a point-in-time :class:`DataSnapshot` ending at ``as_of``.

    The snapshot truncates to ``as_of`` at construction, so downstream code
    cannot look ahead even by accident. Data sourcing follows ``policy`` when
    given, else the legacy ``offline``/``provider`` translation.
    """
    as_of_d = _as_date(as_of)
    prices = get_prices(
        tickers,
        start=start,
        end=as_of_d,
        offline=offline,
        seed=seed,
        provider=provider,
        policy=policy,
    )
    source = _recorded_source(prices)
    snap = DataSnapshot(tickers=list(tickers), prices=prices, as_of=as_of_d,
                        source=source)
    if lookback_days is not None:
        snap.prices = snap.window(lookback_days)
    return snap


def cached_provenance(
    tickers: list[str],
    start: str | date = "2008-01-01",
    end: str | date | None = None,
    *,
    cache_dir: Path | None = None,
    provider: str | None = None,
) -> tuple[str, int] | None:
    """Network-free provenance for an already-cached price panel.

    Reads only the on-disk cache — never fetches, never synthesizes — so a
    status poll can surface data source and freshness without ever risking a
    hung network call. Returns ``(source, age_days)`` where ``source`` is the
    panel's recorded provider name (for example, ``"yfinance"``, ``"alpaca"``,
    or ``"synthetic"``) and ``age_days`` is whole days from the last cached bar
    to today. The selected provider determines the cache namespace without
    validating or importing that provider. Returns ``None`` when no valid cache
    exists for this panel (the caller renders that as "no data").
    """
    end = end or date.today().isoformat()
    cache_dir = Path(cache_dir) if cache_dir else (_CACHE_DIR or state_path("cache"))
    provider_name = _provider_name(provider)
    cache_path = cache_dir / (
        f"{_cache_key(tickers, start, end, provider_name)}.parquet"
    )
    legacy_path = cache_dir / f"{_legacy_cache_key(tickers, start, end)}.parquet"
    try:
        cached = _read_cache(
            cache_path,
            provider_name,
            legacy_path=legacy_path if provider_name == "yfinance" else None,
        )
    except _InvalidCacheError:
        return None
    if cached is None or cached.empty:
        return None
    source = _recorded_source(cached)
    last = cached.index[-1]
    last_date = last.date() if hasattr(last, "date") else _as_date(str(last))
    return source, max(0, (date.today() - last_date).days)


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
        close = _normalize_daily_prices(close)
        close = close.dropna(how="all").ffill().dropna(how="any")
        close.attrs["source"] = "yfinance"
        close.attrs["synthetic"] = False
        return close
    except Exception as exc:  # network / parse / rate-limit — degrade gracefully
        warnings.warn(f"yfinance fetch failed ({exc!r})", stacklevel=2)
        return None


# ---------------------------------------------------------------------------
# Alpaca adapter (lazy import — trader extra)
# ---------------------------------------------------------------------------
def _alpaca_dependencies() -> tuple[object, object, object, object]:
    missing = [
        name
        for name in ("ALPACA_API_KEY", "ALPACA_API_SECRET")
        if not os.environ.get(name, "").strip()
    ]
    if missing:
        raise RuntimeError(f"alpaca provider requires {' and '.join(missing)}")

    try:
        from alpaca.data.enums import Adjustment  # noqa: PLC0415
        from alpaca.data.historical import StockHistoricalDataClient  # noqa: PLC0415
        from alpaca.data.requests import StockBarsRequest  # noqa: PLC0415
        from alpaca.data.timeframe import TimeFrame  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "alpaca provider requires the 'alpaca-py' package; "
            "install qlab[trader]"
        ) from exc
    return Adjustment, StockHistoricalDataClient, StockBarsRequest, TimeFrame


def _fetch_alpaca(tickers: list[str], start: str, end: str) -> pd.DataFrame | None:
    (
        Adjustment,
        StockHistoricalDataClient,
        StockBarsRequest,
        TimeFrame,
    ) = _alpaca_dependencies()

    try:
        client = StockHistoricalDataClient(
            os.environ["ALPACA_API_KEY"],
            os.environ["ALPACA_API_SECRET"],
        )
        request = StockBarsRequest(
            symbol_or_symbols=tickers,
            timeframe=TimeFrame.Day,
            start=pd.Timestamp(start).to_pydatetime(),
            end=pd.Timestamp(end).to_pydatetime(),
            adjustment=Adjustment.ALL,
        )
        raw = client.get_stock_bars(request).df
        if raw is None or raw.empty:
            return None

        close_values = raw["close"]
        if isinstance(raw.index, pd.MultiIndex):
            symbol_level = "symbol" if "symbol" in raw.index.names else 0
            close = close_values.unstack(level=symbol_level)
        elif len(tickers) == 1:
            close = close_values.to_frame(name=tickers[0])
        else:
            raise ValueError("Alpaca multi-symbol bars did not include a symbol index")

        close = _normalize_daily_prices(close)
        close = close.sort_index().reindex(columns=tickers)
        close = close.dropna(how="all").ffill().dropna(how="any")
        close.attrs["source"] = "alpaca"
        close.attrs["synthetic"] = False
        return close
    except Exception as exc:  # network / parse / rate-limit — degrade gracefully
        warnings.warn(f"alpaca fetch failed ({exc!r})", stacklevel=2)
        return None


def _fetch_registered_yfinance(
    tickers: list[str], start: str, end: str,
) -> pd.DataFrame | None:
    return _fetch_yfinance(tickers, start, end)


def _fetch_registered_alpaca(
    tickers: list[str], start: str, end: str,
) -> pd.DataFrame | None:
    return _fetch_alpaca(tickers, start, end)


# The small indirection preserves the established monkeypatch seam around the
# concrete adapters while making registration itself public and extensible.
PROVIDERS.update({
    "yfinance": _fetch_registered_yfinance,
    "alpaca": _fetch_registered_alpaca,
})


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
def _normalize_daily_prices(df: pd.DataFrame) -> pd.DataFrame:
    """Canonicalize daily bars to sorted, tz-naive midnight date labels."""
    attrs = dict(df.attrs)
    normalized = df.copy()
    index = pd.DatetimeIndex(pd.to_datetime(normalized.index))
    if index.tz is not None:
        # Preserve the provider's calendar-date label while removing timezone
        # information, then discard daily-bar publication/offset hours.
        index = index.tz_localize(None)
    normalized.index = index.normalize()
    normalized = normalized.sort_index()
    normalized.attrs.update(attrs)
    return normalized


def _provider_name(provider: str | None) -> str:
    return (provider or os.environ.get("QLAB_DATA_PROVIDER") or "yfinance").strip().lower()


def _legacy_cache_key(tickers: list[str], start, end) -> str:
    raw = f"{'-'.join(sorted(tickers))}|{start}|{end}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _cache_key(tickers: list[str], start, end, provider: str) -> str:
    raw = f"{provider}|{'-'.join(sorted(tickers))}|{start}|{end}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _resolve_provider(provider: str | None) -> tuple[str, ProviderFetch]:
    name = _provider_name(provider)
    try:
        return name, PROVIDERS[name]
    except KeyError as exc:
        available = ", ".join(sorted(PROVIDERS))
        raise RuntimeError(
            f"unknown market data provider {name!r}; available providers: {available}"
        ) from exc


def _validate_provider_setup(provider: str) -> None:
    if provider == "alpaca":
        _alpaca_dependencies()


def _recorded_source(df: pd.DataFrame) -> str:
    source = df.attrs.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("market data provenance is missing")
    return source.strip().lower()


def _cache_metadata_path(path: Path) -> Path:
    return path.with_suffix(".metadata.json")


def _payload_identity(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as payload:
        for chunk in iter(lambda: payload.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _read_cache(
    path: Path,
    expected_provider: str,
    *,
    legacy_path: Path | None = None,
) -> pd.DataFrame | None:
    with _CACHE_LOCK:
        payload_exists = path.exists() or path.with_suffix(".pkl").exists()
        metadata_exists = _cache_metadata_path(path).exists()
        if not payload_exists and not metadata_exists and legacy_path is not None:
            legacy_exists = (
                legacy_path.exists()
                or legacy_path.with_suffix(".pkl").exists()
            )
            if legacy_exists:
                return _migrate_legacy_cache(
                    legacy_path, path, expected_provider)
        return _read_current_cache(path, expected_provider)


def _read_current_cache(
    path: Path,
    expected_provider: str,
) -> pd.DataFrame | None:
    pkl = path.with_suffix(".pkl")
    metadata_path = _cache_metadata_path(path)
    payload_exists = path.exists() or pkl.exists()
    if not payload_exists and not metadata_path.exists():
        return None
    if not payload_exists:
        raise _InvalidCacheError("provenance metadata exists without a cache payload")
    if not metadata_path.exists():
        raise _InvalidCacheError("cache payload has no provenance metadata")

    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _InvalidCacheError("cache provenance metadata is unreadable") from exc
    if not isinstance(metadata, dict):
        raise _InvalidCacheError("cache provenance metadata is not an object")
    if metadata.get("version") != _CACHE_METADATA_VERSION:
        raise _InvalidCacheError("cache provenance metadata version is unsupported")

    provider = metadata.get("provider")
    source = metadata.get("source")
    synthetic = metadata.get("synthetic")
    storage = metadata.get("storage")
    payload_file = metadata.get("payload_file")
    payload_sha256 = metadata.get("payload_sha256")
    payload_size = metadata.get("payload_size")
    if provider != expected_provider:
        raise _InvalidCacheError(
            f"cache belongs to provider {provider!r}, not {expected_provider!r}"
        )
    if not isinstance(source, str) or not source:
        raise _InvalidCacheError("cache source provenance is missing")
    if not isinstance(synthetic, bool):
        raise _InvalidCacheError("cache synthetic provenance is missing")
    if source == "synthetic":
        if not synthetic:
            raise _InvalidCacheError("synthetic cache provenance is inconsistent")
    elif source != expected_provider or synthetic:
        raise _InvalidCacheError(
            f"cache source {source!r} is invalid for provider {expected_provider!r}"
        )

    if storage == "parquet":
        payload_path = path
        reader = pd.read_parquet
    elif storage == "pickle":
        payload_path = pkl
        reader = pd.read_pickle
    else:
        raise _InvalidCacheError("cache storage format is unknown")
    if not payload_path.exists():
        raise _InvalidCacheError(
            f"cache metadata points to missing {storage} payload"
        )
    if payload_file != payload_path.name:
        raise _InvalidCacheError(
            "cache metadata points to an unexpected payload file"
        )
    if (
        not isinstance(payload_sha256, str)
        or len(payload_sha256) != 64
        or type(payload_size) is not int
        or payload_size < 0
    ):
        raise _InvalidCacheError("cache payload identity metadata is missing")
    try:
        identity_before = _payload_identity(payload_path)
    except OSError as exc:
        raise _InvalidCacheError(
            f"cache {storage} payload is unreadable") from exc
    if identity_before != (payload_sha256, payload_size):
        raise _InvalidCacheError(
            "cache payload identity does not match provenance metadata"
        )
    try:
        df = reader(payload_path)
    except Exception as exc:
        raise _InvalidCacheError(f"cache {storage} payload is unreadable") from exc
    if not isinstance(df, pd.DataFrame):
        raise _InvalidCacheError("cache payload is not a DataFrame")
    try:
        identity_after = _payload_identity(payload_path)
    except OSError as exc:
        raise _InvalidCacheError(
            f"cache {storage} payload changed while being read") from exc
    if identity_after != identity_before:
        raise _InvalidCacheError(
            f"cache {storage} payload changed while being read"
        )

    df = _normalize_daily_prices(df)
    df.attrs.clear()
    df.attrs.update({"source": source, "synthetic": synthetic})
    return df


def _migrate_legacy_cache(
    legacy_path: Path,
    current_path: Path,
    expected_provider: str,
) -> pd.DataFrame:
    if expected_provider != "yfinance":
        raise _InvalidCacheError(
            "legacy cache can only be migrated as provider 'yfinance'"
        )
    legacy_pkl = legacy_path.with_suffix(".pkl")
    candidates = [
        (legacy_path, "parquet", pd.read_parquet),
        (legacy_pkl, "pickle", pd.read_pickle),
    ]
    existing = [
        (payload_path, storage, reader)
        for payload_path, storage, reader in candidates
        if payload_path.exists()
    ]
    if len(existing) != 1:
        raise _InvalidCacheError(
            "legacy cache must contain exactly one readable payload"
        )
    payload_path, storage, reader = existing[0]
    try:
        legacy = reader(payload_path)
    except Exception as exc:
        raise _InvalidCacheError(
            f"legacy cache {storage} payload is unreadable") from exc
    if not isinstance(legacy, pd.DataFrame):
        raise _InvalidCacheError("legacy cache payload is not a DataFrame")

    source = legacy.attrs.get("source")
    synthetic = legacy.attrs.get("synthetic")
    if not isinstance(source, str) or not source.strip():
        raise _InvalidCacheError("legacy cache provenance is missing")
    source = source.strip().lower()
    if not isinstance(synthetic, bool):
        raise _InvalidCacheError("legacy cache synthetic provenance is missing")
    if source == "synthetic":
        if not synthetic:
            raise _InvalidCacheError(
                "legacy synthetic cache provenance is inconsistent")
    elif source != "yfinance" or synthetic:
        raise _InvalidCacheError(
            f"legacy cache source {source!r} is not valid yfinance provenance"
        )

    legacy = _normalize_daily_prices(legacy)
    legacy.attrs.clear()
    legacy.attrs.update({"source": source, "synthetic": synthetic})
    _write_cache(current_path, legacy, "yfinance")
    migrated = _read_current_cache(current_path, "yfinance")
    if migrated is None:
        raise _InvalidCacheError("legacy cache migration did not publish a cache")
    return migrated


def _write_cache(path: Path, df: pd.DataFrame, provider: str) -> None:
    source = _recorded_source(df)
    synthetic = df.attrs.get("synthetic")
    if not isinstance(synthetic, bool):
        raise ValueError("market data synthetic provenance is missing")
    if source == "synthetic":
        if not synthetic:
            raise ValueError("synthetic market data provenance is inconsistent")
    elif source != provider or synthetic:
        raise ValueError(
            f"market data source {source!r} does not match provider {provider!r}"
        )

    with _CACHE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        parquet_temp = path.with_name(f".{path.name}.{token}.tmp")
        pkl_path = path.with_suffix(".pkl")
        pickle_temp = pkl_path.with_name(f".{pkl_path.name}.{token}.tmp")
        metadata_path = _cache_metadata_path(path)
        metadata_temp = metadata_path.with_name(
            f".{metadata_path.name}.{token}.tmp")
        temporary_paths = (parquet_temp, pickle_temp, metadata_temp)
        try:
            try:
                df.to_parquet(parquet_temp)
                storage = "parquet"
                payload_temp = parquet_temp
                payload_path = path
                stale_payload = pkl_path
            except Exception:
                parquet_temp.unlink(missing_ok=True)
                # pyarrow not installed — pickle sidecar keeps caching working
                df.to_pickle(pickle_temp)
                storage = "pickle"
                payload_temp = pickle_temp
                payload_path = pkl_path
                stale_payload = path

            payload_sha256, payload_size = _payload_identity(payload_temp)
            metadata = {
                "version": _CACHE_METADATA_VERSION,
                "provider": provider,
                "source": source,
                "synthetic": synthetic,
                "storage": storage,
                "payload_file": payload_path.name,
                "payload_sha256": payload_sha256,
                "payload_size": payload_size,
            }
            metadata_temp.write_text(
                json.dumps(metadata, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            payload_temp.replace(payload_path)
            metadata_temp.replace(metadata_path)
            stale_payload.unlink(missing_ok=True)
        finally:
            for temporary_path in temporary_paths:
                temporary_path.unlink(missing_ok=True)


def _as_date(d: str | date) -> date:
    return d if isinstance(d, date) else pd.Timestamp(d).date()
