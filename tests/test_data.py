"""Provider selection and provenance tests for market data."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pandas as pd
import pytest

from qlab.core import data as market


def _panel(tickers: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            ticker: [100.0 + index, 101.0 + index]
            for index, ticker in enumerate(tickers)
        },
        index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
    )


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
                ("AAA", pd.Timestamp("2024-01-02", tz="UTC")),
                ("BBB", pd.Timestamp("2024-01-02", tz="UTC")),
                ("AAA", pd.Timestamp("2024-01-03", tz="UTC")),
                ("BBB", pd.Timestamp("2024-01-03", tz="UTC")),
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

    prices = market._fetch_alpaca(["AAA", "BBB"], "2024-01-01", "2024-01-05")

    assert requests[-1].timeframe == "day"
    assert requests[-1].adjustment == "all"
    assert list(prices.columns) == ["AAA", "BBB"]
    assert prices.index.tz is None
    assert prices.loc["2024-01-03", "BBB"] == 21.5
    assert prices.attrs == {"source": "alpaca", "synthetic": False}


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
