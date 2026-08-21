"""Provider selection and provenance tests for market data."""

from __future__ import annotations

import sys
from datetime import date
from types import ModuleType, SimpleNamespace

import pandas as pd
import pytest

from qlab.core import data as market
from qlab.core.types import DataSnapshot


def _panel(tickers: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            ticker: [100.0 + index, 101.0 + index]
            for index, ticker in enumerate(tickers)
        },
        index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
    )


def _warm_alpaca_cache(tmp_path, monkeypatch) -> pd.DataFrame:
    original_validator = market._validate_provider_setup
    monkeypatch.setattr(market, "_validate_provider_setup", lambda provider: None)
    monkeypatch.setitem(
        market.PROVIDERS,
        "alpaca",
        lambda tickers, start, end: _panel(tickers),
    )
    prices = market.get_prices(
        ["AAA"],
        "2024-01-01",
        "2024-01-05",
        provider="alpaca",
        cache_dir=tmp_path,
    )
    monkeypatch.setattr(market, "_validate_provider_setup", original_validator)
    return prices


def test_registered_provider_records_name_in_cache_provenance(
    tmp_path, monkeypatch,
):
    calls = []

    def stub(tickers, start, end):
        calls.append((tickers, start, end))
        return _panel(tickers)

    monkeypatch.setitem(market.PROVIDERS, "stub", stub)
    prices = market.get_prices(
        ["AAA", "BBB"],
        "2024-01-01",
        "2024-01-05",
        provider="stub",
        cache_dir=tmp_path,
    )

    assert calls == [(["AAA", "BBB"], "2024-01-01", "2024-01-05")]
    assert prices.attrs == {"source": "stub", "synthetic": False}
    provenance = market.cached_provenance(
        ["AAA", "BBB"],
        "2024-01-01",
        "2024-01-05",
        cache_dir=tmp_path,
        provider="stub",
    )
    assert provenance is not None
    assert provenance[0] == "stub"


def test_yfinance_remains_the_default_provider(tmp_path, monkeypatch):
    calls = []

    def stub_yfinance(tickers, start, end):
        calls.append((tickers, start, end))
        return _panel(tickers)

    monkeypatch.delenv("QLAB_DATA_PROVIDER", raising=False)
    monkeypatch.setattr(market, "_fetch_yfinance", stub_yfinance)
    prices = market.get_prices(
        ["AAA"], "2024-01-01", "2024-01-05", cache_dir=tmp_path,
    )

    assert calls
    assert prices.attrs["source"] == "yfinance"


def test_provider_environment_variable_is_respected(tmp_path, monkeypatch):
    calls = []

    def stub(tickers, start, end):
        calls.append((tickers, start, end))
        return _panel(tickers)

    monkeypatch.setitem(market.PROVIDERS, "stub", stub)
    monkeypatch.setenv("QLAB_DATA_PROVIDER", "stub")
    prices = market.get_prices(
        ["AAA"], "2024-01-01", "2024-01-05", cache_dir=tmp_path,
    )

    assert calls
    assert prices.attrs["source"] == "stub"


def test_daily_index_is_normalized_for_every_registered_provider(
    tmp_path, monkeypatch,
):
    def stub(tickers, start, end):
        return pd.DataFrame(
            {ticker: [100.0] for ticker in tickers},
            index=pd.DatetimeIndex(["2024-09-30 04:00"], tz="UTC"),
        )

    monkeypatch.setitem(market.PROVIDERS, "stub", stub)
    prices = market.get_prices(
        ["AAA"],
        "2024-09-01",
        "2024-10-01",
        provider="stub",
        cache_dir=tmp_path,
    )

    assert prices.index.equals(pd.DatetimeIndex(["2024-09-30"]))


def test_cache_identity_is_provider_specific(tmp_path, monkeypatch):
    calls: list[str] = []

    def provider_a(tickers, start, end):
        calls.append("provider-a")
        return _panel(tickers)

    def provider_b(tickers, start, end):
        calls.append("provider-b")
        return _panel(tickers) + 1_000.0

    monkeypatch.setitem(market.PROVIDERS, "provider-a", provider_a)
    monkeypatch.setitem(market.PROVIDERS, "provider-b", provider_b)
    kwargs = {
        "tickers": ["AAA"],
        "start": "2024-01-01",
        "end": "2024-01-05",
        "cache_dir": tmp_path,
    }

    from_a = market.get_prices(provider="provider-a", **kwargs)
    from_b = market.get_prices(provider="provider-b", **kwargs)
    from_a_again = market.get_prices(provider="provider-a", **kwargs)

    assert calls == ["provider-a", "provider-b"]
    assert from_a_again.equals(from_a)
    assert from_b.iloc[0, 0] == from_a.iloc[0, 0] + 1_000.0
    assert len(list(tmp_path.glob("*.metadata.json"))) == 2
    payloads = list(tmp_path.glob("*.parquet")) + list(tmp_path.glob("*.pkl"))
    assert len(payloads) == 2


