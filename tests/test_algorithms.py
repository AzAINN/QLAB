import pytest

from qlab.algorithms import get_algorithm, list_algorithms, solve_prepared_objective
from qlab.algorithms.offline import mvsk_qubo_resource_count
from qlab.core.objective import build_objective
from qlab.solvers.base import Constraints, available_solvers, get_solver


def test_catalog_separates_operational_research_and_offline_algorithms() -> None:
    stages = {row["stage"] for row in list_algorithms()}
    assert stages == {"operational", "research", "offline"}
    assert all(row["agent_usable"] for row in list_algorithms(stage="operational")
               if row["agent_tool"])
    assert not any(row["agent_usable"] for row in list_algorithms(stage="offline"))


def test_quantum_algorithms_are_offline_and_absent_from_runtime_discovery() -> None:
    quantum = list_algorithms(category="quantum")
    assert quantum
    assert {row["stage"] for row in quantum} == {"offline"}
    assert "qaoa" not in available_solvers()
    assert "qubo_resource_count" not in available_solvers()
    with pytest.raises(KeyError, match="offline research"):
        get_solver("qaoa")


def test_offline_resource_estimator_remains_available_only_by_explicit_import() -> None:
    from math import comb

    n_assets, resolution_bits = 7, 4
    report = mvsk_qubo_resource_count(n_assets, resolution_bits)
    weight_bits = n_assets * resolution_bits
    expected_auxiliaries = comb(weight_bits, 2) + weight_bits
    assert report["weight_qubits"] == weight_bits
    assert report["auxiliary_qubits"] == expected_auxiliaries
    assert report["total_logical_qubits"] == weight_bits + expected_auxiliaries
    assert report["continuous_variables"] == n_assets


def test_catalog_runs_only_staged_prepared_objective_algorithms(moment_set) -> None:
    objective = build_objective("min_variance", moment_set)
    result = solve_prepared_objective("hrp", objective, Constraints())
    assert result.solver == "hrp"

    mvsk = build_objective("mvsk", moment_set, skew_lambda=0.5, kurt_lambda=0.5)
    with pytest.raises(PermissionError, match="stage='research'"):
        solve_prepared_objective("mvsk_multistart", mvsk, Constraints())

    with pytest.raises(PermissionError, match="stage='offline'"):
        solve_prepared_objective("qaoa_discretized_mv", mvsk, Constraints())


def test_mvsk_is_research_not_the_operational_policy() -> None:
    from qlab.algorithms import get_operational_policy

    mvsk = get_algorithm("mvsk_multistart")
    assert mvsk.stage == "research"
    assert mvsk.agent_usable is False
    assert get_operational_policy("hrp").algorithm_id == "hrp"


def test_operational_objective_forms_are_exactly_the_solvable_ones(moment_set) -> None:
    """Every form the helper reports must have an operational solver, and every
    excluded portfolio form (mvsk) must not — this is the boundary objective.build
    enforces so a research-only objective never reaches the optimizer.
    """
    from qlab.algorithms.catalog import operational_objective_forms

    forms = operational_objective_forms()
    assert forms == {"min_variance", "max_utility"}

    operational = list_algorithms(stage="operational")
    for form in forms:
        # At least one operational, prepared-objective algorithm can consume it.
        assert any(
            spec["prepared_objective"] and form in spec["objective_forms"]
            for spec in operational
        )

    # mvsk is deliberately absent: no operational algorithm declares it, so a
    # built mvsk objective is unsolvable on the staged surface.
    assert "mvsk" not in forms
    mvsk = build_objective("mvsk", moment_set, skew_lambda=0.5, kurt_lambda=0.5)
    with pytest.raises(ValueError, match="supports"):
        solve_prepared_objective("min_variance", mvsk, Constraints())


def test_target_semivariance_is_research_and_refused_by_staged_solve(
    moment_set,
) -> None:
    spec = get_algorithm("target_semivariance")
    assert spec.category == "optimization"
    assert spec.stage == "research"
    assert spec.solver == "target_semivariance"
    assert spec.prepared_objective is False
    assert spec.agent_usable is False

    objective = build_objective(
        "target_semivariance", moment_set, target=0.0
    )
    with pytest.raises(PermissionError, match="stage='research'"):
        solve_prepared_objective(
            "target_semivariance", objective, Constraints()
        )


def test_exact_selection_is_visible_but_not_staged_solveable(moment_set) -> None:
    selection = get_algorithm("selection_k_of_n")
    assert selection.category == "selection"
    assert selection.stage == "research"
    assert selection.solver is None
    assert selection.prepared_objective is False
    assert selection.agent_usable is False

    objective = build_objective("min_variance", moment_set)
    with pytest.raises(PermissionError, match="stage='research'"):
        solve_prepared_objective(
            "selection_k_of_n", objective, Constraints()
        )


def test_catalog_rejects_objective_algorithm_mismatch(moment_set) -> None:
    objective = build_objective("mvsk", moment_set, skew_lambda=0.5, kurt_lambda=0.5)
    with pytest.raises(ValueError, match="supports"):
        solve_prepared_objective("min_variance", objective, Constraints())


def test_catalog_lookup_has_clear_unknown_error() -> None:
    with pytest.raises(KeyError, match="unknown algorithm"):
        get_algorithm("does-not-exist")


def test_cli_has_no_staged_qaoa_switch() -> None:
    from qlab.autopilot.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["run-once", "--qaoa"])
