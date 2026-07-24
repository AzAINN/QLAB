"""Categorized algorithm catalog and its staged-execution boundary."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from qlab.core.types import Objective, SolveResult
from qlab.solvers.base import Constraints, get_solver

Stage = Literal["operational", "research", "offline"]


@dataclass(frozen=True)
class AlgorithmSpec:
    id: str
    label: str
    category: str
    stage: Stage
    objective_forms: tuple[str, ...]
    solver: str | None
    agent_tool: str | None
    description: str
    requires: str | None = None
    prepared_objective: bool = False

    @property
    def agent_usable(self) -> bool:
        return self.stage == "operational" and self.agent_tool is not None

    def to_dict(self) -> dict:
        out = asdict(self)
        out["objective_forms"] = list(self.objective_forms)
        out["agent_usable"] = self.agent_usable
        return out


_ALGORITHMS = (
    AlgorithmSpec(
        "equal_weight", "Equal weight", "benchmark", "operational",
        ("equal_weight",), None, "backtest.run",
        "Deterministic 1/N benchmark with no estimated parameters.",
    ),
    AlgorithmSpec(
        "sixty_forty", "60/40", "benchmark", "operational",
        ("sixty_forty",), None, "backtest.run",
        "Configured institutional stock/bond benchmark.",
    ),
    AlgorithmSpec(
        "hrp", "Hierarchical risk parity", "allocation", "operational",
        ("min_variance",), "hrp", "algorithms.solve",
        "Correlation clustering followed by recursive risk allocation.",
        prepared_objective=True,
    ),
    AlgorithmSpec(
        "risk_parity", "Equal risk contribution", "allocation", "operational",
        ("min_variance",), "risk_parity", "algorithms.solve",
        "Long-only equal-risk-contribution portfolio.",
        prepared_objective=True,
    ),
    AlgorithmSpec(
        "min_variance", "Minimum variance", "optimization", "operational",
        ("min_variance", "max_utility"), "classical", "algorithms.solve",
        "Single-start constrained SLSQP with an optional convex fast path.",
        prepared_objective=True,
    ),
    AlgorithmSpec(
        "mvsk_multistart", "MVSK multistart", "optimization", "research",
        ("mvsk",), "classical_multistart", None,
        "Higher-moment hypothesis arm; retained for honest ablation, not deployment.",
        prepared_objective=True,
    ),
    AlgorithmSpec(
        "scenario_cvar", "Scenario CVaR", "optimization", "operational",
        ("scenario_cvar",), "cvar_lp", "backtest.run",
        "Rockafellar-Uryasev linear program over the historical scenario panel.",
    ),
    AlgorithmSpec(
        "target_semivariance", "Target semivariance", "optimization", "research",
        ("target_semivariance",), "target_semivariance", None,
        "Mean squared below-target return over the historical scenario panel.",
        prepared_objective=False,
    ),
    AlgorithmSpec(
        "selection_k_of_n", "Exact k-of-N selection", "selection", "research",
        ("selection_qubo",), None, "selection.run",
        "Exact classical relevance/redundancy enumeration for N<=25; a "
        "research-only candidate-universe gate, not an allocation or paper policy.",
    ),
    AlgorithmSpec(
        "stock_factor_covariance", "Stock factor covariance", "estimation",
        "research", ("factor_covariance",), None, None,
        "Research-only estimation building block for a future stock sleeve; "
        "it is not an allocation or paper-trading path.",
    ),
    AlgorithmSpec(
        "entropy_pooling_views", "Entropy-pooling risk views", "estimation",
        "research", ("min_variance", "mvsk"), None, None,
        "Sequential minimum-relative-entropy scenario reweighting for bounded "
        "vol/correlation/tail views with per-asset means pinned — risk views "
        "never move expected returns. KL-budgeted; research stage.",
    ),
    AlgorithmSpec(
        "vol_prediction_ridge", "Realized-volatility ridge baseline",
        "prediction", "research", ("risk_forecast",), None,
        "research.predict_vol",
        "Purged walk-forward baseline for next-21-day equal-weight portfolio "
        "realized volatility. It predicts risk only and requires the IC "
        "admission gate; it never estimates returns or enters paper execution.",
    ),
    AlgorithmSpec(
        "regime_min_variance", "Regime-conditioned minimum variance", "allocation",
        "research", ("min_variance",), "classical", None,
        "Covariance-only research arm conditioned by the deterministic regime signal.",
    ),
    AlgorithmSpec(
        "mvsk_vol_target", "Vol-targeted MVSK", "overlay", "research",
        ("mvsk",), "classical_multistart", None,
        "Research-only de-risking overlay; it does not satisfy the live mandate.",
    ),
    AlgorithmSpec(
        "dirac3_mvsk", "Dirac-3 MVSK", "external_solver", "research",
        ("mvsk",), "dirac3", None,
        "Optional external continuous-HUBO adapter; unavailable without QCI credentials.",
        requires="QCI_API_URL and QCI_API_TOKEN",
    ),
    AlgorithmSpec(
        "qaoa_selection", "QAOA selection QUBO", "quantum", "offline",
        ("selection_qubo",), "qaoa", None,
        "Aer-only research implementation retained for isolated experiments, not runtime use.",
        requires="qlab[offline-quantum]",
    ),
    AlgorithmSpec(
        "qaoa_discretized_mv", "QAOA discretized minimum variance", "quantum", "offline",
        ("discretized_mv",), "qaoa", None,
        "Aer-only discretized-weight experiment retained outside the staged desk.",
        requires="qlab[offline-quantum]",
    ),
    AlgorithmSpec(
        "mvsk_ising_resource_estimator", "MVSK Ising resource estimator", "quantum",
        "offline", ("mvsk",), "qubo_resource_count", None,
        "Constructed encoding experiment retained for offline architecture research only.",
    ),
)
_BY_ID = {spec.id: spec for spec in _ALGORITHMS}


def list_algorithms(
    *, category: str | None = None, stage: Stage | None = None
) -> list[dict]:
    """Return stable, JSON-ready catalog rows filtered by category or stage."""
    rows = _ALGORITHMS
    if category:
        rows = tuple(spec for spec in rows if spec.category == category)
    if stage:
        rows = tuple(spec for spec in rows if spec.stage == stage)
    return [spec.to_dict() for spec in rows]


def get_algorithm(algorithm_id: str) -> AlgorithmSpec:
    try:
        return _BY_ID[algorithm_id]
    except KeyError as exc:
        raise KeyError(
            f"unknown algorithm {algorithm_id!r}; available: {sorted(_BY_ID)}"
        ) from exc


def operational_solver_names() -> set[str]:
    return {
        spec.solver for spec in _ALGORITHMS
        if spec.stage == "operational" and spec.solver is not None
    }


def operational_algorithm_for_solver(
    solver: str, objective_form: str
) -> AlgorithmSpec:
    """Resolve a legacy solver name to its staged catalog entry."""
    matches = [
        spec for spec in _ALGORITHMS
        if spec.stage == "operational"
        and spec.prepared_objective
        and spec.solver == solver
        and objective_form in spec.objective_forms
    ]
    if not matches:
        raise PermissionError(
            f"solver {solver!r} for {objective_form!r} is not available on the "
            "staged agent surface"
        )
    return matches[0]


def solve_prepared_objective(
    algorithm_id: str,
    objective: Objective,
    constraints: Constraints,
) -> SolveResult:
    """Run a cataloged operational method against a prepared objective.

    Research and offline entries are deliberately non-runnable through this
    function. That makes the same boundary apply to the MCP server, TUI, and
    any Claude Code agent using the catalog.
    """
    spec = get_algorithm(algorithm_id)
    if spec.stage != "operational":
        raise PermissionError(
            f"algorithm {algorithm_id!r} is stage={spec.stage!r} and is not "
            "runnable through the staged agent surface"
        )
    if not spec.prepared_objective or not spec.solver:
        raise PermissionError(
            f"algorithm {algorithm_id!r} does not take a prepared objective; "
            f"it is exercised through its declared tool {spec.agent_tool!r}"
        )
    if objective.form not in spec.objective_forms:
        raise ValueError(
            f"algorithm {algorithm_id!r} supports {spec.objective_forms}, "
            f"not objective form {objective.form!r}"
        )
    return get_solver(spec.solver).solve(objective, constraints)