def test_parquet_cache_provenance_survives_attrless_read(
    tmp_path, monkeypatch,
):
    calls = []

    def stub(tickers, start, end):
        calls.append((tickers, start, end))
        return _panel(tickers)

    monkeypatch.setitem(market.PROVIDERS, "stub", stub)
    kwargs = {
        "tickers": ["AAA"],
        "start": "2024-01-01",
        "end": "2024-01-05",
        "provider": "stub",
        "cache_dir": tmp_path,
    }
    market.get_prices(**kwargs)

    read_parquet = market.pd.read_parquet

    def read_without_attrs(*args, **kwargs):
        cached = read_parquet(*args, **kwargs)
        cached.attrs.clear()
        return cached

    monkeypatch.setattr(market.pd, "read_parquet", read_without_attrs)
    cached = market.get_prices(**kwargs)

    assert len(calls) == 1
    assert cached.attrs == {"source": "stub", "synthetic": False}
    provenance = market.cached_provenance(
        ["AAA"],
        "2024-01-01",
        "2024-01-05",
        cache_dir=tmp_path,
        provider="stub",
    )
    assert provenance is not None
    assert provenance[0] == "stub"


def test_cache_without_provenance_is_invalid(tmp_path, monkeypatch):
    tickers = ["AAA"]
    start, end = "2024-01-01", "2024-01-05"
    cache_path = tmp_path / (
        f"{market._cache_key(tickers, start, end, 'yfinance')}.parquet"
    )
    _panel(tickers).to_parquet(cache_path)

    assert market.cached_provenance(
        tickers,
        start,
        end,
        cache_dir=tmp_path,
        provider="yfinance",
    ) is None
    with pytest.raises(
        RuntimeError,
        match="offline cache.*cache payload has no provenance metadata",
    ):
        market.get_prices(
            tickers,
            start,
            end,
            offline=True,
            cache_dir=tmp_path,
            provider="yfinance",
        )

    calls = []

    def stub_yfinance(requested_tickers, requested_start, requested_end):
        calls.append((requested_tickers, requested_start, requested_end))
        return _panel(requested_tickers) + 1_000.0

    monkeypatch.setattr(market, "_fetch_yfinance", stub_yfinance)
    with pytest.warns(UserWarning, match="ignoring invalid yfinance market cache"):
        refreshed = market.get_prices(
            tickers,
            start,
            end,
            cache_dir=tmp_path,
            provider="yfinance",
        )

    assert calls == [(tickers, start, end)]
    assert refreshed.iloc[0, 0] == 1_100.0
    assert refreshed.attrs == {"source": "yfinance", "synthetic": False}


def test_cache_pair_write_serializes_real_synthetic_interleaving(
    tmp_path, monkeypatch,
):
    """Concurrent writers cannot pair a synthetic payload with live metadata."""
    import threading

    tickers = ["AAA"]
    start, end = "2024-01-01", "2024-01-05"
    cache_path = tmp_path / (
        f"{market._cache_key(tickers, start, end, 'yfinance')}.parquet"
    )
    live = _panel(tickers)
    live.attrs.update({"source": "yfinance", "synthetic": False})
    synthetic = _panel(tickers) + 1_000.0
    synthetic.attrs.update({"source": "synthetic", "synthetic": True})

    live_payload_written = threading.Event()
    release_live_writer = threading.Event()
    synthetic_finished = threading.Event()
    original_to_parquet = market.pd.DataFrame.to_parquet

    def gated_to_parquet(frame, *args, **kwargs):
        result = original_to_parquet(frame, *args, **kwargs)
        if frame.attrs.get("source") == "yfinance":
            live_payload_written.set()
            if not release_live_writer.wait(2.0):
                raise TimeoutError("test did not release live cache writer")
        return result

    monkeypatch.setattr(market.pd.DataFrame, "to_parquet", gated_to_parquet)
    errors = []

    def write(frame, finished=None):
        try:
            market._write_cache(cache_path, frame, "yfinance")
        except Exception as exc:  # surfaced on the test thread below
            errors.append(exc)
        finally:
            if finished is not None:
                finished.set()

    live_thread = threading.Thread(target=write, args=(live,))
    synthetic_thread = threading.Thread(
        target=write, args=(synthetic, synthetic_finished))
    live_thread.start()
    assert live_payload_written.wait(2.0)
    synthetic_thread.start()
    try:
        assert not synthetic_finished.wait(0.1)
    finally:
        release_live_writer.set()
        live_thread.join(2.0)
        synthetic_thread.join(2.0)

    assert not live_thread.is_alive()
    assert not synthetic_thread.is_alive()
    assert errors == []
    cached = market._read_cache(cache_path, "yfinance")
    assert cached is not None
    assert cached.equals(synthetic)
    assert cached.attrs == {"source": "synthetic", "synthetic": True}
    assert list(tmp_path.glob("*.tmp")) == []


