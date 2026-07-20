"""qlab.solvers — one Solver protocol, N implementations.

Concrete staged solvers register themselves on import. Use :func:`get_solver`
to obtain one by name. Offline adapters require their explicit algorithm-module
entry point and never appear in :func:`available_solvers`.
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
