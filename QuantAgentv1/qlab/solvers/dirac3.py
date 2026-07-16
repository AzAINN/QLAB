"""QCI Dirac-3 adapter — the continuous MVSK solver (arm A4).

This is the *structural* quantum fit (research-plan §5.1): Dirac-3 takes the
degree-4 MVSK objective in **7 continuous variables** natively — all-to-all
connectivity, native sum-to-R constraint that *is* the long-only fully-invested
budget — with no binarization, no quadratization, no penalty gadgets. Contrast
the ~434 logical qubits the same objective needs on gate hardware (see
:func:`qlab.core.objective.mvsk_qubo_resource_count`).

Running it requires a QCI account (``QCI_API_TOKEN`` / ``QCI_API_URL``). Without
credentials this adapter raises :class:`Dirac3Unavailable`, which the arm layer
catches to fall back to ``classical_multistart`` — so the pipeline never breaks,
and the compiled HUBO payload is still returned for inspection (you can *see*
exactly what would be submitted).
"""

from __future__ import annotations

import os
import time

import numpy as np

from qlab.core.objective import compile_dirac_hubo, evaluate
from qlab.core.types import Objective, SolveResult, Weights
from qlab.solvers.base import Constraints, Solver, register_solver


class Dirac3Unavailable(RuntimeError):
    """Raised when Dirac-3 credentials/SDK are absent. Carries the HUBO payload."""

    def __init__(self, message: str, payload: dict | None = None):
        super().__init__(message)
        self.payload = payload or {}


@register_solver("dirac3")
class Dirac3Solver(Solver):
    def __init__(self, num_samples: int = 20, relaxation_schedule: int = 2):
        self.num_samples = num_samples
        self.relaxation_schedule = relaxation_schedule

    def solve(self, objective: Objective, constraints: Constraints, **_ctx) -> SolveResult:
        t0 = time.perf_counter()
        payload = compile_dirac_hubo(objective, budget=constraints.budget)

        token = os.environ.get("QCI_API_TOKEN")
        url = os.environ.get("QCI_API_URL")
        if not (token and url):
            raise Dirac3Unavailable(
                "QCI_API_TOKEN / QCI_API_URL not set — Dirac-3 arm unavailable. "
                "Falling back to classical_multistart. The compiled continuous-HUBO "
                "payload is attached for inspection.",
                payload=payload,
            )

        # --- live submission path (requires the qci-client SDK) --------------
        try:
            w = self._submit(objective, constraints, payload, token, url)
        except Dirac3Unavailable:
            raise
        except Exception as exc:  # network / SDK / decode — surface, don't fake
            raise Dirac3Unavailable(f"Dirac-3 submission failed: {exc!r}", payload)

        from qlab.solvers.base import finalize_weights

        w = finalize_weights(w, constraints)
        constraints.validate(w)
        return SolveResult(
            weights=Weights(tickers=objective.tickers, values=[float(x) for x in w]),
            objective_value=evaluate(objective, w),
            solver="dirac3", status="optimal",
            wall_clock_s=time.perf_counter() - t0,
            diagnostics={"payload": payload, "num_samples": self.num_samples},
        )

    def _submit(self, objective, constraints, payload, token, url) -> np.ndarray:
        """Encode MVSK as an EQC polynomial and submit to Dirac-3.

        Implemented against QCI's ``eqc_models`` / ``qci_client`` SDK. Kept behind
        a lazy import so the package installs and runs without the QCI stack.
        """
        try:
            from eqc_models.solvers import Dirac3ContinuousCloudSolver  # type: ignore
            from eqc_models.base import PolynomialModel  # type: ignore
        except ImportError as exc:
            raise Dirac3Unavailable(
                f"qci eqc_models SDK not installed ({exc}); "
                "`pip install eqc-models` to enable the Dirac-3 arm.",
                payload,
            )

        # Build the degree-≤4 polynomial (coefficients, indices) from the tensors.
        coeffs, indices = _mvsk_polynomial(objective)
        model = PolynomialModel(coefficients=coeffs, indices=indices)
        model.sum_constraint = constraints.budget            # native sum-to-R
        solver = Dirac3ContinuousCloudSolver(url=url, api_token=token)
        response = solver.solve(
            model,
            sum_constraint=constraints.budget,
            relaxation_schedule=self.relaxation_schedule,
            num_samples=self.num_samples,
        )
        solutions = response["results"]["solutions"]
        return np.asarray(solutions[0], dtype=float)


def _mvsk_polynomial(obj: Objective) -> tuple[list[float], list[list[int]]]:
    """Flatten Σ / coskew / cokurt into (coeff, index-tuple) polynomial terms.

    Indices are 1-based with 0 as the padding sentinel, matching QCI's fixed-
    degree polynomial convention (degree 4 → 4-length index tuples).
    """
    n = obj.n
    coeffs: list[float] = []
    indices: list[list[int]] = []

    def add(coef: float, idx: list[int]):
        if abs(coef) < 1e-15:
            return
        pad = [0] * (4 - len(idx)) + [i + 1 for i in idx]
        coeffs.append(float(coef))
        indices.append(pad)

    for i in range(n):
        for j in range(n):
            add(obj.cov[i, j], [i, j])
    if obj.coskew is not None and obj.skew_lambda:
        for i in range(n):
            for j in range(n):
                for k in range(n):
                    add(-obj.skew_lambda * obj.coskew[i, j, k], [i, j, k])
    if obj.cokurt is not None and obj.kurt_lambda:
        it = np.ndindex(n, n, n, n)
        for (i, j, k, l) in it:
            add(obj.kurt_lambda * obj.cokurt[i, j, k, l], [i, j, k, l])
    return coeffs, indices
