"""Solver arms: feasibility, the mandate cap, and classical/mock agreement."""

from __future__ import annotations

import numpy as np
import pytest

from qlab.core.objective import build_objective
from qlab.solvers.base import Constraints, get_solver, project_capped_simplex


def _check_feasible(res, constraints):
    w = res.weights.as_array()
    assert abs(w.sum() - constraints.budget) < 1e-2
    assert (w >= -1e-6).all()
    assert (w <= constraints.max_weight + 1e-6).all()


@pytest.mark.parametrize("solver_name,form", [
    ("classical", "min_variance"),
    ("classical_multistart", "mvsk"),
    ("risk_parity", "min_variance"),
    ("hrp", "min_variance"),
    ("mock", "min_variance"),
])
def test_solvers_produce_feasible_weights(moment_set, solver_name, form):
    obj = build_objective(form, moment_set, skew_lambda=0.5, kurt_lambda=0.5)
    c = Constraints(max_weight=0.40)
    res = get_solver(solver_name).solve(obj, c)
    _check_feasible(res, c)


def test_cvar_lp_needs_returns_and_is_feasible(moment_set, snap):
    obj = build_objective("min_variance", moment_set)
    c = Constraints(max_weight=0.40)
    returns = snap.log_returns(504).to_numpy()
    res = get_solver("cvar_lp").solve(obj, c, returns=returns)
    _check_feasible(res, c)


def test_min_variance_beats_equal_weight_on_variance(moment_set):
    obj = build_objective("min_variance", moment_set)
    c = Constraints()
    res = get_solver("classical").solve(obj, c)
    w_mv = res.weights.as_array()
    w_eq = np.full(obj.n, 1.0 / obj.n)
    assert w_mv @ obj.cov @ w_mv <= w_eq @ obj.cov @ w_eq + 1e-9


def test_capped_simplex_respects_cap():
    v = np.array([10.0, 1.0, 1.0, 1.0])
    w = project_capped_simplex(v, budget=1.0, cap=0.4)
    assert abs(w.sum() - 1.0) < 1e-9
    assert w.max() <= 0.4 + 1e-9


def test_dirac3_unavailable_without_credentials(moment_set, monkeypatch):
    from qlab.solvers.dirac3 import Dirac3Unavailable

    monkeypatch.delenv("QCI_API_TOKEN", raising=False)
    monkeypatch.delenv("QCI_API_URL", raising=False)
    obj = build_objective("mvsk", moment_set, skew_lambda=0.5, kurt_lambda=0.5)
    with pytest.raises(Dirac3Unavailable) as exc:
        get_solver("dirac3").solve(obj, Constraints())
    # the compiled continuous-HUBO payload is still attached for inspection
    assert exc.value.payload["n_variables"] == obj.n
