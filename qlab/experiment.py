"""Experiment orchestration — batch ablation and single-shot recommendation.

The two operating modes (research-plan §2.3):

* :func:`run_ablation` — **batch mode**. Executes a declarative spec end-to-end
  and writes every arm to the registry, so the submission numbers reproduce from
  ``git clone && qlab batch configs/specs/ablation_v1.yaml``.
* :func:`recommend` — **stepped mode**. One point-in-time allocation with a real
  classical-vs-quantum comparison, for the live demo narrative and the autopilot.

Benchmark and classical arms get a full walk-forward backtest; quantum arms get a
single-shot solve plus diagnostics (optimality gaps, qubit counts, the 434-qubit
resource count) — their headline value is the *measured* comparison, not a
16-year backtest of a selection vector.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from qlab.arms import Arm, MomentsConfig, build_policy, estimate, solve_arm
from qlab.core import data as market
from qlab.core.backtest import BacktestResult, run_backtest
from qlab.core.metrics import block_bootstrap_ci, deflated_sharpe, periodic_sharpe
from qlab.core.objective import build_objective, compile_scipy
from qlab.core.types import DataSnapshot
from qlab.core.universe import load_universe
from qlab.solvers.base import Constraints, get_solver
from qlab.state.registry import Registry

_REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Batch mode
# ---------------------------------------------------------------------------
def run_ablation(
    spec: str | Path | dict,
    *,
    registry: Registry | None = None,
    offline: bool = False,
    run_qaoa: bool = True,
) -> dict[str, Any]:
    """Run the full experiment matrix from a spec and persist it to the registry."""
    spec = _load_spec(spec)
    reg = registry or Registry()
    run_id = reg.log_run("ablation", spec)

    uni = load_universe()
    tickers = uni.tickers(spec.get("data", {}).get("universe", "core"))
    d = spec.get("data", {})
    seed = int(spec.get("seed", 7))
    prices = market.get_prices(tickers, d.get("start", "2008-01-01"),
                               d.get("end"), offline=offline, seed=seed)

    bt = spec.get("backtest", {})
    m = spec.get("moments", {})
    moments_cfg = MomentsConfig(
        lookback_days=int(bt.get("lookback_days", 756)),
        shrinkage=m.get("shrinkage", "ledoit_wolf"),
        denoise=m.get("denoise", "marchenko_pastur"),
        comoment_shrinkage=m.get("comoment_shrinkage", 0.5),
        comoment_target=m.get("comoment_target", "isserlis"),
    )
    arms = [Arm(**_arm_kwargs(a)) for a in spec.get("arms", [])]
    arm_by_id = {a.id: a for a in arms}
    n_trials = max(1, len([a for a in arms if a.objective not in ("sixty_forty",)]))

    results: dict[str, Any] = {"run_id": run_id, "arms": {}, "quantum": {}}

    bt_results: dict[str, BacktestResult] = {}
    for arm in arms:
        try:
            res = run_backtest(
                prices, build_policy(arm, moments=moments_cfg),
                arm_id=arm.id, cadence=bt.get("rebalance", "quarterly"),
                lookback_days=moments_cfg.lookback_days,
                cost_bps=float(bt.get("cost_bps", 5)), n_trials=n_trials,
            )
            bt_results[arm.id] = res
        except Exception as exc:  # keep the ablation resilient arm-by-arm
            results["arms"][arm.id] = {"error": repr(exc)}

    # cross-trial Sharpe variance + registry-counted trials -> honest DSR + CIs
    candidates = [aid for aid in bt_results if arm_by_id[aid].objective != "sixty_forty"]
    psrs = [periodic_sharpe(bt_results[aid].returns) for aid in candidates]
    v_sr = float(np.var(psrs, ddof=1)) if len(psrs) > 1 else 0.0
    # cumulative, registry-wide count of non-benchmark arms (this run's candidates
    # included), computed before this run's backtests are logged so it never
    # double-counts an arm already persisted from a prior run.
    n_trials_dsr = max(len(reg.backtest_arm_ids() | set(candidates)), 2)
    for arm_id, res in bt_results.items():
        res.metrics["deflated_sharpe"] = deflated_sharpe(
            res.returns, periodic_sharpe(res.returns),
            n_trials=n_trials_dsr, trial_sharpe_var=v_sr)
        for name, fn in (("sharpe_ci", periodic_sharpe), ("sortino_ci", _sortino_stat)):
            res.metrics[name] = list(block_bootstrap_ci(res.returns, fn))
        reg.log_backtest(run_id, arm_id, res.metrics,
                         objective=arm_by_id[arm_id].objective)
        results["arms"][arm_id] = {
            "objective": arm_by_id[arm_id].objective,
            "solver": arm_by_id[arm_id].solver,
            "metrics": res.metrics, "total_turnover": res.total_turnover}
    results["n_trials_registry"] = reg.backtest_trial_count()
    results["n_trials_dsr"] = n_trials_dsr

    # quantum arms — single-shot at the latest snapshot
    for qa in spec.get("quantum_arms", []):
        arm = Arm(**_arm_kwargs(qa))
        q_tickers = uni.tickers(arm.universe)
        q_prices = market.get_prices(q_tickers, d.get("start", "2008-01-01"),
                                     d.get("end"), offline=offline, seed=seed)
        snap = DataSnapshot(q_tickers, q_prices, q_prices.index[-1].date())
        weights, diag = solve_arm(arm, snap, moments=moments_cfg)
        obj_form = diag.get("objective", arm.objective)
        reg.log_solution(run_id, arm.id,
                         _shim_result(weights, diag), objective_form=obj_form)
        results["quantum"][arm.id] = diag

    reg.record_event("ablation_complete", {"run_id": run_id,
                                            "n_arms": len(arms)})
    results["ranking"] = _rank(results["arms"])
    return results


# ---------------------------------------------------------------------------
# Stepped mode — one recommendation with a classical vs quantum compare
# ---------------------------------------------------------------------------
def recommend(
    *,
    as_of: str | None = None,
    universe: str = "core",
    skew_lambda: float = 0.5,
    kurt_lambda: float = 0.5,
    moments_cfg: MomentsConfig | None = None,
    constraints: Constraints | None = None,
    offline: bool = False,
    run_qaoa: bool = True,
    seed: int = 7,
) -> dict[str, Any]:
    """Produce one allocation recommendation (the demo/autopilot decision)."""
    moments_cfg = moments_cfg or MomentsConfig()
    constraints = constraints or Constraints()
    uni = load_universe()
    tickers = uni.tickers(universe)
    as_of = as_of or _today()
    snap = market.snapshot(tickers, as_of, offline=offline, seed=seed)

    champion = Arm(id="A3", objective="mvsk", solver="classical_multistart",
                   params={"skew_lambda": skew_lambda, "kurt_lambda": kurt_lambda})
    weights, diag = solve_arm(champion, snap, moments=moments_cfg,
                              constraints=constraints)

    compare = compare_classical_quantum(snap, moments_cfg, constraints,
                                        run_qaoa=run_qaoa)
    return {
        "as_of": str(as_of), "universe": universe, "tickers": tickers,
        "recommended_weights": dict(zip(weights.tickers, weights.values)),
        "champion_arm": champion.id, "diagnostics": diag,
        "classical_vs_quantum": compare,
    }


def compare_classical_quantum(
    snapshot: DataSnapshot,
    moments_cfg: MomentsConfig,
    constraints: Constraints,
    *,
    run_qaoa: bool = True,
) -> dict[str, Any]:
    """Run classical min-variance and the QAOA discretized-MV arm on the SAME cov.

    Returns objective value, weights and wall-clock for both so the reporter can
    present a real comparison (spec: ``optimize.compare``).
    """
    ms = estimate(snapshot, moments_cfg, higher=False)
    obj = build_objective("min_variance", ms)
    classical = get_solver("classical").solve(obj, constraints)

    out: dict[str, Any] = {
        "classical": {
            "solver": classical.solver,
            "objective_value": classical.objective_value,
            "wall_clock_s": classical.wall_clock_s,
            "weights": dict(zip(classical.weights.tickers, classical.weights.values)),
        }
    }
    if run_qaoa:
        try:
            qobj = build_objective("discretized_mv", ms, extra={"resolution_bits": 3})
            qres = get_solver("qaoa", reps=2).solve(qobj, constraints)
            f, _ = compile_scipy(obj)
            out["quantum"] = {
                "solver": qres.solver,
                "objective_value": float(f(qres.weights.as_array())),
                "wall_clock_s": qres.wall_clock_s,
                "weights": dict(zip(qres.weights.tickers, qres.weights.values)),
                "diagnostics": qres.diagnostics,
            }
        except Exception as exc:
            out["quantum"] = {"unavailable": repr(exc),
                              "note": "install qlab[quantum] for the Aer QAOA arm"}
    return out


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _sortino_stat(r) -> float:
    downside = r[r < 0]
    if len(downside) <= 1:
        return float("nan")
    dv = downside.std(ddof=1)
    if not dv > 0:
        return float("nan")
    return float(r.mean() / dv)


def _rank(arms: dict) -> list[dict]:
    scored = []
    for aid, a in arms.items():
        met = a.get("metrics", {})
        scored.append({"arm": aid, "sortino": met.get("sortino", float("-inf")),
                       "ann_vol": met.get("ann_vol"),
                       "deflated_sharpe": met.get("deflated_sharpe"),
                       "max_drawdown": met.get("max_drawdown")})
    return sorted(scored, key=lambda x: (x["sortino"] is not None, x["sortino"]),
                  reverse=True)


def _arm_kwargs(a: dict) -> dict:
    return {"id": a["id"], "objective": a["objective"], "solver": a["solver"],
            "params": a.get("params", {}), "universe": a.get("universe", "core")}


def _shim_result(weights, diag):
    from qlab.core.types import SolveResult

    return SolveResult(weights=weights,
                       objective_value=float(diag.get("objective_value", 0.0)),
                       solver=diag.get("solver", "unknown"),
                       wall_clock_s=float(diag.get("wall_clock_s", 0.0)),
                       diagnostics={k: v for k, v in diag.items()
                                    if k not in ("moments",)})


def _load_spec(spec) -> dict:
    if isinstance(spec, dict):
        return spec
    with open(spec, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _today() -> str:
    from datetime import date

    return date.today().isoformat()
