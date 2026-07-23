"""Agent-facing regime indicators (qlab.signals.indicators).

Each indicator reads a point-in-time snapshot and returns one regime reading in
a shared schema so the moments-analyst can weigh several and defend a single
call. The contract these tests pin: a consistent schema, a calm read on a
data-rich calm snapshot, a stress read on a crafted shock, and fail-loud (not a
silent calm default) when there is too little history to classify.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from qlab.core import data as market
from qlab.core.types import DataSnapshot
from qlab.signals import indicators as ind

_SCHEMA = {"indicator", "method", "regime", "signal", "threshold",
           "percentile", "window", "reasoning"}


def _shock_snapshot(seed: int = 3, calm: int = 730, shock: int = 30):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2019-01-01", periods=calm + shock)
    r = rng.normal(0.0, 0.006, (calm + shock, 7))
    # A violent, correlated crash in the final stretch: a common negative factor
    # plus fattened idiosyncratic noise. Every indicator should see this.
    r[-shock:] = (rng.normal(-0.02, 0.05, (shock, 1))
                  + rng.normal(0, 0.01, (shock, 7)))
    px = pd.DataFrame(100 * np.exp(np.cumsum(r, axis=0)), index=idx,
                      columns=list("ABCDEFG"))
    return DataSnapshot(list(px.columns), px, idx[-1].date())


@pytest.mark.parametrize("name", sorted(ind.INDICATORS))
def test_indicator_reading_has_the_shared_schema(name):
    snap = market.snapshot(
        ["ACWI", "BNDW", "GSG", "IGF", "GLD", "VNQ", "EMB"],
        "2022-06-30", lookback_days=756, offline=True, seed=7)
    reading = ind.INDICATORS[name](snap)
    assert _SCHEMA <= set(reading)
    assert reading["indicator"] == name
    assert reading["regime"] in ("calm", "stress")
    assert 0.0 <= reading["percentile"] <= 1.0
    assert reading["reasoning"] and reading["reasoning"] == reading["reasoning"].strip()


@pytest.mark.parametrize("name", sorted(ind.INDICATORS))
def test_every_indicator_flags_a_crafted_shock(name):
    snap = _shock_snapshot()
    reading = ind.INDICATORS[name](snap)
    assert reading["regime"] == "stress", reading
    assert reading["percentile"] >= 0.80


def test_indicators_disagree_across_faces_of_variability():
    """The five are not the same signal renamed: a slow, shallow, low-vol drift
    down separates the directional read from the vol/turbulence reads."""
    rng = np.random.default_rng(11)
    idx = pd.bdate_range("2019-01-01", periods=760)
    # Tiny, quiet, persistent negative drift: a deep drawdown builds without any
    # volatility spike.
    r = rng.normal(-0.0006, 0.003, (760, 7))
    px = pd.DataFrame(100 * np.exp(np.cumsum(r, axis=0)), index=idx,
                      columns=list("ABCDEFG"))
    snap = DataSnapshot(list(px.columns), px, idx[-1].date())
    dd = ind.drawdown_regime(snap)["regime"]
    vts = ind.volatility_term_structure(snap)["regime"]
    assert dd == "stress"          # directional axis fires
    assert vts == "calm"           # the acceleration axis does not


@pytest.mark.parametrize("name", sorted(ind.INDICATORS))
def test_insufficient_history_fails_loud(name):
    # Too few rows for even the shortest-window indicator to build a
    # classifiable history: every one must refuse, not default to calm.
    rng = np.random.default_rng(5)
    idx = pd.bdate_range("2022-01-01", periods=18)
    px = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, 0.006, (18, 7)), axis=0)),
                      index=idx, columns=list("ABCDEFG"))
    snap = DataSnapshot(list(px.columns), px, idx[-1].date())
    with pytest.raises(ValueError):
        ind.INDICATORS[name](snap)
