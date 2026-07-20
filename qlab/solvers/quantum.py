"""Offline Qiskit research arms — excluded from the staged runtime.

Retained experiments:

* **Asset-selection QUBO** (``objective.form == 'selection_qubo'``): pick
  k≈7 of ~19 candidate ETFs. A relevance/redundancy QUBO + cardinality penalty.
  At ≤19 qubits the exact ground state is *enumerable*, so we report a rigorous
  QAOA optimality gap rather than a vibe. This is the real gate-model slot and
  it remains an isolated selection experiment.
* **Discretized MV** (``objective.form == 'discretized_mv'``): the textbook
  qiskit-finance formulation (n=7, r=3 → 21 qubits, covariance only). Reports two
  gaps: QAOA-vs-exact and best-discrete-vs-continuous.
* **Parameterized MVSK encoding estimate** (``solver
  'qubo_resource_count'``): reports construction dimensions for offline
  architecture analysis. It is not evidence of hardware applicability.

Integration notes (spec "Revisions"): Aer's deprecated V1 Sampler is
incompatible with current qiskit-algorithms and the QAOA ansatz needs explicit
transpilation for Aer, so we (a) always compute the exact ground state via
``NumPyMinimumEigensolver`` (stable, enumerable), and (b) attempt the QAOA
sample on top, degrading gracefully with a logged error if the primitive stack
mismatches. Quantum tools must run on the **main thread** (no worker pool) — the
offline harness enforces this. Import through
``qlab.algorithms.offline.get_offline_quantum_solver``; normal solver discovery
and every MCP/HTTP/TUI path intentionally exclude these adapters.
"""

from __future__ import annotations

import time

import numpy as np

from qlab.algorithms.offline.quantum import mvsk_qubo_resource_count
from qlab.core.objective import evaluate
from qlab.core.types import Objective, SolveResult, Weights
from qlab.solvers.base import Constraints, Solver, register_solver


# ---------------------------------------------------------------------------
# QUBO builders (pure — no qiskit needed to construct the problem)
# ---------------------------------------------------------------------------
def build_selection_qubo(cov: np.ndarray, k: int, *, lam_rel=1.0, lam_red=1.0):
    """Relevance/redundancy selection QUBO with an equality cardinality target.

    Diagonal rewards standalone diversification value (low-vol assets);
    off-diagonal penalizes redundancy (correlation). Returns a
    ``qiskit_optimization.QuadraticProgram`` with a ``Σ x = k`` constraint.
    """
    from qiskit_optimization import QuadraticProgram

    n = cov.shape[0]
    d = np.sqrt(np.clip(np.diag(cov), 1e-18, None))
    corr = np.abs(cov / np.outer(d, d))
    relevance = 1.0 / d                                  # low vol => relevant
    relevance = relevance / relevance.max()

    qp = QuadraticProgram("selection")
    for i in range(n):
        qp.binary_var(name=f"x{i}")
    linear = {f"x{i}": float(-lam_rel * relevance[i]) for i in range(n)}
    quadratic = {
        (f"x{i}", f"x{j}"): float(lam_red * corr[i, j])
        for i in range(n) for j in range(i + 1, n)
    }
    qp.minimize(linear=linear, quadratic=quadratic)
    qp.linear_constraint(
        linear={f"x{i}": 1 for i in range(n)}, sense="==", rhs=k, name="cardinality"
    )
    return qp


def build_discretized_mv_qp(cov: np.ndarray, resolution_bits: int):
    """Discretized minimum-variance as an integer program (degree-2 only).

    Each asset gets an integer level; weights are levels / budget. Returns the
    ``QuadraticProgram`` and the integer budget ``B`` used for decoding.
    """
    from qiskit_optimization import QuadraticProgram

    n = cov.shape[0]
    B = 2 ** resolution_bits - 1
    qp = QuadraticProgram("discretized_mv")
    for i in range(n):
        qp.integer_var(lowerbound=0, upperbound=B, name=f"L{i}")
    # objective (1/B^2) L' Σ L
    quad = {(f"L{i}", f"L{j}"): float(cov[i, j] / (B * B))
            for i in range(n) for j in range(n)}
    qp.minimize(quadratic=quad)
    qp.linear_constraint(
        linear={f"L{i}": 1 for i in range(n)}, sense="==", rhs=B, name="budget"
    )
    return qp, B


