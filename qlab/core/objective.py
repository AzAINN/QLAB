"""The one true objective, and its three compilers.

Invariant 4 (research-plan §2.2): *the objective is built once as a coefficient
tensor and compiled to (a) a scipy callable, (b) the Qiskit QUBO/Ising encoding,
and (c) the Dirac-3 continuous-HUBO encoding.* Property-tested for agreement, so
any divergence between solver arms is encoding drift, not solver quality.

The **primary objective is risk-only MVSK** — minimize variance, reward
coskewness, penalize cokurtosis, with **no expected-return term** (research-plan
§4). This pre-empts the strongest critique ("your alpha is just your return
forecast") and still fully exercises the higher-order polynomial:

    f(w) = wᵀΣw  −  λ₃ · (S : w w w)  +  λ₄ · (K : w w w w)

subject to the long-only, fully-invested budget ``Σ wᵢ = 1``, ``w ≥ 0``.

The QUBO compiler does not *solve* MVSK — it **counts the resources** required to
put degree-4 MVSK on gate hardware. That count *is* the quantum-architecture
argument (§0.3), made reproducible instead of asserted.
"""

from __future__ import annotations

from math import comb
from typing import Callable

import numpy as np

from qlab.core.types import MomentSet, Objective


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
    """
    extra = dict(extra or {})
    l3, l4 = float(skew_lambda), float(kurt_lambda)
    if form == "mvsk" and lambda_scale == "auto" and (l3 or l4):
        n = ms.n
        w0 = np.full(n, 1.0 / n)
        var0 = float(w0 @ ms.cov @ w0)
        extra.update({"lambda_scale": "auto",
                      "skew_lambda_raw": l3, "kurt_lambda_raw": l4})
        if ms.coskew is not None and l3:
            m3 = abs(float(np.einsum("ijk,i,j,k->", ms.coskew, w0, w0, w0)))
            l3 = l3 * var0 / max(m3, 1e-30)
        if ms.cokurt is not None and l4:
            m4 = abs(float(np.einsum("ijkl,i,j,k,l->", ms.cokurt, w0, w0, w0, w0)))
            l4 = l4 * var0 / max(m4, 1e-30)
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


def evaluate(obj: Objective, w: np.ndarray) -> float:
    """Evaluate the objective at ``w`` (uniform reporting across solver arms)."""
    if obj.form in ("min_variance", "max_utility", "mvsk"):
        f, _ = compile_scipy(obj)
        return f(np.asarray(w, dtype=float))
    # For forms without a scalar polynomial (risk_parity, hrp, cvar) report the
    # portfolio variance so arms remain loosely comparable.
    w = np.asarray(w, dtype=float)
    return float(w @ obj.cov @ w)


# ---------------------------------------------------------------------------
# Compiler (b): Qiskit QUBO/Ising RESOURCE COUNT (Slot Q-C — count, don't run)
# ---------------------------------------------------------------------------
def mvsk_qubo_resource_count(n: int, resolution_bits: int) -> dict[str, int]:
    """Count the resources to put degree-4 MVSK on a gate-model machine.

    Reproduces research-plan §0.3 exactly. To binarize the weights at ``r``
    bits and quadratize the resulting degree-4 pseudo-Boolean polynomial into an
    Ising Hamiltonian:

    * weight vector → ``N = n·r`` binary variables;
    * coskewness has ``C(n+2, 3)`` unique entries, each expanding to ``r³``
      degree-3 monomials;
    * cokurtosis has ``C(n+3, 4)`` unique entries, each expanding to ``r⁴``
      degree-4 monomials;
    * efficient (Rosenberg) quadratization adds one auxiliary per distinct
      binary product: ``C(N, 2) + N`` auxiliaries, each an AND-gadget penalty.

    At ``n=7, r=4`` this returns **434 logical qubits, 406 penalty gadgets** —
    versus **7 continuous variables** on Dirac-3. That single comparison is the
    whole quantum-architecture argument, and it is only credible because the
    encoder was built and the resources counted.
    """
    r = resolution_bits
    N = n * r
    coskew_entries = comb(n + 2, 3)
    cokurt_entries = comb(n + 3, 4)
    deg3_monomials = coskew_entries * (r ** 3)
    deg4_monomials = cokurt_entries * (r ** 4)
    aux_qubits = comb(N, 2) + N
    return {
        "n_assets": n,
        "resolution_bits": r,
        "weight_qubits": N,
        "coskew_entries": coskew_entries,
        "cokurt_entries": cokurt_entries,
        "degree3_monomials": deg3_monomials,
        "degree4_monomials": deg4_monomials,
        "auxiliary_qubits": aux_qubits,
        "penalty_gadgets": aux_qubits,
        "total_logical_qubits": N + aux_qubits,
        "dirac3_continuous_variables": n,     # the structural comparison
        "heron_hardware_qubits": 156,         # for context
    }


# ---------------------------------------------------------------------------
# Compiler (c): Dirac-3 continuous-HUBO payload
# ---------------------------------------------------------------------------
def compile_dirac_hubo(obj: Objective, budget: float = 1.0) -> dict:
    """Emit the continuous-HUBO payload Dirac-3 would take *natively*.

    Degree-4 MVSK in ``n`` continuous variables with a native sum-to-R
    constraint (``R = budget``) that *is* the long-only fully-invested budget.
    No binarization, no quadratization, no penalty gadgets. Returned as a
    structured spec (term counts + coefficient references) so it can be
    submitted by :mod:`qlab.solvers.dirac3` when a QCI account is configured.
    """
    n = obj.n
    payload: dict = {
        "n_variables": n,
        "variable_type": "continuous",
        "max_degree": 4 if (obj.cokurt is not None and obj.kurt_lambda) else 2,
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
