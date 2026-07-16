"""Deterministic mock solver — dev, tests, and demo fallback.

Result caching and a mock adapter from day one is an explicit risk mitigation
(research-plan §10): it keeps the pipeline exercisable when QCI / IBM QPU are
unavailable, and gives tests a fast, network-free, reproducible arm.

The mock returns inverse-variance weights (a sane, deterministic allocation)
respecting the box + budget constraints.
"""

from __future__ import annotations

import time

import numpy as np

from qlab.core.types import Objective, SolveResult, Weights
from qlab.solvers.base import Constraints, Solver, finalize_weights, register_solver


@register_solver("mock")
class MockSolver(Solver):
    def solve(self, objective: Objective, constraints: Constraints, **_ctx) -> SolveResult:
        t0 = time.perf_counter()
        var = np.clip(np.diag(objective.cov), 1e-12, None)
        w = finalize_weights(1.0 / var, constraints)
        constraints.validate(w)
        from qlab.core.objective import evaluate

        return SolveResult(
            weights=Weights(tickers=objective.tickers, values=[float(x) for x in w]),
            objective_value=evaluate(objective, w),
            solver=self.name,
            status="optimal",
            wall_clock_s=time.perf_counter() - t0,
            diagnostics={"note": "deterministic inverse-variance mock"},
        )
