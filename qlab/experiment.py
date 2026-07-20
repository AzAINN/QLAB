"""Staged experiment orchestration — batch ablation and recommendations.

The two operating modes (research-plan §2.3):

* :func:`run_ablation` — **batch mode**. Executes a declarative spec end-to-end
  and writes every arm to the registry, so the submission numbers reproduce from
  ``git clone && qlab batch configs/specs/ablation_v1.yaml``.
* :func:`recommend` — **stepped mode**. One point-in-time allocation for the
  governed operator desk and autopilot.

Only operational and declared research arms belong here. Offline quantum
experiments live under :mod:`qlab.algorithms.offline` and are rejected by this
staged runner.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import yaml

from qlab.algorithms import get_algorithm, get_operational_policy
from qlab.arms import Arm, MomentsConfig, build_policy, solve_arm
from qlab.core import data as market
from qlab.core.backtest import BacktestResult, run_backtest
from qlab.core.metrics import block_bootstrap_ci, deflated_sharpe, periodic_sharpe
from qlab.core.universe import load_universe
from qlab.paths import data_root
from qlab.solvers.base import Constraints
from qlab.state.registry import Registry


# ---------------------------------------------------------------------------
# Batch mode
# ---------------------------------------------------------------------------
def run_ablation(
    spec: str | Path | dict,
    *,
    registry: Registry | None = None,
    offline: bool = False,
) -> dict[str, Any]:
    """Run the full experiment matrix from a spec and persist it to the registry."""
    spec = _load_spec(spec)
    if spec.get("quantum_arms"):
        raise ValueError(
            "quantum_arms are offline research and cannot run in the staged "
            "ablation; use qlab.algorithms.offline explicitly"
        )
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

    results: dict[str, Any] = {"run_id": run_id, "arms": {}}

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

    # cross-trial Sharpe variance + registry-counted trials -> honest DSR + CIs.
    # research_only arms (e.g. the vol-target overlay, A3t) get a full backtest
    # and are reported, but must not inflate the trial count: they cannot
    # reach the live trader, so counting them as trials would understate the
    # deflated Sharpe of every real candidate.
    candidates = [aid for aid in bt_results
                  if arm_by_id[aid].objective != "sixty_forty"
                  and not arm_by_id[aid].params.get("research_only")]
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
        arm = arm_by_id[arm_id]
        # tag research_only arms' persisted objective so a future run's trial
        # count (Registry.backtest_arm_ids) can exclude them too.
        logged_objective = (f"{arm.objective}:research"
                           if arm.params.get("research_only") else arm.objective)
        reg.log_backtest(run_id, arm_id, res.metrics, objective=logged_objective)
        results["arms"][arm_id] = {
            "objective": arm_by_id[arm_id].objective,
            "solver": arm_by_id[arm_id].solver,
            "metrics": res.metrics, "total_turnover": res.total_turnover}
    results["n_trials_registry"] = reg.backtest_trial_count()
    results["n_trials_dsr"] = n_trials_dsr

    reg.record_event("ablation_complete", {"run_id": run_id,
                                            "n_arms": len(arms)})
    results["ranking"] = _rank(results["arms"])
    return results


# ---------------------------------------------------------------------------
# Stepped mode — one staged recommendation
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
    seed: int = 7,
    policy_id: str = "hrp",
) -> dict[str, Any]:
    """Produce one allocation recommendation from an explicit deployed policy."""
    moments_cfg = moments_cfg or MomentsConfig()
    constraints = constraints or Constraints()
    uni = load_universe()
    tickers = uni.tickers(universe)
    as_of = as_of or _today()
    snap = market.snapshot(tickers, as_of, offline=offline, seed=seed)

    policy = get_operational_policy(policy_id)
    champion = policy.arm()
    weights, diag = solve_arm(champion, snap, moments=moments_cfg,
                              constraints=constraints)

    return {
        "as_of": str(as_of), "universe": universe, "tickers": tickers,
        "recommended_weights": dict(zip(weights.tickers, weights.values)),
        "champion_arm": champion.id, "diagnostics": diag,
        "algorithm": get_algorithm(policy.algorithm_id).to_dict(),
        "operational_policy": policy.to_dict(),
        "research_note": (
            "MVSK remains in the ablation as a research hypothesis and is not "
            "the configured paper allocation policy."
        ),
    }


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
    path = Path(spec)
    if not path.exists() and not path.is_absolute():
        packaged = data_root() / path
        if packaged.exists():
            path = packaged
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _today() -> str:
    from datetime import date

    return date.today().isoformat()
