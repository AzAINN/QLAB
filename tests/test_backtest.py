"""Walk-forward backtest engine + the experiment runner."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from qlab.arms import Arm, MomentsConfig, build_policy, solve_arm
from qlab.core import data as market
from qlab.core.backtest import rebalance_dates, run_backtest
from qlab.core.metrics import block_bootstrap_ci, deflated_sharpe
from qlab.experiment import _arm_kwargs, _load_spec, run_ablation
from qlab.solvers.base import get_solver
from qlab.state.registry import Registry

_ABLATION_SPEC_PATH = Path(__file__).resolve().parents[1] / "configs" / "specs" / "ablation_v1.yaml"

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


def test_research_only_arm_excluded_from_dsr_trial_count():
    """A research_only arm must still get a full backtest and appear in the
    report, but must not inflate the DSR trial count — it's a diagnostic
    arm (e.g. the vol-target overlay) that cannot reach the live trader, so
    counting it as a trial would understate the deflated Sharpe of every
    real candidate.
    """
    from qlab.experiment import run_ablation
    from qlab.state.registry import Registry
    reg = Registry(":memory:")
    spec = {"name": "t3", "seed": 7,
            "data": {"universe": "core", "start": "2017-01-01", "end": "2020-12-31"},
            "backtest": {"rebalance": "quarterly", "lookback_days": 504, "cost_bps": 5},
            "moments": {}, "arms": [
                {"id": "B1", "objective": "equal_weight", "solver": "none"},
                {"id": "A1", "objective": "min_variance", "solver": "classical"},
                {"id": "RM", "objective": "min_variance", "solver": "classical",
                 "params": {"research_only": True}}]}
    out = run_ablation(spec, registry=reg, offline=True, run_qaoa=False)
    assert "RM" in out["arms"]                            # still reported
    assert out["n_trials_dsr"] == 2                        # but not counted as a trial
    spec2 = dict(spec, arms=[{"id": "B3", "objective": "risk_parity", "solver": "risk_parity"}])
    out2 = run_ablation(spec2, registry=reg, offline=True, run_qaoa=False)
    assert out2["n_trials_dsr"] == 3                       # accumulates B1, A1, B3; RM still excluded


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
    # magnitude check: with correct cash-carry through the drift loop, the
    # de-risked book's realised vol should track its cash share (~0.73 of
    # raw at target_vol=0.06 vs raw ~0.082), not merely be "somewhat lower".
    # A buggy drift loop that renormalizes to sum 1.0 daily silently re-levers
    # the cash-carrying weight vector back to full investment after one day,
    # which this stricter bound catches.
    assert m_tgt["ann_vol"] < 0.85 * m_raw["ann_vol"]


def test_drift_preserves_cash_share():
    """Half-invested and fully-invested twins should differ in realised vol
    by roughly the ratio of their invested shares (0.5), not converge to the
    same vol because the drift loop silently re-levers back to full exposure.
    """
    from qlab.core.types import Weights

    px = market.get_prices(["ACWI", "BNDW", "GSG", "GLD", "VNQ"],
                           "2015-01-01", "2021-12-31", offline=True, seed=7)

    def half_invested(snap):
        vals = [0.25, 0.25] + [0.0] * (len(snap.tickers) - 2)
        return Weights(tickers=snap.tickers, values=vals)

    def fully_invested(snap):
        vals = [0.5, 0.5] + [0.0] * (len(snap.tickers) - 2)
        return Weights(tickers=snap.tickers, values=vals)

    m_half = run_backtest(px, half_invested, arm_id="half", cadence="quarterly",
                          lookback_days=504).metrics
    m_full = run_backtest(px, fully_invested, arm_id="full", cadence="quarterly",
                          lookback_days=504).metrics
    ratio = m_half["ann_vol"] / m_full["ann_vol"]
    assert 0.4 <= ratio <= 0.6


def test_ablation_spec_solvers_resolve_and_b3_is_real_risk_parity():
    """Spec integrity for the real ``configs/specs/ablation_v1.yaml``.

    (a) every arm's ``solver`` must exist in the solver registry (``none`` is
        the documented benchmark sentinel that ``solve_arm`` never routes
        through ``get_solver`` for, so it's skipped rather than treated as a
        registry miss); solvers behind an optional dependency (qiskit, a QCI
        SDK) degrade to a skip instead of a hard failure.
    (b) B3 (the practitioner risk-parity benchmark) must actually declare the
        ``risk_parity`` solver -- not ``classical``, which would silently
        collapse it onto A1's min-variance solve (both share
        ``_objective_form("risk_parity") == "min_variance"``), making the
        two arms of the ablation matrix bit-identical.
    (c) B3's solved weights must genuinely differ from A1's on the same
        snapshot -- confirming the ERC solver is doing real work, not just
        that the YAML string changed.
    (d) B3's solved weights must also differ from equal-weight -- the ERC
        objective is dimensionful at daily-return covariance scale (its value
        at the equal-weight start is ~1e-11, below SLSQP's ftol resolution),
        so a naive implementation exits at x0 and silently returns 1/N. A1
        (min-variance) differing from B3 doesn't rule that out, since A1 need
        not equal 1/N either -- this pins down that B3 itself moved.
    """
    spec = _load_spec(str(_ABLATION_SPEC_PATH))

    for arm_dict in list(spec["arms"]) + list(spec.get("quantum_arms", [])):
        solver_name = arm_dict["solver"]
        if solver_name == "none":
            continue  # benchmark sentinel; solve_arm bypasses get_solver entirely
        try:
            get_solver(solver_name)
        except KeyError as exc:
            pytest.fail(
                f"arm {arm_dict['id']!r}: solver {solver_name!r} does not "
                f"resolve via qlab.solvers.base.get_solver: {exc}"
            )
        except ImportError:
            pytest.skip(
                f"arm {arm_dict['id']!r}: solver {solver_name!r} needs an "
                "optional dependency that isn't installed"
            )

    by_id = {a["id"]: a for a in spec["arms"]}
    assert by_id["B3"]["solver"] == "risk_parity"

    b3 = Arm(**_arm_kwargs(by_id["B3"]))
    a1 = Arm(**_arm_kwargs(by_id["A1"]))
    snap = market.snapshot(CORE, "2020-06-30", offline=True, seed=7)
    cfg = MomentsConfig(lookback_days=252)
    w_b3, _ = solve_arm(b3, snap, moments=cfg)
    w_a1, _ = solve_arm(a1, snap, moments=cfg)
    l1 = float((w_b3.as_series() - w_a1.as_series()).abs().sum())
    assert l1 > 0.01, f"B3 (risk parity) is bit-identical to A1 (min-variance): L1={l1}"

    w_eq = pd.Series(1.0 / len(w_b3.tickers), index=w_b3.tickers, dtype=float)
    l1_eq = float((w_b3.as_series() - w_eq).abs().sum())
    assert l1_eq > 0.05, f"B3 (risk parity) collapsed to equal-weight: L1={l1_eq}"
