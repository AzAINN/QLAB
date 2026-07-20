"""Offline-only access to the retained Aer QAOA research implementation."""

from __future__ import annotations

from math import comb
from typing import Any


def mvsk_qubo_resource_count(n: int, resolution_bits: int) -> dict[str, int]:
    """Offline estimate for binarizing and quadratizing degree-4 MVSK.

    This is retained as architecture-research evidence, not as a staged product
    capability or a claim about directly applicable hardware.
    """
    r = resolution_bits
    weight_qubits = n * r
    coskew_entries = comb(n + 2, 3)
    cokurt_entries = comb(n + 3, 4)
    auxiliary_qubits = comb(weight_qubits, 2) + weight_qubits
    return {
        "n_assets": n,
        "resolution_bits": r,
        "weight_qubits": weight_qubits,
        "coskew_entries": coskew_entries,
        "cokurt_entries": cokurt_entries,
        "degree3_monomials": coskew_entries * (r ** 3),
        "degree4_monomials": cokurt_entries * (r ** 4),
        "auxiliary_qubits": auxiliary_qubits,
        "penalty_gadgets": auxiliary_qubits,
        "total_logical_qubits": weight_qubits + auxiliary_qubits,
        "continuous_variables": n,
    }


def get_offline_quantum_solver(name: str, **kwargs: Any):
    """Load an offline quantum solver without adding it to runtime discovery."""
    if name not in {"qaoa", "qubo_resource_count"}:
        raise KeyError(f"unknown offline quantum solver {name!r}")
    from qlab.solvers import quantum  # noqa: F401  (registers offline adapters)
    from qlab.solvers.base import _get_registered_solver

    return _get_registered_solver(name, **kwargs)


def compare_classical_qaoa(snapshot, moments_cfg, constraints) -> dict[str, Any]:
    """Offline same-covariance diagnostic retained for research notebooks.

    This function is intentionally absent from the CLI, HTTP API, and MCP
    server. Importing this module is the explicit opt-in boundary.
    """
    from qlab.arms import estimate
    from qlab.core.objective import build_objective, compile_scipy
    from qlab.solvers.base import get_solver

    moments = estimate(snapshot, moments_cfg, higher=False)
    objective = build_objective("min_variance", moments)
    classical = get_solver("classical").solve(objective, constraints)
    out: dict[str, Any] = {
        "classical": {
            "solver": classical.solver,
            "objective_value": classical.objective_value,
            "wall_clock_s": classical.wall_clock_s,
            "weights": dict(zip(classical.weights.tickers, classical.weights.values)),
        }
    }
    discrete = build_objective(
        "discretized_mv", moments, extra={"resolution_bits": 3}
    )
    qaoa = get_offline_quantum_solver("qaoa", reps=2).solve(discrete, constraints)
    objective_fn, _ = compile_scipy(objective)
    out["qaoa"] = {
        "solver": qaoa.solver,
        "objective_value": float(objective_fn(qaoa.weights.as_array())),
        "wall_clock_s": qaoa.wall_clock_s,
        "weights": dict(zip(qaoa.weights.tickers, qaoa.weights.values)),
        "diagnostics": qaoa.diagnostics,
    }
    return out
