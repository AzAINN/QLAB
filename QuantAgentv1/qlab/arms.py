"""The experiment matrix, wired.

``qlab.arms`` is the one place that composes the pure layers — data → moments →
objective → solver — into a ``policy(snapshot) -> Weights`` the backtester and
the autopilot both consume. Keeping this glue in a single module means the core
math, the solvers, and the servers stay decoupled and independently testable
(research-plan §2.1 component boundaries).

Each arm holds *everything else constant and varies one thing* (the objective or
the solver), which is what makes the ablation an honest comparison (§6).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from qlab.core.moments import estimate_moments
from qlab.core.objective import build_objective
from qlab.core.types import DataSnapshot, MomentSet, SolveResult, Weights
from qlab.core.universe import load_universe
from qlab.solvers.base import Constraints, get_solver

BENCHMARKS = {"sixty_forty", "equal_weight"}
# Which objective forms actually need the higher-moment tensors:
_NEEDS_HIGHER_MOMENTS = {"mvsk"}


@dataclass
class MomentsConfig:
    """The agent-tunable estimation judgment slots."""

    lookback_days: int = 756
    shrinkage: str = "ledoit_wolf"
    denoise: str | None = "marchenko_pastur"
    comoment_shrinkage: float = 0.5


@dataclass
class Arm:
    """One row of the experiment matrix."""

    id: str
    objective: str
    solver: str
    params: dict = field(default_factory=dict)
    universe: str = "core"


# ---------------------------------------------------------------------------
# estimation + single-shot solve
# ---------------------------------------------------------------------------
def estimate(snapshot: DataSnapshot, cfg: MomentsConfig, *, higher: bool) -> MomentSet:
    return estimate_moments(
        snapshot,
        lookback_days=cfg.lookback_days,
        shrinkage=cfg.shrinkage,
        denoise=cfg.denoise,
        comoment_shrinkage=cfg.comoment_shrinkage,
        include_mu=False,
        higher_moments=higher,
    )


def solve_arm(
    arm: Arm,
    snapshot: DataSnapshot,
    *,
    moments: MomentsConfig | None = None,
    constraints: Constraints | None = None,
) -> tuple[Weights, dict]:
    """Solve one arm at one point in time. Returns (weights, diagnostics)."""
    moments = moments or MomentsConfig()
    constraints = constraints or Constraints()

    # -- benchmarks: no estimation, no solve --------------------------------
    if arm.objective in BENCHMARKS:
        w = _benchmark_weights(arm.objective, snapshot)
        return w, {"arm": arm.id, "objective": arm.objective, "solver": "none"}

    higher = arm.objective in _NEEDS_HIGHER_MOMENTS
    ms = estimate(snapshot, moments, higher=higher)

    obj = build_objective(
        _objective_form(arm.objective),
        ms,
        skew_lambda=float(arm.params.get("skew_lambda", 0.0)),
        kurt_lambda=float(arm.params.get("kurt_lambda", 0.0)),
        extra={k: v for k, v in arm.params.items() if k in ("k", "resolution_bits")},
    )

    context: dict = {}
    if arm.solver == "cvar_lp":
        context["returns"] = snapshot.log_returns(moments.lookback_days).to_numpy()
    if "k" in arm.params:
        context["k"] = int(arm.params["k"])

    result, note = _dispatch_solver(arm, obj, constraints, context)
    diag = {"arm": arm.id, "objective": arm.objective, "solver": result.solver,
            "objective_value": result.objective_value,
            "wall_clock_s": result.wall_clock_s, **result.diagnostics}
    if note:
        diag["fallback"] = note
    diag["moments"] = ms.summary()
    return result.weights, diag


def _dispatch_solver(arm, obj, constraints, context) -> tuple[SolveResult, str | None]:
    """Run the arm's solver, with the A4 Dirac-3 → classical fallback."""
    if arm.solver == "dirac3":
        from qlab.solvers.dirac3 import Dirac3Unavailable

        try:
            solver = get_solver("dirac3")
            return solver.solve(obj, constraints, **context), None
        except Dirac3Unavailable as exc:
            solver = get_solver("classical_multistart")
            return solver.solve(obj, constraints, **context), f"dirac3_unavailable: {exc}"

    if arm.solver == "qaoa":
        reps = int(arm.params.get("reps", 2))
        try:
            solver = get_solver("qaoa", reps=reps)
            return solver.solve(obj, constraints, **context), None
        except Exception as exc:  # qiskit missing / primitive mismatch
            # graceful classical stand-in so the pipeline never breaks
            fallback = "classical" if obj.form == "discretized_mv" else "mock"
            solver = get_solver(fallback)
            shim = build_objective("min_variance", _as_moment_shim(obj))
            return solver.solve(shim, constraints, **context), f"qaoa_unavailable: {exc!r}"

    solver = get_solver(arm.solver)
    return solver.solve(obj, constraints, **context), None


# ---------------------------------------------------------------------------
# backtest policy
# ---------------------------------------------------------------------------
def build_policy(arm: Arm, *, moments: MomentsConfig | None = None,
                 constraints: Constraints | None = None):
    """Return a ``policy(snapshot) -> Weights`` for the backtest engine."""

    def policy(snapshot: DataSnapshot) -> Weights:
        w, _diag = solve_arm(arm, snapshot, moments=moments, constraints=constraints)
        return w

    policy.__name__ = f"policy_{arm.id}"
    return policy


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _benchmark_weights(name: str, snapshot: DataSnapshot) -> Weights:
    tickers = snapshot.tickers
    if name == "equal_weight":
        return Weights.equal(tickers)
    if name == "sixty_forty":
        uni = load_universe()
        target = uni.benchmarks.get("sixty_forty", {})
        vals = [float(target.get(t, 0.0)) for t in tickers]
        s = sum(vals)
        if s <= 0:  # universe doesn't contain the 60/40 legs — degrade to 1/N
            return Weights.equal(tickers)
        return Weights(tickers=tickers, values=[v / s for v in vals])
    raise ValueError(f"unknown benchmark {name!r}")


def _objective_form(objective_name: str) -> str:
    return {
        "min_variance": "min_variance",
        "risk_parity": "min_variance",     # solver reads cov only
        "hrp": "min_variance",
        "scenario_cvar": "min_variance",   # shell; cvar solver reads the returns panel
        "mvsk": "mvsk",
        "max_utility": "max_utility",
        "selection_qubo": "selection_qubo",
        "discretized_mv": "discretized_mv",
    }.get(objective_name, objective_name)


def _as_moment_shim(obj) -> MomentSet:
    return MomentSet(tickers=obj.tickers, as_of=__import__("datetime").date.today(),
                     cov=obj.cov)
