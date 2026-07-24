"""Core data + snapshot invariants, including the look-ahead tripwire."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from qlab.core import data as market
from qlab.core.types import DataSnapshot, Weights

CORE = ["ACWI", "BNDW", "GSG", "IGF", "GLD", "VNQ", "EMB"]


def test_synthetic_is_deterministic_across_calls():
    a = market.synthetic_prices(CORE, "2015-01-01", "2018-01-01", seed=7)
    b = market.synthetic_prices(CORE, "2015-01-01", "2018-01-01", seed=7)
    assert np.allclose(a.to_numpy(), b.to_numpy())


def test_online_mode_refreshes_a_synthetic_cache(tmp_path, monkeypatch):
    tickers = ["A", "B"]
    start, end = "2020-01-01", "2020-01-10"
    cached = market.get_prices(
        tickers,
        start,
        end,
        offline=True,
        cache_dir=tmp_path,
    )
    assert cached.attrs["source"] == "synthetic"
    cached_roundtrip = market.get_prices(
        tickers,
        start,
        end,
        offline=True,
        cache_dir=tmp_path,
    )
    assert cached_roundtrip.attrs["source"] == "synthetic"
    assert cached_roundtrip.attrs["synthetic"] is True

    calls = []
    live = pd.DataFrame(
        {"A": [10.0, 11.0], "B": [20.0, 21.0]},
        index=pd.to_datetime(["2020-01-02", "2020-01-03"]),
    )

    def fake_fetch(requested_tickers, requested_start, requested_end):
        calls.append((requested_tickers, requested_start, requested_end))
        return live.copy()

    monkeypatch.setattr(market, "_fetch_yfinance", fake_fetch)
    refreshed = market.get_prices(
        tickers,
        start,
        end,
        offline=False,
        cache_dir=tmp_path,
    )

    assert calls
    assert refreshed.attrs["source"] == "yfinance"
    assert refreshed.attrs["synthetic"] is False
    assert refreshed.equals(live)


def test_snapshot_cannot_look_ahead():
    prices = market.synthetic_prices(CORE, "2015-01-01", "2020-01-01", seed=7)
    snap = DataSnapshot(CORE, prices, date(2017, 6, 30))
    assert snap.prices.index.max() <= pd.Timestamp("2017-06-30")
    # window only looks backward
    win = snap.window(100)
    assert len(win) <= 100
    assert win.index.max() <= pd.Timestamp("2017-06-30")


def test_snapshot_log_returns_shape(snap):
    r = snap.log_returns(lookback_days=252)
    assert r.shape[1] == len(CORE)
    assert not r.isna().all().any()


def test_weights_validation_rejects_nonfinite():
    with pytest.raises(Exception):
        Weights(tickers=["A", "B"], values=[float("nan"), 0.5])


def test_weights_equal_sums_to_one():
    w = Weights.equal(CORE)
    assert abs(w.total - 1.0) < 1e-9


def test_market_news_offline_tilts_and_is_deterministic():
    """Offline news is deterministic, honestly synthetic, and follows the bias.

    A risk-off tilt yields risk-off synthetic headlines; a risk-on tilt yields
    risk-on ones — coherent with the synthetic market — and the same inputs
    always return the same payload (cached), so the offline suite is stable.
    """
    from qlab.core.news import fetch_market_news

    off = fetch_market_news(as_of="2020-03-31", offline=True,
                            synthetic_bias=-0.6, limit=3)
    assert off["source"] == "synthetic"
    assert off["tilt_label"] == "risk_off"
    assert 1 <= len(off["headlines"]) <= 3
    assert all(h["title"].startswith("[synthetic]") for h in off["headlines"])
    assert -1.0 <= off["risk_tilt"] <= 1.0

    on = fetch_market_news(as_of="2021-01-31", offline=True,
                           synthetic_bias=0.6, limit=3)
    assert on["tilt_label"] == "risk_on"
    assert on["headlines"] != off["headlines"]

    # deterministic + cached: identical inputs → identical payload
    again = fetch_market_news(as_of="2020-03-31", offline=True,
                             synthetic_bias=-0.6, limit=3)
    assert again == off


def test_market_news_never_raises_and_always_carries_a_disclaimer():
    from qlab.core.news import fetch_market_news

    for bias in (-1.0, 0.0, 1.0):
        news = fetch_market_news(as_of="2019-06-28", offline=True,
                                 synthetic_bias=bias, limit=6)
        assert news["disclaimer"]
        assert news["tilt_label"] in ("risk_off", "neutral", "risk_on")
        # headlines are labelled untrusted third-party context
        assert "untrusted" in news["disclaimer"].lower()
