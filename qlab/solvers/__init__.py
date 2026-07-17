"""qlab.solvers — one Solver protocol, N implementations.

Concrete solvers register themselves on import. Use :func:`get_solver` to
obtain one by name; heavy/optional backends (Qiskit QAOA, Dirac-3) are imported
lazily so the light core never has to pay for them.
"""

from qlab.solvers.base import (
    Constraints,
    Solver,
    available_solvers,
    get_solver,
    project_to_simplex,
    register_solver,
)

__all__ = [
    "Constraints",
    "Solver",
    "available_solvers",
    "get_solver",
    "project_to_simplex",
    "register_solver",
]
