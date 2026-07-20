"""Governed algorithm discovery for operators and AI agents.

Implementations remain in :mod:`qlab.solvers`; this package is the deployment
catalog that says which methods are operational, research-only, or offline.
"""

from qlab.algorithms.catalog import (
    AlgorithmSpec,
    get_algorithm,
    list_algorithms,
    solve_prepared_objective,
)
from qlab.algorithms.policy import (
    OperationalPolicy,
    get_operational_policy,
    list_operational_policies,
)

__all__ = [
    "AlgorithmSpec",
    "get_algorithm",
    "list_algorithms",
    "solve_prepared_objective",
    "OperationalPolicy",
    "get_operational_policy",
    "list_operational_policies",
]
