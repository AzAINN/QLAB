"""Solver arms: feasibility, the mandate cap, and classical/mock agreement."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from qlab.core.objective import build_objective
from qlab.core.types import MomentSet
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


def test_risk_parity_is_not_equal_weight_on_heterogeneous_assets():
    """B3's ERC objective must be dimensionless (relative risk contributions),
    not the raw ``sum((rc - port_var/n)**2)`` form: at daily-return covariance
    scale (~1e-4 entries) the latter is ~1e-11 at the equal-weight start,
    below SLSQP's ftol resolution, so the optimizer exits immediately at x0
    and silently returns 1/N regardless of how heterogeneous the assets are.
    """
    tickers = ["HIVOL", "B", "C", "D"]
    n = len(tickers)
    vols = np.array([0.02, 0.005, 0.005, 0.005])  # asset 0 has 4x the vol
    corr = 0.2
    corr_mat = np.full((n, n), corr)
    np.fill_diagonal(corr_mat, 1.0)
    cov = np.outer(vols, vols) * corr_mat

    ms = MomentSet(tickers=tickers, as_of=date(2022, 6, 30), cov=cov)
    obj = build_objective("min_variance", ms)
    c = Constraints()
    res = get_solver("risk_parity").solve(obj, c)
    w = res.weights.as_array()

    w_eq = np.full(n, 1.0 / n)
    l1 = float(np.abs(w - w_eq).sum())
    assert l1 > 0.05, f"ERC weights collapsed to equal-weight: {w}"
    assert np.argmin(w) == 0, f"high-vol asset should get the smallest weight: {w}"

    Sigma = obj.cov
    port_var = float(w @ Sigma @ w)
    rc = w * (Sigma @ w) / port_var
    assert np.max(np.abs(rc - 1.0 / n)) < 0.02, f"relative risk contributions not equalized: {rc}"


def test_dirac3_unavailable_without_credentials(moment_set, monkeypatch):
    from qlab.solvers.dirac3 import Dirac3Unavailable

    monkeypatch.delenv("QCI_API_TOKEN", raising=False)
    monkeypatch.delenv("QCI_API_URL", raising=False)
    obj = build_objective("mvsk", moment_set, skew_lambda=0.5, kurt_lambda=0.5)
    with pytest.raises(Dirac3Unavailable) as exc:
        get_solver("dirac3").solve(obj, Constraints())
    # the compiled continuous-HUBO payload is still attached for inspection
    assert exc.value.payload["n_variables"] == obj.n
