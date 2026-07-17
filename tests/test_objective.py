"""The one-true-objective: cross-compiler agreement + the 434-vs-7 resource count.

If the scipy compilation and a brute-force polynomial disagree, the solver arms
would be optimizing different things and every comparison would be invalid
(research-plan invariant 4). These tests pin that down.
"""

from __future__ import annotations

import numpy as np
import pytest

from qlab.core.objective import (
    build_objective,
    compile_dirac_hubo,
    compile_scipy,
    mvsk_qubo_resource_count,
)


def _brute_force_mvsk(w, cov, coskew, cokurt, l3, l4):
    val = float(w @ cov @ w)
    val -= l3 * float(np.einsum("ijk,i,j,k->", coskew, w, w, w))
    val += l4 * float(np.einsum("ijkl,i,j,k,l->", cokurt, w, w, w, w))
    return val


def test_scipy_value_matches_brute_force(moment_set):
    obj = build_objective("mvsk", moment_set, skew_lambda=0.5, kurt_lambda=0.5)
    f, _ = compile_scipy(obj)
    rng = np.random.default_rng(3)
    for _ in range(20):
        w = rng.dirichlet(np.ones(obj.n))
        expected = _brute_force_mvsk(w, obj.cov, obj.coskew, obj.cokurt, 0.5, 0.5)
        assert abs(f(w) - expected) < 1e-10


def test_gradient_matches_finite_difference(moment_set):
    obj = build_objective("mvsk", moment_set, skew_lambda=0.5, kurt_lambda=0.5)
    f, g = compile_scipy(obj)
    rng = np.random.default_rng(4)
    w = rng.dirichlet(np.ones(obj.n))
    analytic = g(w)
    eps = 1e-6
    for i in range(obj.n):
        wp, wm = w.copy(), w.copy()
        wp[i] += eps
        wm[i] -= eps
        fd = (f(wp) - f(wm)) / (2 * eps)
        assert abs(analytic[i] - fd) < 1e-4


def test_qubo_resource_count_reproduces_headline():
    rc = mvsk_qubo_resource_count(7, 4)
    assert rc["weight_qubits"] == 28
    assert rc["coskew_entries"] == 84          # C(9,3)
    assert rc["cokurt_entries"] == 210         # C(10,4)
    assert rc["auxiliary_qubits"] == 406       # C(28,2)+28
    assert rc["penalty_gadgets"] == 406
    assert rc["total_logical_qubits"] == 434
    assert rc["dirac3_continuous_variables"] == 7


def test_dirac_hubo_payload_structure(moment_set):
    obj = build_objective("mvsk", moment_set, skew_lambda=0.5, kurt_lambda=0.5)
    payload = compile_dirac_hubo(obj, budget=1.0)
    assert payload["n_variables"] == obj.n
    assert payload["variable_type"] == "continuous"
    assert payload["max_degree"] == 4
    assert payload["sum_constraint"]["equals"] == 1.0