def test_cache_payload_must_match_provenance_sidecar(tmp_path):
    """A crash/interleaving mismatch is invalid, never trusted as live data."""
    tickers = ["AAA"]
    start, end = "2024-01-01", "2024-01-05"
    cache_path = tmp_path / (
        f"{market._cache_key(tickers, start, end, 'yfinance')}.parquet"
    )
    live = _panel(tickers)
    live.attrs.update({"source": "yfinance", "synthetic": False})
    synthetic = _panel(tickers) + 1_000.0
    synthetic.attrs.update({"source": "synthetic", "synthetic": True})

    market._write_cache(cache_path, live, "yfinance")
    live_metadata = market._cache_metadata_path(cache_path).read_text(
        encoding="utf-8")
    market._write_cache(cache_path, synthetic, "yfinance")
    market._cache_metadata_path(cache_path).write_text(
        live_metadata, encoding="utf-8")

    with pytest.raises(
        RuntimeError,
        match="offline cache.*payload identity does not match provenance metadata",
    ):
        market.get_prices(
            tickers,
            start,
            end,
            offline=True,
            cache_dir=tmp_path,
            provider="yfinance",
        )


def test_offline_migrates_verified_legacy_yfinance_cache(tmp_path):
    """The pre-provider cache key remains usable with explicit provenance."""
    tickers = ["AAA"]
    start, end = "2024-01-01", "2024-01-05"
    legacy_path = tmp_path / (
        f"{market._legacy_cache_key(tickers, start, end)}.parquet"
    )
    legacy = _panel(tickers)
    legacy.attrs.update({"source": "yfinance", "synthetic": False})
    legacy.to_parquet(legacy_path)

    cached = market.get_prices(
        tickers,
        start,
        end,
        offline=True,
        cache_dir=tmp_path,
        provider="yfinance",
    )

    current_path = tmp_path / (
        f"{market._cache_key(tickers, start, end, 'yfinance')}.parquet"
    )
    assert cached.equals(legacy)
    assert cached.attrs == {"source": "yfinance", "synthetic": False}
    assert current_path.exists() or current_path.with_suffix(".pkl").exists()
    assert market._cache_metadata_path(current_path).exists()
    assert market._read_cache(current_path, "yfinance").equals(legacy)


def test_offline_refuses_unverifiable_legacy_cache(tmp_path):
    """An attrless legacy payload cannot silently become synthetic data."""
    tickers = ["AAA"]
    start, end = "2024-01-01", "2024-01-05"
    legacy_path = tmp_path / (
        f"{market._legacy_cache_key(tickers, start, end)}.parquet"
    )
    _panel(tickers).to_parquet(legacy_path)

    with pytest.raises(
        RuntimeError,
        match="offline cache.*legacy cache provenance is missing",
    ):
        market.get_prices(
            tickers,
            start,
            end,
            offline=True,
            cache_dir=tmp_path,
            provider="yfinance",
        )


def test_snapshot_forwards_explicit_provider(tmp_path, monkeypatch):
    monkeypatch.setitem(
        market.PROVIDERS,
        "stub",
        lambda tickers, start, end: _panel(tickers),
    )
    monkeypatch.setattr(market, "_CACHE_DIR", tmp_path)

    snap = market.snapshot(
        ["AAA"],
        "2024-01-05",
        start="2024-01-01",
        provider="stub",
    )

    assert snap.source == "stub"
    assert snap.prices.attrs["source"] == "stub"


