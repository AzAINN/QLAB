"""The canonical portfolio objective and its staged compilers.

Invariant 4 (research-plan §2.2): *the objective is built once as a coefficient
tensor and compiled to a scipy callable and an optional Dirac-3 continuous-HUBO
payload.* Property-tested for agreement, so
any divergence between solver arms is encoding drift, not solver quality.

The **primary objective is risk-only MVSK** — minimize variance, reward
coskewness, penalize cokurtosis, with **no expected-return term** (research-plan
§4). This pre-empts the strongest critique ("your alpha is just your return
forecast") and still fully exercises the higher-order polynomial:

    f(w) = wᵀΣw  −  λ₃ · (S : w w w)  +  λ₄ · (K : w w w w)

subject to the long-only, fully-invested budget ``Σ wᵢ = 1``, ``w ≥ 0``.

Offline encoding experiments consume the same polynomial terms through the
explicit :mod:`qlab.algorithms.offline` boundary; they are not staged here.

Scenario objectives are intentionally separate from that polynomial compiler.
In particular, target semivariance is evaluated directly on the historical
return panel, just as scenario CVaR is, and has no ``polynomial_terms`` form.
"""

from __future__ import annotations

from collections import Counter
from itertools import combinations_with_replacement
from math import factorial
from typing import Callable

import numpy as np

from qlab.core.types import MomentSet, Objective

_SUPPORTED_FORMS = frozenset({
    "min_variance",
    "mvsk",
    "max_utility",
    "risk_parity",
    "scenario_cvar",
    "target_semivariance",
    "selection_qubo",
    "discretized_mv",
})


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
def build_objective(
    form: str,
    ms: MomentSet,
    *,
    skew_lambda: float = 0.0,
    kurt_lambda: float = 0.0,
    risk_aversion: float = 1.0,
    target: float = 0.0,
    extra: dict | None = None,
    lambda_scale: str = "auto",
) -> Objective:
    """Construct an :class:`Objective` from an estimated :class:`MomentSet`.

    R0.1: daily-return coskew/cokurt tensors are 4-6 orders of magnitude below
    the variance term, so a literal ``λ₃ · (S:www)`` at ``λ=0.5`` was silently
    negligible next to ``wᵀΣw`` — MVSK collapsed to min-variance regardless of
    λ. With ``lambda_scale="auto"`` (the default), ``skew_lambda``/``kurt_lambda``
    are reinterpreted as *"this term is worth this many multiples of the
    variance term at the equal-weight portfolio"* — units-invariant, so the
    higher moments actually contribute. ``lambda_scale="raw"`` preserves the
    old literal-coefficient behavior.

    The auto-scale denominator is a *norm-based* floor, not a bare epsilon:
    the equal-weight contraction ``m3 = |S:w0w0w0|`` (resp. ``m4``) can cancel
    near zero even when the tensor itself is large, which would otherwise send
    λ_eff to (numerically finite but meaningless) huge values. Flooring at a
    fraction of the Frobenius-norm-implied scale keeps λ_eff bounded relative
    to the tensor's actual magnitude under total contraction cancellation.
    """
    if form not in _SUPPORTED_FORMS:
        raise ValueError(
            f"unsupported objective form {form!r}; "
            f"available: {sorted(_SUPPORTED_FORMS)}"
        )
    if lambda_scale not in ("auto", "raw"):
        raise ValueError(f"lambda_scale must be 'auto' or 'raw', got {lambda_scale!r}")
    extra = dict(extra or {})
    if form == "target_semivariance":
        target = float(target)
        if not np.isfinite(target):
            raise ValueError(f"target must be finite, got {target!r}")
        extra["target"] = target
    l3, l4 = float(skew_lambda), float(kurt_lambda)
    if form == "mvsk" and lambda_scale == "auto":
        n = ms.n
        w0 = np.full(n, 1.0 / n)
        var0 = float(w0 @ ms.cov @ w0)
        extra.update({"lambda_scale": "auto",
                      "skew_lambda_raw": l3, "kurt_lambda_raw": l4})
        if ms.coskew is not None and l3:
            m3 = abs(float(np.einsum("ijk,i,j,k->", ms.coskew, w0, w0, w0)))
            # |S:w0^3| <= ||S||_F * ||w0||^3 = ||S||_F * n^-1.5; floor caps
            # lambda_eff at 100x that norm-implied scale under cancellation.
            m3_floor = 1e-2 * float(np.linalg.norm(ms.coskew.ravel())) * n ** -1.5
            l3 = l3 * var0 / max(m3, m3_floor, 1e-30)
        if ms.cokurt is not None and l4:
            m4 = abs(float(np.einsum("ijkl,i,j,k,l->", ms.cokurt, w0, w0, w0, w0)))
            # |K:w0^4| <= ||K||_F * ||w0||^4 = ||K||_F * n^-2; same rationale.
            m4_floor = 1e-2 * float(np.linalg.norm(ms.cokurt.ravel())) * n ** -2.0
            l4 = l4 * var0 / max(m4, m4_floor, 1e-30)
    elif form == "mvsk":
        extra.setdefault("lambda_scale", lambda_scale)
    return Objective(
        form=form,                       # type: ignore[arg-type]
        tickers=ms.tickers,
        cov=ms.cov,
        mu=ms.mu,
        coskew=ms.coskew,
        cokurt=ms.cokurt,
        skew_lambda=l3,
        kurt_lambda=l4,
        risk_aversion=risk_aversion,
        extra=extra,
    )


