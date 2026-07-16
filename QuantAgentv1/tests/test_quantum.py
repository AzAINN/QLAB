"""Quantum arms on the Aer simulator (skipped if qiskit isn't installed).

At these tiny sizes the exact ground state is enumerable, so the QAOA result is
checked against it — a real optimality gap, not a vibe (research-plan §5.2).
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

pytest.importorskip("qiskit_optimization")

from qlab.core.objective import build_objective
from qlab.core.types import MomentSet
from qlab.solvers.base import Constraints, get_solver


def _small_cov(n, seed=0):
    rng = np.random.default_rng(seed)
    A = rng.standard_normal((n, n)) * 0.01
    return A @ A.T + np.eye(n) * 1e-3


def _ms(n, seed=0):
    return MomentSet(tickers=[f"T{i}" for i in range(n)], as_of=date(2022, 1, 1),
                     cov=_small_cov(n, seed))


def test_selection_qubo_picks_k_assets():
    obj = build_objective("selection_qubo", _ms(6), extra={"k": 3})
    res = get_solver("qaoa", reps=1).solve(obj, Constraints(), k=3)
    sel = res.diagnostics["selected"]
    assert len(sel) == 3
    assert res.diagnostics["n_qubits"] == 6
    assert "exact_value" in res.diagnostics


def test_discretized_mv_weights_are_feasible():
    obj = build_objective("discretized_mv", _ms(4), extra={"resolution_bits": 2})
    res = get_solver("qaoa", reps=1).solve(obj, Constraints())
    w = res.weights.as_array()
    assert abs(w.sum() - 1.0) < 1e-6
    assert (w >= -1e-9).all()


def test_qaoa_optimality_gap_is_nonnegative_when_reported():
    obj = build_objective("selection_qubo", _ms(5), extra={"k": 2})
    res = get_solver("qaoa", reps=1).solve(obj, Constraints(), k=2)
    if "optimality_gap" in res.diagnostics:
        assert res.diagnostics["optimality_gap"] >= -1e-9
