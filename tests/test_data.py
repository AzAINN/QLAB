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
        ({}, "alpaca provider requires ALPACA_API_KEY and ALPACA_API_SECRET"),
        (
            {"ALPACA_API_KEY": "key"},
            "alpaca provider requires ALPACA_API_SECRET",
        ),
        (
            {"ALPACA_API_SECRET": "secret"},
            "alpaca provider requires ALPACA_API_KEY",
        ),
    ],
)
def test_alpaca_missing_credentials_fail_loud(
    tmp_path, monkeypatch, credentials, expected,
):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    for name, value in credentials.items():
        monkeypatch.setenv(name, value)
    _warm_alpaca_cache(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError) as exc_info:
        market.get_prices(
            ["AAA"],
            "2024-01-01",
            "2024-01-05",
            provider="alpaca",
            cache_dir=tmp_path,
        )

    assert str(exc_info.value) == expected


def test_alpaca_missing_package_fails_loud(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_API_SECRET", "secret")
    _warm_alpaca_cache(tmp_path, monkeypatch)
    monkeypatch.setitem(sys.modules, "alpaca", None)

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
        def __init__(self, key, secret):
            assert (key, secret) == ("key", "secret")

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