def term_contributions(obj: Objective, w: np.ndarray) -> dict[str, float]:
    """Per-term magnitudes at ``w`` — the R0.1 diagnostic (logged with solves)."""
    w = np.asarray(w, dtype=float)
    out = {"variance": float(w @ obj.cov @ w), "skew_term": 0.0, "kurt_term": 0.0}
    if obj.coskew is not None and obj.skew_lambda:
        out["skew_term"] = -obj.skew_lambda * float(
            np.einsum("ijk,i,j,k->", obj.coskew, w, w, w))
    if obj.cokurt is not None and obj.kurt_lambda:
        out["kurt_term"] = obj.kurt_lambda * float(
            np.einsum("ijkl,i,j,k,l->", obj.cokurt, w, w, w, w))
    return out


# ---------------------------------------------------------------------------
# Compiler (a): scipy callable + analytic gradient
# ---------------------------------------------------------------------------
def compile_scipy(obj: Objective) -> tuple[Callable[[np.ndarray], float],
                                            Callable[[np.ndarray], np.ndarray]]:
    """Return ``(f, grad)`` for scipy minimizers. All arms minimise ``f``."""
    Sigma = obj.cov
    S = obj.coskew
    K = obj.cokurt
    l3, l4 = obj.skew_lambda, obj.kurt_lambda

    if obj.form == "min_variance":
        def f(w: np.ndarray) -> float:
            return float(w @ Sigma @ w)

        def g(w: np.ndarray) -> np.ndarray:
            return 2.0 * Sigma @ w

    elif obj.form == "max_utility":
        mu = obj.mu if obj.mu is not None else np.zeros(obj.n)
        ra = obj.risk_aversion

        def f(w: np.ndarray) -> float:
            return float(ra * (w @ Sigma @ w) - mu @ w)

        def g(w: np.ndarray) -> np.ndarray:
            return 2.0 * ra * Sigma @ w - mu

    elif obj.form == "mvsk":
        def f(w: np.ndarray) -> float:
            val = float(w @ Sigma @ w)
            if S is not None and l3:
                val -= l3 * float(np.einsum("ijk,i,j,k->", S, w, w, w))
            if K is not None and l4:
                val += l4 * float(np.einsum("ijkl,i,j,k,l->", K, w, w, w, w))
            return val

        def g(w: np.ndarray) -> np.ndarray:
            grad = 2.0 * Sigma @ w
            if S is not None and l3:
                grad -= l3 * 3.0 * np.einsum("ijk,j,k->i", S, w, w)
            if K is not None and l4:
                grad += l4 * 4.0 * np.einsum("ijkl,j,k,l->i", K, w, w, w)
            return grad

    else:
        raise ValueError(
            f"form {obj.form!r} has no direct scipy compilation "
            "(handled by a dedicated solver, e.g. risk_parity / hrp / cvar)"
        )

    return f, g


def compile_target_semivariance(
    obj: Objective,
    returns: np.ndarray,
) -> tuple[Callable[[np.ndarray], float], Callable[[np.ndarray], np.ndarray]]:
    """Compile mean squared below-target return over a scenario panel.

    ``returns`` is a ``T × n`` matrix in the same ticker order as ``obj``.
    The target is a per-scenario portfolio return stored by
    :func:`build_objective`; observations at or above it contribute zero:

    ``mean(max(target - returns @ w, 0) ** 2)``.

    This convex, piecewise-quadratic scenario objective deliberately bypasses
    the moment-polynomial compiler.
    """
    if obj.form != "target_semivariance":
        raise ValueError(
            "target semivariance compilation requires "
            "form='target_semivariance'"
        )
    scenarios = np.asarray(returns, dtype=float)
    if scenarios.ndim != 2:
        raise ValueError(
            "target_semivariance needs a two-dimensional scenario return panel"
        )
    if scenarios.shape[0] == 0:
        raise ValueError(
            "target_semivariance needs at least one return scenario"
        )
    if scenarios.shape[1] != obj.n:
        raise ValueError(
            "target_semivariance scenario columns must match objective assets: "
            f"got {scenarios.shape[1]}, expected {obj.n}"
        )
    if not np.isfinite(scenarios).all():
        raise ValueError(
            "target_semivariance scenario return panel must contain only "
            "finite values"
        )
    target = float(obj.extra.get("target", 0.0))
    if not np.isfinite(target):
        raise ValueError(f"target must be finite, got {target!r}")

    def f(w: np.ndarray) -> float:
        shortfall = np.maximum(target - scenarios @ w, 0.0)
        return float(np.mean(shortfall ** 2))

    def g(w: np.ndarray) -> np.ndarray:
        shortfall = np.maximum(target - scenarios @ w, 0.0)
        return -2.0 * (scenarios.T @ shortfall) / scenarios.shape[0]

    return f, g


