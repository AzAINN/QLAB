"""The one ``Solver`` contract and a small adapter registry.

Invariant (research-plan §2.1): ``qlab.solvers`` owns *one ``Solver`` protocol,
N implementations* and knows nothing about the registry or agents. Every staged
arm — classical multistart, HRP, scenario-CVaR LP, Dirac-3, mock — takes
an :class:`~qlab.core.types.Objective` plus :class:`Constraints` and returns a
uniform :class:`~qlab.core.types.SolveResult`. That parity is what makes the
ablation an apples-to-apples comparison rather than a measurement of encoding
drift.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from qlab.core.types import Objective, SolveResult


@dataclass
class Constraints:
    """Box + budget constraints shared by every solver.

    The defaults are the long-only, fully-invested mandate. ``max_weight`` maps
    to the per-asset cap from ``mandate.yaml``.
    """

    long_only: bool = True
    budget: float = 1.0          # weights sum to this
    min_weight: float = 0.0
    max_weight: float = 1.0

    def bounds(self, n: int) -> list[tuple[float, float]]:
        lo = max(self.min_weight, 0.0 if self.long_only else self.min_weight)
        return [(lo, self.max_weight)] * n

    def validate(self, w: np.ndarray, tol: float = 1e-4) -> None:
        """Referee check: hard constraints are unarguable (invariant 2)."""
        if self.long_only and np.any(w < -tol):
            raise ValueError("long-only violated: negative weight")
        if abs(float(w.sum()) - self.budget) > 1e-2:
            raise ValueError(f"budget violated: sum(w)={w.sum():.4f} != {self.budget}")
        if np.any(w > self.max_weight + tol):
            raise ValueError(f"max-weight cap {self.max_weight} violated")


class Solver(ABC):
    """Abstract solver. ``context`` carries arm-specific extras (returns panel,
    selection cardinality, moment set) without polluting the core signature."""

    name: str = "solver"

    @abstractmethod
    def solve(
        self, objective: Objective, constraints: Constraints, **context: Any
    ) -> SolveResult: ...


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------
_REGISTRY: dict[str, Callable[..., Solver]] = {}
_OFFLINE_SOLVERS = {"qaoa", "qubo_resource_count"}


def register_solver(name: str) -> Callable[[type[Solver]], type[Solver]]:
    def deco(cls: type[Solver]) -> type[Solver]:
        _REGISTRY[name] = cls
        cls.name = name
        return cls
    return deco


def get_solver(name: str, **kwargs: Any) -> Solver:
    """Instantiate a staged implementation, importing optional adapters lazily."""
    if name in _OFFLINE_SOLVERS:
        raise KeyError(
            f"solver {name!r} is offline research; use "
            "qlab.algorithms.offline.get_offline_quantum_solver explicitly"
        )
    if name not in _REGISTRY:
        _import_all_solvers()
    if name not in _REGISTRY:
        raise KeyError(f"unknown solver {name!r}; available: {sorted(_REGISTRY)}")
    return _REGISTRY[name](**kwargs)


def _get_registered_solver(name: str, **kwargs: Any) -> Solver:
    """Instantiate an already registered adapter, including offline research."""
    if name not in _REGISTRY:
        raise KeyError(f"solver {name!r} has not been registered")
    return _REGISTRY[name](**kwargs)


def available_solvers() -> list[str]:
    _import_all_solvers()
    return sorted(name for name in _REGISTRY if name not in _OFFLINE_SOLVERS)


def _import_all_solvers() -> None:
    # importing the modules triggers their @register_solver decorators
    from qlab.solvers import classical, cvar, hrp, mock  # noqa: F401
    try:
        from qlab.solvers import dirac3  # noqa: F401
    except Exception:
        pass


# ---------------------------------------------------------------------------
# shared numerics
# ---------------------------------------------------------------------------
def project_to_simplex(v: np.ndarray, budget: float = 1.0) -> np.ndarray:
    """Euclidean projection of ``v`` onto the long-only budget simplex."""
    v = np.asarray(v, dtype=float)
    n = len(v)
    u = np.sort(v)[::-1]
    cssv = np.cumsum(u) - budget
    rho = np.nonzero(u * np.arange(1, n + 1) > cssv)[0]
    if len(rho) == 0:
        return np.full(n, budget / n)
    rho = rho[-1]
    theta = cssv[rho] / (rho + 1.0)
    return np.maximum(v - theta, 0.0)


def project_capped_simplex(v: np.ndarray, budget: float, cap: float) -> np.ndarray:
    """Project onto ``{w ≥ 0, Σw = budget, w ≤ cap}`` (water-filling).

    Needed so mandate-constrained solves (per-asset cap < budget) stay feasible
    even after the simplex projection concentrates mass.
    """
    v = np.clip(np.asarray(v, dtype=float), 0.0, None)
    n = len(v)
    if cap * n < budget - 1e-9:
        raise ValueError(f"infeasible: cap {cap} × {n} assets < budget {budget}")
    saturated = np.zeros(n, dtype=bool)
    for _ in range(n + 1):
        free = ~saturated
        remaining = budget - cap * saturated.sum()
        s = v[free].sum()
        v[free] = (v[free] * remaining / s) if s > 0 else remaining / free.sum()
        newly = free & (v > cap + 1e-12)
        if not newly.any():
            break
        v[newly] = cap
        saturated |= newly
    return v


def finalize_weights(w: np.ndarray, constraints: "Constraints") -> np.ndarray:
    """Clip, renormalize to budget, and enforce the per-asset cap. Feasible-by-
    construction output that every solver routes through before validation."""
    w = np.clip(np.asarray(w, dtype=float), 0.0, None)
    s = w.sum()
    n = len(w)
    w = (w * constraints.budget / s) if s > 0 else np.full(n, constraints.budget / n)
    if constraints.max_weight < constraints.budget and np.any(w > constraints.max_weight + 1e-9):
        w = project_capped_simplex(w, constraints.budget, constraints.max_weight)
    return w