# ---------------------------------------------------------------------------
# qiskit solve helper — exact always; QAOA best-effort
# ---------------------------------------------------------------------------
def _solve_qubo(qp, reps: int, run_qaoa: bool = True) -> dict:
    from qiskit_optimization.algorithms import MinimumEigenOptimizer
    from qiskit_optimization.converters import QuadraticProgramToQubo

    conv = QuadraticProgramToQubo()
    qubo = conv.convert(qp)
    n_qubits = qubo.get_num_binary_vars()

    from qiskit_algorithms import NumPyMinimumEigensolver

    exact_opt = MinimumEigenOptimizer(NumPyMinimumEigensolver())
    exact = exact_opt.solve(qubo)

    out = {
        "n_qubits": int(n_qubits),
        "exact_x": np.asarray(exact.x, dtype=float),
        "exact_value": float(exact.fval),
        "converter": conv,
        "qubo": qubo,
    }

    if run_qaoa:
        try:
            from qiskit_optimization.algorithms import MinimumEigenOptimizer as MEO

            qaoa = _make_qaoa(reps)
            qres = MEO(qaoa).solve(qubo)
            out["qaoa_x"] = np.asarray(qres.x, dtype=float)
            out["qaoa_value"] = float(qres.fval)
            out["qaoa_sampler"] = qaoa._sampler_label  # type: ignore[attr-defined]
            # optimality gap on the QUBO energy (enumerable exact ground state)
            denom = abs(out["exact_value"]) + 1e-12
            out["optimality_gap"] = abs(out["qaoa_value"] - out["exact_value"]) / denom
        except Exception as exc:                        # version/primitive mismatch
            out["qaoa_error"] = repr(exc)
    return out


def _make_qaoa(reps: int):
    """Build a QAOA that actually runs on the Aer simulator under qiskit >= 2.

    qiskit-algorithms 0.4 uses V2 primitives, and a V2 ``SamplerV2`` requires the
    parameterized ansatz to be transpiled to ISA circuits — so QAOA takes a
    ``transpiler`` pass manager (the spec's "QAOA ansatz needs explicit
    transpilation for Aer" finding, resolved). Noiseless Aer = the training
    simulator; only the final optimized circuit would go to real hardware.
    """
    from qiskit_aer import AerSimulator
    from qiskit_algorithms import QAOA
    from qiskit_algorithms.optimizers import COBYLA
    from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager

    backend = AerSimulator()
    pm = generate_preset_pass_manager(optimization_level=1, backend=backend)
    try:
        from qiskit_aer.primitives import SamplerV2 as AerSamplerV2

        # Fixed shots + seed: makes the noiseless-Aer result reproducible and
        # avoids the intermittent empty-distribution parse error at larger qubit
        # counts when two QAOA runs execute back-to-back in one process.
        try:
            sampler = AerSamplerV2(default_shots=4096, seed=7)
        except TypeError:
            sampler = AerSamplerV2()
        label = "aer_sampler_v2"
    except Exception:  # pragma: no cover
        from qiskit.primitives import StatevectorSampler

        sampler = StatevectorSampler()
        label = "statevector_sampler_v2"

    qaoa = QAOA(sampler=sampler, optimizer=COBYLA(maxiter=100), reps=reps, transpiler=pm)
    qaoa._sampler_label = label
    return qaoa