def evaluate(obj: Objective, w: np.ndarray) -> float:
    """Evaluate the objective at ``w`` (uniform reporting across solver arms)."""
    if obj.form in ("min_variance", "max_utility", "mvsk"):
        f, _ = compile_scipy(obj)
        return f(np.asarray(w, dtype=float))
    if obj.form == "target_semivariance":
        raise ValueError(
            "target_semivariance evaluation requires its scenario return panel; "
            "use compile_target_semivariance"
        )
    # For forms without a scalar polynomial (risk_parity, hrp, cvar) report the
    # portfolio variance so arms remain loosely comparable.
    w = np.asarray(w, dtype=float)
    return float(w @ obj.cov @ w)


# ---------------------------------------------------------------------------
# Canonical polynomial terms — the single source of truth (invariant 4)
# ---------------------------------------------------------------------------
def _perm_multiplicity(idx: tuple[int, ...]) -> int:
    c = Counter(idx)
    m = factorial(len(idx))
    for v in c.values():
        m //= factorial(v)
    return m


def polynomial_terms(obj: Objective) -> list[tuple[float, tuple[int, ...]]]:
    """The one polynomial, as unique sorted-index terms (invariant 4).

    ``sum(c * prod(w[i] for i in idx))`` over the returned terms equals
    ``compile_scipy(obj)[0](w)`` exactly — property-tested, and consumed by the
    Dirac-3 and Ising encoders so all arms share one coefficient source.
    """
    if obj.form not in ("min_variance", "mvsk"):
        raise ValueError(f"no polynomial form for {obj.form!r}")
    terms: list[tuple[float, tuple[int, ...]]] = []
    n = obj.n
    for idx in combinations_with_replacement(range(n), 2):
        c = float(obj.cov[idx]) * _perm_multiplicity(idx)
        if c:
            terms.append((c, idx))
    if obj.form == "mvsk" and obj.coskew is not None and obj.skew_lambda:
        for idx in combinations_with_replacement(range(n), 3):
            c = -obj.skew_lambda * float(obj.coskew[idx]) * _perm_multiplicity(idx)
            if c:
                terms.append((c, idx))
    if obj.form == "mvsk" and obj.cokurt is not None and obj.kurt_lambda:
        for idx in combinations_with_replacement(range(n), 4):
            c = obj.kurt_lambda * float(obj.cokurt[idx]) * _perm_multiplicity(idx)
            if c:
                terms.append((c, idx))
    return terms


def evaluate_terms(terms, w) -> float:
    w = np.asarray(w, dtype=float)
    return float(sum(c * np.prod(w[list(idx)]) for c, idx in terms))


# ---------------------------------------------------------------------------
# Compiler (b): Dirac-3 continuous-HUBO payload
# ---------------------------------------------------------------------------
def compile_dirac_hubo(obj: Objective, budget: float = 1.0) -> dict:
    """Emit the continuous-HUBO payload Dirac-3 would take *natively*.

    Degree-4 MVSK in ``n`` continuous variables with a native sum-to-R
    constraint (``R = budget``) that *is* the long-only fully-invested budget.
    No binarization, no quadratization, no penalty gadgets. Returned as a
    structured spec (term counts + coefficient references) so it can be
    submitted by :mod:`qlab.solvers.dirac3` when a QCI account is configured.
    """
    if obj.form == "target_semivariance":
        raise ValueError(
            "target_semivariance is scenario-based and has no continuous-HUBO "
            "polynomial payload"
        )
    n = obj.n
    payload: dict = {
        "n_variables": n,
        "variable_type": "continuous",
        "max_degree": (
            4 if (obj.cokurt is not None and obj.kurt_lambda)
            else 3 if (obj.coskew is not None and obj.skew_lambda)
            else 2
        ),
        "sum_constraint": {"variables": list(range(n)), "equals": budget},
        "nonneg": True,
        "terms": {
            "degree2_from_cov": n * n,
            "degree3_from_coskew": (n ** 3) if (obj.coskew is not None
                                               and obj.skew_lambda) else 0,
            "degree4_from_cokurt": (n ** 4) if (obj.cokurt is not None
                                                and obj.kurt_lambda) else 0,
        },
        "lambdas": {"skew": obj.skew_lambda, "kurt": obj.kurt_lambda},
        "objective_hash": obj.content_hash(),
    }
    return payload
