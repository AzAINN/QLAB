"""Scenario-CVaR optimization (Rockafellar–Uryasev LP) — arm A2.

This is *the honest rival to the entire thesis* (research-plan §6.1). Co-moment
tensors are a **lossy compression** of the empirical return distribution;
scenario-CVaR uses the distribution **directly** and is a linear program that
quantum adds nothing to. If A2 beats the MVSK arms out of sample, the moment-
tensor approach isn't buying anything — so we build it and report it either way.

Formulation (minimize CVaR of losses, risk-only, no expected-return term):

    min_{w, α, u}   α + 1/((1-β)·T) · Σ_t u_t
    s.t.            u_t ≥ −wᵀr_t − α,   u_t ≥ 0,   Σ w = 1,   0 ≤ w ≤ cap
"""

from __future__ import annotations

import time

import numpy as np
from scipy.optimize import linprog

from qlab.core.types import Objective, SolveResult, Weights
from qlab.solvers.base import Constraints, Solver, finalize_weights, register_solver

_MAX_SCENARIOS = 1500  # cap LP size; subsample deterministically beyond this


@register_solver("cvar_lp")
class ScenarioCVaRSolver(Solver):
    def __init__(self, beta: float = 0.95, seed: int = 7):
        self.beta = beta
        self.seed = seed

    def solve(
        self, objective: Objective, constraints: Constraints, *, returns=None, **_ctx
    ) -> SolveResult:
        if returns is None:
            raise ValueError(
                "cvar_lp needs the scenario return panel; pass returns=<T×n array>"
            )
        t0 = time.perf_counter()
        R = np.asarray(returns, dtype=float)
        if R.shape[0] > _MAX_SCENARIOS:
            rng = np.random.default_rng(self.seed)
            idx = rng.choice(R.shape[0], _MAX_SCENARIOS, replace=False)
            R = R[np.sort(idx)]
        T, n = R.shape
        beta = self.beta

        # decision vector x = [w(n), alpha(1), u(T)]
        nv = n + 1 + T
        c = np.zeros(nv)
        c[n] = 1.0
        c[n + 1 :] = 1.0 / ((1.0 - beta) * T)

        # u_t >= -w·r_t - alpha  ->  -w·r_t - alpha - u_t <= 0
        A_ub = np.zeros((T, nv))
        A_ub[:, :n] = -R
        A_ub[:, n] = -1.0
        A_ub[np.arange(T), n + 1 + np.arange(T)] = -1.0
        b_ub = np.zeros(T)

        A_eq = np.zeros((1, nv))
        A_eq[0, :n] = 1.0
        b_eq = np.array([constraints.budget])

        bounds = (
            constraints.bounds(n)              # w
            + [(None, None)]                    # alpha (free)
            + [(0.0, None)] * T                 # u >= 0
        )

        res = linprog(c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq,
                      bounds=bounds, method="highs")
        if not res.success:
            raise RuntimeError(f"CVaR LP failed: {res.message}")
        w = finalize_weights(res.x[:n], constraints)
        constraints.validate(w)
        cvar_value = float(res.fun)
        return SolveResult(
            weights=Weights(tickers=objective.tickers, values=[float(x) for x in w]),
            objective_value=cvar_value,
            solver=self.name,
            status="optimal",
            wall_clock_s=time.perf_counter() - t0,
            diagnostics={"beta": beta, "n_scenarios": int(T), "cvar": cvar_value},
        )