@pytest.mark.parametrize(
    ("credentials", "expected"),
    [
        # No login anywhere: the one sentence that names both fixes.
        ({}, "needs credentials"),
        # A half-set env pair is a broken setup, refused by the auth module
        # by name — never fallen through to a profile nobody asked for.
        ({"ALPACA_API_KEY": "key"}, "ALPACA_API_SECRET is not set"),
        ({"ALPACA_API_SECRET": "secret"}, "ALPACA_API_KEY is not set"),
    ],
)
def test_alpaca_missing_credentials_fail_loud(
    tmp_path, monkeypatch, credentials, expected,
):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    # The data lane now honors `alpaca profile login`; point the profile
    # store at an empty dir so a developer's real session cannot leak in.
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(tmp_path / "no-profiles"))
    for name, value in credentials.items():
        monkeypatch.setenv(name, value)
    _warm_alpaca_cache(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match=expected):
        market.get_prices(
            ["AAA"],
            "2024-01-01",
            "2024-01-05",
            provider="alpaca",
            cache_dir=tmp_path,
        )


def test_alpaca_missing_package_fails_loud(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_API_SECRET", "secret")
    _warm_alpaca_cache(tmp_path, monkeypatch)
    # The whole family, not just the root: an earlier test's validation may
    # have imported the real submodules, and a cached `alpaca.data.enums`
    # satisfies a from-import without ever consulting the stubbed root.
    for name in ("alpaca", "alpaca.data", "alpaca.data.enums",
                 "alpaca.data.historical", "alpaca.data.requests",
                 "alpaca.data.timeframe"):
        monkeypatch.setitem(sys.modules, name, None)

    with pytest.raises(RuntimeError, match="'alpaca-py' package"):
        market.get_prices(
            ["AAA"],
            "2024-01-01",
            "2024-01-05",
            provider="alpaca",
            cache_dir=tmp_path,
        )


def test_alpaca_requests_adjusted_daily_closes(monkeypatch):
    requests = []
    raw = pd.DataFrame(
        {
            "open": [10.0, 20.0, 11.0, 21.0],
            "close": [10.5, 20.5, 11.5, 21.5],
        },
        index=pd.MultiIndex.from_tuples(
            [
                ("AAA", pd.Timestamp("2024-09-27 04:00", tz="UTC")),
                ("BBB", pd.Timestamp("2024-09-27 04:00", tz="UTC")),
                ("AAA", pd.Timestamp("2024-09-30 04:00", tz="UTC")),
                ("BBB", pd.Timestamp("2024-09-30 04:00", tz="UTC")),
            ],
            names=["symbol", "timestamp"],
        ),
    )

    class StockBarsRequest:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            requests.append(self)

    class StockHistoricalDataClient:
        def __init__(self, api_key=None, secret_key=None, oauth_token=None):
            assert (api_key, secret_key) == ("key", "secret")
            assert oauth_token is None

        def get_stock_bars(self, request):
            assert request is requests[-1]
            return SimpleNamespace(df=raw)

    modules = {
        "alpaca": ModuleType("alpaca"),
        "alpaca.data": ModuleType("alpaca.data"),
        "alpaca.data.enums": ModuleType("alpaca.data.enums"),
        "alpaca.data.historical": ModuleType("alpaca.data.historical"),
        "alpaca.data.requests": ModuleType("alpaca.data.requests"),
        "alpaca.data.timeframe": ModuleType("alpaca.data.timeframe"),
    }
    modules["alpaca"].__path__ = []
    modules["alpaca.data"].__path__ = []
    modules["alpaca.data.enums"].Adjustment = SimpleNamespace(ALL="all")
    modules["alpaca.data.enums"].DataFeed = SimpleNamespace(
        IEX="iex", SIP="sip", DELAYED_SIP="delayed_sip")
    modules["alpaca.data.historical"].StockHistoricalDataClient = (
        StockHistoricalDataClient
    )
    modules["alpaca.data.requests"].StockBarsRequest = StockBarsRequest
    modules["alpaca.data.timeframe"].TimeFrame = SimpleNamespace(Day="day")
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_API_SECRET", "secret")

    prices = market._fetch_alpaca(["AAA", "BBB"], "2024-09-01", "2024-10-01")
    snap = DataSnapshot(
        tickers=["AAA", "BBB"],
        prices=prices,
        as_of=date(2024, 9, 30),
        source="alpaca",
    )

    assert requests[-1].timeframe == "day"
    assert requests[-1].adjustment == "all"
    # Explicit feed, matching the policy's own: the SDK default is SIP, which
    # the free tier refuses for recent data while the desk claims iex.
    assert requests[-1].feed == "iex"
    assert list(prices.columns) == ["AAA", "BBB"]
    assert prices.index.tz is None
    assert prices.index.equals(
        pd.DatetimeIndex(["2024-09-27", "2024-09-30"], name="timestamp")
    )
    assert prices.loc["2024-09-30", "BBB"] == 21.5
    assert snap.prices.index[-1] == pd.Timestamp("2024-09-30")
    assert prices.attrs == {"source": "alpaca", "synthetic": False}


def test_offline_mode_serves_alpaca_cache_without_setup_validation(
    tmp_path, monkeypatch,
):
    warm = _warm_alpaca_cache(tmp_path, monkeypatch)

    def fail_validation(provider):
        raise AssertionError(f"offline mode validated {provider}")

    def fail_fetch(tickers, start, end):
        raise AssertionError("offline mode invoked the provider")

    monkeypatch.setattr(market, "_validate_provider_setup", fail_validation)
    monkeypatch.setitem(market.PROVIDERS, "alpaca", fail_fetch)
    cached = market.get_prices(
        ["AAA"],
        "2024-01-01",
        "2024-01-05",
        offline=True,
        provider="alpaca",
        cache_dir=tmp_path,
    )

    assert cached.equals(warm)
    assert cached.attrs == {"source": "alpaca", "synthetic": False}


def test_offline_mode_bypasses_provider_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("QLAB_DATA_PROVIDER", "alpaca")

    prices = market.get_prices(
        ["AAA"],
        "2024-01-01",
        "2024-01-05",
        offline=True,
        provider="not-registered",
        cache_dir=tmp_path,
    )

    assert prices.attrs == {"source": "synthetic", "synthetic": True}


# --- DataPolicy (Phase 1 slice 1) --------------------------------------------


def test_data_policy_constructors_set_expected_flags():
    op = market.DataPolicy.alpaca_operational("sip")
    assert (op.mode, op.provider, op.feed) == ("operational", "alpaca", "sip")
    assert op.allow_network and not op.allow_synthetic
    assert op.require_fresh and op.execution_eligible

    hist = market.DataPolicy.alpaca_historical()
    assert (hist.mode, hist.provider, hist.feed) == ("historical", "alpaca", "iex")
    assert hist.allow_network and not hist.allow_synthetic
    assert not hist.require_fresh and not hist.execution_eligible

    for policy in (market.DataPolicy.demo(), market.DataPolicy.test()):
        assert policy.provider == "synthetic"
        assert not policy.allow_network and policy.allow_synthetic
        assert not policy.execution_eligible


def test_data_policy_rejects_unknown_feed():
    with pytest.raises(ValueError, match="invalid Alpaca feed"):
        market.DataPolicy.alpaca_operational("nasdaq_totalview")


def test_data_policy_is_immutable():
    op = market.DataPolicy.alpaca_operational()
    with pytest.raises(Exception):
        op.allow_synthetic = True  # frozen dataclass


def test_effective_policy_translates_legacy_offline():
    # An explicit policy always wins.
    explicit = market.DataPolicy.alpaca_historical()
    assert market._effective_policy(True, "alpaca", explicit) is explicit

    # Legacy offline → demo-grade: no network, synthetic permitted, provider
    # preserved only as the cache namespace.
    off = market._effective_policy(True, "alpaca", None)
    assert not off.allow_network and off.allow_synthetic and off.provider == "alpaca"

    on = market._effective_policy(False, None, None)
    assert on.allow_network and on.allow_synthetic and on.provider == "yfinance"


def test_operational_policy_refuses_synthetic_when_fetch_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(market, "_validate_provider_setup", lambda provider: None)
    monkeypatch.setitem(market.PROVIDERS, "alpaca", lambda t, s, e: None)  # outage
    with pytest.raises(market.DataUnavailable, match="forbids a synthetic"):
        market.get_prices(
            ["AAA"], "2024-01-01", "2024-01-05",
            cache_dir=tmp_path, policy=market.DataPolicy.alpaca_operational(),
        )


def test_operational_policy_never_returns_a_yfinance_cache(tmp_path, monkeypatch):
    # Warm a yfinance cache for the same panel, then request under an Alpaca
    # operational policy whose fetch fails: the yfinance cache must NOT satisfy
    # the Alpaca request (different provider namespace), and no synthetic.
    monkeypatch.setitem(market.PROVIDERS, "yfinance", lambda t, s, e: _panel(t))
    market.get_prices(["AAA"], "2024-01-01", "2024-01-05",
                      provider="yfinance", cache_dir=tmp_path)
    monkeypatch.setattr(market, "_validate_provider_setup", lambda provider: None)
    monkeypatch.setitem(market.PROVIDERS, "alpaca", lambda t, s, e: None)
    with pytest.raises(market.DataUnavailable):
        market.get_prices(
            ["AAA"], "2024-01-01", "2024-01-05",
            cache_dir=tmp_path, policy=market.DataPolicy.alpaca_operational(),
        )


def test_operational_policy_serves_real_alpaca_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr(market, "_validate_provider_setup", lambda provider: None)
    monkeypatch.setitem(market.PROVIDERS, "alpaca", lambda t, s, e: _panel(t))
    prices = market.get_prices(
        ["AAA"], "2024-01-01", "2024-01-05",
        cache_dir=tmp_path, policy=market.DataPolicy.alpaca_operational(),
    )
    assert prices.attrs == {"source": "alpaca", "synthetic": False}


def test_snapshot_forwards_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(market, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(market, "_validate_provider_setup", lambda provider: None)
    monkeypatch.setitem(market.PROVIDERS, "alpaca", lambda t, s, e: None)
    with pytest.raises(market.DataUnavailable):
        market.snapshot(["AAA"], "2024-01-03",
                        policy=market.DataPolicy.alpaca_operational())


def test_synthetic_cache_identity_includes_the_seed(tmp_path):
    """A seed sweep must produce different samples, not one repeated sample.

    The cache key omitted the seed, so the first offline call populated the
    cache and every later call with a different seed silently got that first
    panel back. A seed sweep is the standard way to check whether a research
    result is robust or a fluke, and this made every sample identical — so any
    result looked perfectly stable, including a spurious one. It corrupted a
    real measurement before it was found.
    """
    import numpy as np

    from qlab.core.data import get_prices

    first = get_prices(["ACWI", "BNDW"], "2008-01-01", offline=True, seed=7,
                       cache_dir=tmp_path)
    other = get_prices(["ACWI", "BNDW"], "2008-01-01", offline=True, seed=999,
                       cache_dir=tmp_path)
    assert not np.allclose(first.to_numpy(), other.to_numpy())

    # And the same seed is still served from cache, unchanged.
    again = get_prices(["ACWI", "BNDW"], "2008-01-01", offline=True, seed=7,
                       cache_dir=tmp_path)
    assert np.allclose(first.to_numpy(), again.to_numpy())


def test_alpaca_data_client_uses_the_profile_login(tmp_path, monkeypatch):
    """The data lane honors `alpaca profile login`, not only the env pair.

    This is the seam that kept a browser-logged-in desk on research-grade
    yfinance forever: the trading half resolved the profile, the data half
    demanded env vars, and every permit refused paper proposals.
    """
    from qlab.core.data import _alpaca_client_kwargs

    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    config = tmp_path / "alpaca-config"
    (config / "profiles").mkdir(parents=True)
    (config / "profiles" / "paper.yaml").write_text(
        "api_key: pk\nsecret_key: sk\n", encoding="utf-8")
    (config / "config.yaml").write_text("profile: paper\n", encoding="utf-8")
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(config))

    assert _alpaca_client_kwargs() == {"api_key": "pk", "secret_key": "sk"}


def test_alpaca_data_client_carries_an_oauth_login(tmp_path, monkeypatch):
    from qlab.core.data import _alpaca_client_kwargs

    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    config = tmp_path / "alpaca-config"
    (config / "profiles").mkdir(parents=True)
    (config / "profiles" / "paper.yaml").write_text(
        "access_token: tok\n", encoding="utf-8")
    (config / "config.yaml").write_text("profile: paper\n", encoding="utf-8")
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(config))

    assert _alpaca_client_kwargs() == {"oauth_token": "tok"}
