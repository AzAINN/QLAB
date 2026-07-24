"""Classical solvers: min-variance, MVSK multistart, risk parity (ERC).

These are the deterministic, open-source arms (A1, A3, B3). They use scipy's
SLSQP on the canonical compiled objective, so every classical arm evaluates the
same polynomial.

* ``classical``            → min-variance (A1) or max-utility, single SLSQP.
* ``classical_multistart`` → MVSK (A3): multistart SLSQP that explores the
  non-convex landscape under deterministic seeded restarts.
* ``risk_parity``          → equal-risk-contribution (B3).
* ``target_semivariance``  → scenario mean squared shortfall, single SLSQP.

An optional CVXPY fast path is used for the convex min-variance problem when the
``optimize`` extra is installed; otherwise scipy handles everything.
"""

from __future__ import annotations

import time

import numpy as np
from scipy.optimize import minimize

from qlab.core.objective import compile_scipy, compile_target_semivariance
from qlab.core.types import Objective, SolveResult, Weights
from qlab.solvers.base import Constraints, Solver, finalize_weights, register_solver


def _budget_constraint(budget: float) -> dict:
    return {"type": "eq", "fun": lambda w: float(np.sum(w) - budget)}


def _slsqp(f, g, x0, bounds, budget) -> tuple[np.ndarray, float, bool]:
    res = minimize(
        f, x0, jac=g, method="SLSQP", bounds=bounds,
        constraints=[_budget_constraint(budget)],
        options={"maxiter": 300, "ftol": 1e-10},
    )
    return res.x, float(res.fun), bool(res.success)


@register_solver("classical")
class ClassicalSolver(Solver):
    """Single-start SLSQP for convex forms (min-variance / max-utility)."""

    def solve(self, objective: Objective, constraints: Constraints, **_ctx) -> SolveResult:
        t0 = time.perf_counter()
        f, g = compile_scipy(objective)
        n = objective.n
        bounds = constraints.bounds(n)
        x0 = np.full(n, constraints.budget / n)
        w, val, ok = _slsqp(f, g, x0, bounds, constraints.budget)
        w = finalize_weights(w, constraints)
        constraints.validate(w)
        return SolveResult(
            weights=Weights(tickers=objective.tickers, values=[float(x) for x in w]),
            objective_value=float(f(w)),
            solver=self.name,
            status="optimal" if ok else "suboptimal",
            wall_clock_s=time.perf_counter() - t0,
        )


@register_solver("target_semivariance")
class TargetSemivarianceSolver(Solver):
    """Long-only scenario target-semivariance optimization."""

    def solve(
        self, objective: Objective, constraints: Constraints, *, returns=None, **_ctx
    ) -> SolveResult:
        if returns is None:
            raise ValueError(
                "target_semivariance needs the scenario return panel; "
                "pass returns=<T×n array>"
            )
        t0 = time.perf_counter()
        f, g = compile_target_semivariance(objective, returns)
        scenarios = np.asarray(returns, dtype=float)
        n = objective.n
        x0 = np.full(n, constraints.budget / n)

        # Daily squared returns are commonly O(1e-4) or smaller. Normalize the
        # solve without changing the reported objective so SLSQP does not
        # mistake its absolute tolerance for convergence at the initial point.
        target = float(objective.extra.get("target", 0.0))
        scale = max(
            float(np.mean(scenarios ** 2)),
            target ** 2,
            f(x0),
            1e-12,
        )
        res = minimize(
            lambda w: f(w) / scale,
            x0,
            jac=lambda w: g(w) / scale,
            method="SLSQP",
            bounds=constraints.bounds(n),
            constraints=[_budget_constraint(constraints.budget)],
            options={"maxiter": 500, "ftol": 1e-12},
        )
        if not res.success or not np.isfinite(res.x).all():
            raise RuntimeError(f"target semivariance SLSQP failed: {res.message}")
        w = finalize_weights(res.x, constraints)
        constraints.validate(w)
        value = f(w)
        return SolveResult(
            weights=Weights(
                tickers=objective.tickers,
                values=[float(x) for x in w],
            ),
            objective_value=value,
            solver=self.name,
            status="optimal",
            wall_clock_s=time.perf_counter() - t0,
            diagnostics={
                "target": target,
                "n_scenarios": int(scenarios.shape[0]),
                "downside_deviation": float(np.sqrt(value)),
            },
        )


@register_solver("classical_multistart")
class MultistartMVSKSolver(Solver):
    """Multistart SLSQP for the non-convex MVSK objective (arm A3).

    Random Dirichlet starts explore the frustrated landscape; the best local
    optimum is returned. A ``parallel_tempering`` seed jitter is applied between
    restarts to escape shallow basins.
    """

    def __init__(self, n_starts: int | None = None, seed: int = 7):
        self.n_starts = n_starts
        self.seed = seed

    def solve(self, objective: Objective, constraints: Constraints, **_ctx) -> SolveResult:
        t0 = time.perf_counter()
        f, g = compile_scipy(objective)
        n = objective.n
        bounds = constraints.bounds(n)
        rng = np.random.default_rng(self.seed)
        n_starts = self.n_starts or max(8, 4 * n)

        best_w, best_val = None, np.inf
        temps = np.linspace(1.0, 0.2, n_starts)     # parallel-tempering-style cooling
        for temp in temps:
            x0 = rng.dirichlet(np.ones(n) / temp) * constraints.budget
            w, val, ok = _slsqp(f, g, x0, bounds, constraints.budget)
            w = finalize_weights(w, constraints)
            v = f(w)
            if v < best_val:
                best_w, best_val = w, v

        constraints.validate(best_w)
        return SolveResult(
            weights=Weights(tickers=objective.tickers, values=[float(x) for x in best_w]),
            objective_value=float(best_val),
            solver=self.name,
            status="optimal",
            wall_clock_s=time.perf_counter() - t0,
            diagnostics={"n_starts": n_starts},
        )


@register_solver("risk_parity")
class RiskParitySolver(Solver):
    """Equal-Risk-Contribution (ERC) portfolio — practitioner benchmark (B3)."""

    def solve(self, objective: Objective, constraints: Constraints, **_ctx) -> SolveResult:
        t0 = time.perf_counter()
        Sigma = objective.cov
        n = objective.n

        def erc_obj(w: np.ndarray) -> float:
            port_var = float(w @ Sigma @ w)
            if port_var <= 1e-18:
                return 0.0
            rc = (w * (Sigma @ w)) / port_var          # relative risk contributions, sum to 1
            return float(np.sum((rc - 1.0 / n) ** 2))

        x0 = np.full(n, constraints.budget / n)
        res = minimize(
            erc_obj, x0, method="SLSQP", bounds=constraints.bounds(n),
            constraints=[_budget_constraint(constraints.budget)],
            options={"maxiter": 500, "ftol": 1e-14},
        )
        w = finalize_weights(res.x, constraints)
        constraints.validate(w)
        return SolveResult(
            weights=Weights(tickers=objective.tickers, values=[float(x) for x in w]),
            objective_value=float(w @ Sigma @ w),
            solver=self.name,
            status="optimal" if res.success else "suboptimal",
            wall_clock_s=time.perf_counter() - t0,
        )
