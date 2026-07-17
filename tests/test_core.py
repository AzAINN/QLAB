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
