"""Walk-forward backtest engine + the experiment runner."""

from __future__ import annotations

import numpy as np

from qlab.arms import Arm, MomentsConfig, build_policy
from qlab.core import data as market
from qlab.core.backtest import rebalance_dates, run_backtest
from qlab.experiment import run_ablation
from qlab.state.registry import Registry

CORE = ["ACWI", "BNDW", "GSG", "IGF", "GLD", "VNQ", "EMB"]


def test_rebalance_dates_are_within_index():
    prices = market.get_prices(CORE, "2016-01-01", "2020-12-31", offline=True, seed=7)
    rd = rebalance_dates(prices.index, "quarterly")
    assert len(rd) > 10
    assert all(d in prices.index for d in rd)


def test_backtest_produces_metrics():
    prices = market.get_prices(CORE, "2016-01-01", "2021-12-31", offline=True, seed=7)
    pol = build_policy(Arm("A1", "min_variance", "classical"),
                       moments=MomentsConfig(lookback_days=504))
    res = run_backtest(prices, pol, arm_id="A1", cadence="quarterly",
                       lookback_days=504, cost_bps=5, n_trials=4)
    for key in ("ann_return", "ann_vol", "sortino", "max_drawdown",
                "realized_skew", "realized_kurtosis", "deflated_sharpe"):
        assert key in res.metrics
    assert res.total_turnover > 0


def test_ablation_runs_and_ranks(reg):
    spec = {
        "name": "t", "seed": 7,
        "data": {"universe": "core", "start": "2017-01-01", "end": "2021-12-31"},
        "backtest": {"rebalance": "quarterly", "lookback_days": 504, "cost_bps": 5},
        "moments": {"shrinkage": "ledoit_wolf", "denoise": "marchenko_pastur",
                    "comoment_shrinkage": 0.5},
        "arms": [
            {"id": "B1", "objective": "equal_weight", "solver": "none"},
            {"id": "A1", "objective": "min_variance", "solver": "classical"},
        ],
        "quantum_arms": [
            {"id": "QC", "objective": "mvsk", "solver": "qubo_resource_count",
             "params": {"resolution_bits": 4}},
        ],
    }
    report = run_ablation(spec, registry=reg, offline=True, run_qaoa=False)
    assert set(report["arms"]) == {"B1", "A1"}
    assert report["quantum"]["QC"]["total_logical_qubits"] == 434
    assert len(report["ranking"]) == 2
