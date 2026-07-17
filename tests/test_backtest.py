"""Walk-forward backtest engine + the experiment runner."""

from __future__ import annotations

import numpy as np
import pandas as pd

from qlab.arms import Arm, MomentsConfig, build_policy
from qlab.core import data as market
from qlab.core.backtest import rebalance_dates, run_backtest
from qlab.core.metrics import block_bootstrap_ci, deflated_sharpe
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


def _noise(seed, n=1000, mu=0.0):
    rng = np.random.default_rng(seed)
    return pd.Series(rng.normal(mu, 0.01, n))


def test_deflated_sharpe_is_calibrated_not_degenerate():
    rs = [_noise(s) for s in range(8)]
    srs = [float(r.mean() / r.std(ddof=1)) for r in rs]
    v = float(np.var(srs, ddof=1))
    dsrs = [deflated_sharpe(r, sr, n_trials=8, trial_sharpe_var=v)
            for r, sr in zip(rs, srs)]
    assert all(0.0 < d < 1.0 for d in dsrs)
    assert max(dsrs) > 0.01                      # the old bug pinned all of these to ~0
    skilled = _noise(99, mu=0.001)
    sr_sk = float(skilled.mean() / skilled.std(ddof=1))
    assert deflated_sharpe(skilled, sr_sk, n_trials=8, trial_sharpe_var=v) > max(dsrs)


def test_deflated_sharpe_monotone_in_trials():
    r = _noise(1, mu=0.0005)
    sr = float(r.mean() / r.std(ddof=1))
    assert deflated_sharpe(r, sr, n_trials=20, trial_sharpe_var=0.001) < \
           deflated_sharpe(r, sr, n_trials=2, trial_sharpe_var=0.001)


def test_ablation_reports_cis_and_registry_trials(tmp_path):
    from qlab.experiment import run_ablation
    from qlab.state.registry import Registry
    reg = Registry(":memory:")
    spec = {"name": "t", "seed": 7,
            "data": {"universe": "core", "start": "2016-01-01", "end": "2020-12-31"},
            "backtest": {"rebalance": "quarterly", "lookback_days": 504, "cost_bps": 5},
            "moments": {}, "arms": [
                {"id": "B1", "objective": "equal_weight", "solver": "none"},
                {"id": "A1", "objective": "min_variance", "solver": "classical"}]}
    out = run_ablation(spec, registry=reg, offline=True, run_qaoa=False)
    m = out["arms"]["A1"]["metrics"]
    assert "sharpe_ci" in m and m["sharpe_ci"][0] <= m["sharpe_ci"][1]
    assert "sortino_ci" in m
    assert reg.backtest_trial_count() == 2


def test_dsr_trial_count_excludes_benchmark_and_accumulates():
    from qlab.experiment import run_ablation
    from qlab.state.registry import Registry
    reg = Registry(":memory:")
    spec = {"name": "t2", "seed": 7,
            "data": {"universe": "core", "start": "2017-01-01", "end": "2020-12-31"},
            "backtest": {"rebalance": "quarterly", "lookback_days": 504, "cost_bps": 5},
            "moments": {}, "arms": [
                {"id": "B0", "objective": "sixty_forty", "solver": "none"},
                {"id": "B1", "objective": "equal_weight", "solver": "none"},
                {"id": "A1", "objective": "min_variance", "solver": "classical"}]}
    out = run_ablation(spec, registry=reg, offline=True, run_qaoa=False)
    assert out["n_trials_dsr"] == 2                      # B0 excluded; B1+A1 count
    spec2 = dict(spec, arms=[{"id": "B3", "objective": "risk_parity", "solver": "risk_parity"}])
    out2 = run_ablation(spec2, registry=reg, offline=True, run_qaoa=False)
    assert out2["n_trials_dsr"] == 3                     # accumulates: B1, A1, B3


def test_sortino_stat_nan_on_all_positive():
    import numpy as np, pandas as pd
    from qlab.experiment import _sortino_stat
    assert np.isnan(_sortino_stat(pd.Series([0.01, 0.02, 0.015, 0.03])))


def test_vol_target_overlay_reduces_realized_vol():
    from qlab.arms import Arm, MomentsConfig, build_policy
    from qlab.core.backtest import run_backtest
    from qlab.core import data as market
    px = market.get_prices(["ACWI", "BNDW", "GSG", "GLD", "VNQ"],
                           "2015-01-01", "2021-12-31", offline=True, seed=7)
    raw = Arm("A3", "mvsk", "classical_multistart",
              {"skew_lambda": 0.5, "kurt_lambda": 0.5})
    tgt = Arm("A3t", "mvsk", "classical_multistart",
              {"skew_lambda": 0.5, "kurt_lambda": 0.5, "target_vol": 0.06})
    cfg = MomentsConfig(lookback_days=504)
    m_raw = run_backtest(px, build_policy(raw, moments=cfg), cadence="quarterly",
                         lookback_days=504).metrics
    m_tgt = run_backtest(px, build_policy(tgt, moments=cfg), cadence="quarterly",
                         lookback_days=504).metrics
    assert m_tgt["ann_vol"] < m_raw["ann_vol"]
