"""Server-side guardrails — what makes the server the *referee of facts*.

Four mechanisms (spec "Key implementation notes"; research-plan §2.2):

* **schema validation** — tool I/O is pydantic-typed at the FastMCP boundary.
* **the ``as_of`` tripwire** — no data/strategy call may reference a future
  date; look-ahead is refused, not merely discouraged.
* **constraint checks** — solver inputs/outputs are validated against the
  long-only budget box.
* **a call-budget ledger** — a per-session tool-call counter keeps the lab
  "lab-like" rather than unbounded, and caps agent token/compute burn.

These are deterministic and unarguable; the agent cannot talk its way past them.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from typing import Callable

from qlab.arms import MomentsConfig
from qlab.core.types import MomentSet, Objective
from qlab.solvers.base import Constraints
from qlab.state.registry import Registry


class BudgetExceeded(RuntimeError):
    """Raised when the per-session tool-call budget is spent."""


class CallBudget:
    def __init__(self, max_calls: int = 200,
                 on_charge: Callable[[str], None] | None = None):
        self.max_calls = max_calls
        self.on_charge = on_charge
        self.used = 0
        self.by_tool: dict[str, int] = {}

    def charge(self, tool: str) -> None:
        self.used += 1
        self.by_tool[tool] = self.by_tool.get(tool, 0) + 1
        if self.used > self.max_calls:
            raise BudgetExceeded(
                f"tool-call budget of {self.max_calls} exhausted (last: {tool})")
        if self.on_charge is not None:
            self.on_charge(tool)


def check_as_of(as_of: str | date) -> date:
    """Reject any ``as_of`` in the future. The look-ahead tripwire."""
    d = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of
    if d > date.today():
        raise ValueError(f"as_of {d} is in the future — look-ahead refused")
    return d


def check_constraints(weights, constraints: Constraints | None = None) -> None:
    (constraints or Constraints()).validate(weights)


class LabState:
    """Session-scoped state for the quant-lab server.

    Implements *ref-passing*: heavy objects (MomentSet, Objective) live here,
    keyed by content hash; tools return the hash + a lightweight summary, and
    downstream tools look objects back up by id. Tensors never enter the model.
    """

    def __init__(self, registry: Registry | None = None, max_calls: int = 200,
                 offline: bool = False, seed: int = 7):
        self.registry = registry or Registry()
        self.budget = CallBudget(
            max_calls,
            on_charge=lambda tool: self.registry.record_event(
                "tool_call", {"tool": tool},
            ),
        )
        self.offline = offline
        self.seed = seed
        self.moment_sets: dict[str, MomentSet] = {}
        self.objectives: dict[str, Objective] = {}
        self.moments_cfg = MomentsConfig()

    def put_moment_set(self, ms: MomentSet) -> str:
        h = self.registry.log_moment_set(ms)
        self.moment_sets[h] = ms
        return h

    def get_moment_set(self, h: str) -> MomentSet:
        if h not in self.moment_sets:
            raise KeyError(f"unknown moment_set_id {h!r}; call moments.estimate first")
        return self.moment_sets[h]

    def put_objective(self, obj: Objective) -> str:
        h = self.registry.log_objective(obj)
        self.objectives[h] = obj
        return h

    def get_objective(self, h: str) -> Objective:
        if h not in self.objectives:
            raise KeyError(f"unknown objective_id {h!r}; call objective.build first")
        return self.objectives[h]


@lru_cache(maxsize=1)
def require_fastmcp():
    """Import FastMCP, with a clear message if the extra isn't installed."""
    try:
        from fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "FastMCP is not installed. Install the MCP extra:\n"
            "    pip install 'qlab[mcp]'\n"
            f"(original error: {exc})"
        )
    return FastMCP