# ---------------------------------------------------------------------------
# Solvers
# ---------------------------------------------------------------------------
@register_solver("qaoa")
class QAOASolver(Solver):
    """Offline adapter for selection-QUBO and discretized-MV experiments."""

    def __init__(self, reps: int = 2, run_qaoa: bool = True):
        self.reps = reps
        self.run_qaoa = run_qaoa

    def solve(self, objective: Objective, constraints: Constraints, **ctx) -> SolveResult:
        t0 = time.perf_counter()
        if objective.form == "selection_qubo":
            return self._solve_selection(objective, constraints, t0, **ctx)
        if objective.form == "discretized_mv":
            return self._solve_discretized(objective, constraints, t0, **ctx)
        raise ValueError(
            f"qaoa solver handles selection_qubo / discretized_mv, not {objective.form!r}"
        )

    # -- asset-selection QUBO ------------------------------------------------
    def _solve_selection(self, obj, constraints, t0, *, k=None, **_):
        k = k or obj.extra.get("k", max(1, obj.n // 2))
        qp = build_selection_qubo(obj.cov, k)
        r = _solve_qubo(qp, self.reps, self.run_qaoa)
        x = r.get("qaoa_x", r["exact_x"])[: obj.n]
        selected = [obj.tickers[i] for i, xi in enumerate(x) if xi > 0.5]
        # equal-weight the selected basket as this arm's portfolio
        w = np.array([1.0 / len(selected) if t in selected else 0.0
                      for t in obj.tickers]) if selected else np.full(obj.n, 1.0 / obj.n)
        diag = {"selected": selected, "k": int(k), "n_qubits": r["n_qubits"],
                "exact_value": r["exact_value"]}
        for key in ("qaoa_value", "optimality_gap", "qaoa_error", "qaoa_sampler"):
            if key in r:
                diag[key] = r[key]
        return SolveResult(
            weights=Weights(tickers=obj.tickers, values=[float(v) for v in w]),
            objective_value=float(r.get("qaoa_value", r["exact_value"])),
            solver="qaoa", status="optimal",
            wall_clock_s=time.perf_counter() - t0, diagnostics=diag,
        )

    # -- discretized minimum variance ---------------------------------------
    def _solve_discretized(self, obj, constraints, t0, **_):
        r_bits = int(obj.extra.get("resolution_bits", 3))
        qp, B = build_discretized_mv_qp(obj.cov, r_bits)
        r = _solve_qubo(qp, self.reps, self.run_qaoa)
        levels = r.get("qaoa_x", r["exact_x"])[: obj.n]
        s = levels.sum()
        w = (levels / s) if s > 0 else np.full(obj.n, 1.0 / obj.n)
        diag = {"resolution_bits": r_bits, "n_qubits": r["n_qubits"],
                "best_discrete_variance": float(w @ obj.cov @ w)}
        for key in ("qaoa_value", "optimality_gap", "qaoa_error", "exact_value",
                    "qaoa_sampler"):
            if key in r:
                diag[key] = r[key]
        return SolveResult(
            weights=Weights(tickers=obj.tickers, values=[float(v) for v in w]),
            objective_value=float(w @ obj.cov @ w),
            solver="qaoa", status="optimal",
            wall_clock_s=time.perf_counter() - t0, diagnostics=diag,
        )


@register_solver("qubo_resource_count")
class QUBOResourceCountSolver(Solver):
    """Build a parameterized MVSK-to-QUBO construction estimate.

    Returns no meaningful portfolio (placeholder equal weights). The diagnostics
    are a combinatorial upper bound, not a staged solver or hardware-fit claim.
    Requires no qiskit runtime.
    """

    def solve(self, objective: Objective, constraints: Constraints, **ctx) -> SolveResult:
        t0 = time.perf_counter()
        r = int(ctx.get("resolution_bits", objective.extra.get("resolution_bits", 4)))
        count = mvsk_qubo_resource_count(objective.n, r)
        w = np.full(objective.n, 1.0 / objective.n)
        return SolveResult(
            weights=Weights(tickers=objective.tickers, values=[float(v) for v in w]),
            objective_value=evaluate(objective, w),
            solver="qubo_resource_count", status="analysis_only",
            wall_clock_s=time.perf_counter() - t0, diagnostics=count,
        )
