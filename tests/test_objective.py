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
    term_contributions,
)
from qlab.core.moments import co_moments, ledoit_wolf
from qlab.core.types import MomentSet
from datetime import date


def _brute_force_mvsk(w, cov, coskew, cokurt, l3, l4):
    val = float(w @ cov @ w)
    val -= l3 * float(np.einsum("ijk,i,j,k->", coskew, w, w, w))
    val += l4 * float(np.einsum("ijkl,i,j,k,l->", cokurt, w, w, w, w))
    return val


def test_scipy_value_matches_brute_force(moment_set):
    # raw scale: this test pins the polynomial arithmetic (cross-compiler
    # agreement) to literal lambda=0.5, independent of R0.1 auto-scaling.
    obj = build_objective("mvsk", moment_set, skew_lambda=0.5, kurt_lambda=0.5,
                          lambda_scale="raw")
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


# ---------------------------------------------------------------------------
# R0.1: auto-scaled MVSK lambdas (this helper is also used by later tasks)
# ---------------------------------------------------------------------------
def _heavy_tailed_ms(seed=3, n=5, T=750):
    rng = np.random.default_rng(seed)
    X = rng.standard_t(df=4, size=(T, n)) * 0.01
    X[:, 0] -= (rng.random(T) < 0.03) * 0.05          # asymmetric crash asset
    cov, _ = ledoit_wolf(X)
    S, K = co_moments(X, cov, comoment_shrinkage=0.0)
    return MomentSet(tickers=[f"A{i}" for i in range(n)], as_of=date(2020, 1, 1),
                     cov=cov, coskew=S, cokurt=K), X


def test_auto_scaled_terms_are_comparable():
    ms, _ = _heavy_tailed_ms()
    obj = build_objective("mvsk", ms, skew_lambda=0.5, kurt_lambda=0.5)
    w0 = np.full(obj.n, 1.0 / obj.n)
    c = term_contributions(obj, w0)
    assert c["variance"] > 0
    # each active term within one order of magnitude of 0.5x variance
    assert 0.05 * c["variance"] < abs(c["skew_term"]) < 5.0 * c["variance"]
    assert 0.05 * c["variance"] < abs(c["kurt_term"]) < 5.0 * c["variance"]
    assert obj.extra["lambda_scale"] == "auto"
    assert obj.extra["skew_lambda_raw"] == 0.5
    # Exact identity: for this data the equal-weight contraction is well away
    # from zero (verified: m3/m4 both exceed their norm-based floors — see
    # objective.py's build_objective docstring), so the auto-scale formula's
    # construction gives |term| == lambda_raw * variance exactly at w0.
    assert abs(c["skew_term"]) == pytest.approx(0.5 * c["variance"], rel=1e-6)
    assert abs(c["kurt_term"]) == pytest.approx(0.5 * c["variance"], rel=1e-6)


def test_raw_scale_preserves_old_behavior():
    ms, _ = _heavy_tailed_ms()
    obj = build_objective("mvsk", ms, skew_lambda=0.5, kurt_lambda=0.5,
                          lambda_scale="raw")
    assert obj.skew_lambda == 0.5 and obj.kurt_lambda == 0.5


def test_auto_scale_degenerate_tensors_do_not_blow_up():
    # If the m3/m4 denominators floor at a bare epsilon (1e-30) instead of a
    # norm-based floor, a large-but-cancelling tensor sends lambda_eff to
    # meaningless values. Zero tensors are the extreme case: contraction is
    # exactly zero, so lambda_eff would explode under a naive epsilon floor.
    n = 4
    cov = np.eye(n) * 1e-4
    ms = MomentSet(
        tickers=[f"A{i}" for i in range(n)], as_of=date(2020, 1, 1),
        cov=cov, coskew=np.zeros((n, n, n)), cokurt=np.zeros((n, n, n, n)),
    )
    obj = build_objective("mvsk", ms, skew_lambda=0.5, kurt_lambda=0.5)
    assert np.isfinite(obj.skew_lambda)
    assert np.isfinite(obj.kurt_lambda)
    f, _ = compile_scipy(obj)
    assert np.isfinite(f(np.full(n, 0.25)))

    # coskew/cokurt entirely absent: the auto branch must not divide by
    # anything at all for these terms, and lambdas pass through untouched.
    ms_none = MomentSet(
        tickers=[f"A{i}" for i in range(n)], as_of=date(2020, 1, 1),
        cov=cov, coskew=None, cokurt=None,
    )
    obj_none = build_objective("mvsk", ms_none, skew_lambda=0.5, kurt_lambda=0.5)
    assert np.isfinite(obj_none.skew_lambda)
    assert np.isfinite(obj_none.kurt_lambda)


def test_mvsk_diverges_from_min_variance_when_scaled():
    ms, _ = _heavy_tailed_ms()
    from qlab.solvers.base import Constraints, get_solver
    c = Constraints(max_weight=0.6)
    w_mv = get_solver("classical").solve(build_objective("min_variance", ms), c).weights.as_array()
    w_mvsk = get_solver("classical_multistart").solve(
        build_objective("mvsk", ms, skew_lambda=1.0, kurt_lambda=1.0), c).weights.as_array()
    assert np.abs(w_mv - w_mvsk).sum() > 0.02
