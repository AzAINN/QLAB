"""The cardinality policy: exact k-of-N selection, then min-variance on the k.

Two steps that are deliberately *not* one optimization. A cardinality penalty
folded into a continuous objective is a knob a solver can trade against risk;
``select_k_of_n`` enumerates every ``C(N, k)`` basket instead, so the count is a
fact about the answer rather than a preference expressed in it. The chosen
names then go through the *same* minimum-variance path the ``min_variance``
catalog entry runs — nothing about the allocation math is new here, which is
what makes an A6-vs-A1 comparison read as "cardinality" and nothing else.

Everything outside the basket is exactly ``0.0``, not merely small: the trader
counts a holding at ``w > 1e-4`` and a residual that rounds into an order would
make the count a lie.

Research stage. The catalog entry ``cardinal_min_variance`` carries no
``agent_tool``, so no agent can reach this through the staged surface; the
ablation runner and the tests are its only callers until evidence promotes it.
"""

from __future__ import annotations

import numpy as np

from qlab.algorithms.catalog import solve_prepared_objective
from qlab.core.objective import build_objective
from qlab.core.selection import MAX_EXACT_ASSETS, select_k_of_n
from qlab.core.types import MomentSet
from qlab.solvers.base import Constraints

# The catalog entry whose solver does the allocation. Naming it here rather
# than naming a solver keeps this policy pinned to whatever `min_variance`
# actually runs — if that entry changes solver, A6 changes with A1.
_ALLOCATION_ALGORITHM = "min_variance"

# The threshold `Mandate.check_targets` and the trader both count a holding at.
# The policy measures its own delivered count the same way, so "k names" means
# the same thing here as it does everywhere downstream.
HOLDING_TOLERANCE = 1e-4


def solve_cardinal_min_variance(
    ms: MomentSet,
    k: int,
    mandate,
    *,
    constraints: Constraints | None = None,
) -> dict[str, float]:
    """Select exactly ``k`` names, then minimize variance over just those.

    ``mandate`` is positional and may be ``None`` — a research backtest has no
    mandate to check against — but it is never defaulted: a caller that holds a
    mandate and forgets to pass it would get an unchecked plan, so the choice
    has to be stated. When a mandate is supplied, the finished target map goes
    through :meth:`Mandate.check_targets`, so ``max_holdings`` and every other
    limit apply to the same numbers the trader would receive.
    """
    if isinstance(k, bool) or not isinstance(k, (int, np.integer)):
        raise TypeError(f"k must be an integer; got {type(k).__name__}")
    k = int(k)
    tickers = list(ms.tickers)
    n = len(tickers)
    if k < 1:
        raise ValueError(f"k must be at least 1; got k={k}")
    if k > MAX_EXACT_ASSETS:
        raise ValueError(
            f"exact k-of-N selection enumerates baskets, so k must be "
            f"<= {MAX_EXACT_ASSETS}; got k={k}"
        )
    if k > n:
        raise ValueError(
            f"cannot hold {k} of {n} names; k must not exceed the moment set's "
            f"universe"
        )
    cap = getattr(mandate, "max_holdings", None) if mandate is not None else None
    if cap is not None and k > int(cap):
        raise ValueError(
            f"cardinality k={k} exceeds the mandate max_holdings cap of "
            f"{int(cap)}; a plan that cannot be approved must not be solved"
        )

    selection = select_k_of_n(tickers, k, covariance=np.asarray(ms.cov, dtype=float))
    selected = list(selection.selected)
    index = [tickers.index(name) for name in selected]

    sub = MomentSet(
        tickers=selected,
        as_of=ms.as_of,
        cov=np.asarray(ms.cov, dtype=float)[np.ix_(index, index)],
    )
    objective = build_objective("min_variance", sub)
    if constraints is None:
        constraints = _constraints_for(mandate, k)
    result = solve_prepared_objective(_ALLOCATION_ALGORITHM, objective, constraints)

    sub_weights = dict(
        zip(result.weights.tickers, (float(v) for v in result.weights.values),
            strict=True)
    )
    targets = {ticker: sub_weights.get(ticker, 0.0) for ticker in tickers}

    # Exactly k is a claim about the DELIVERED plan, not just about the basket.
    # Long-only min-variance is free to park a selected name on its lower bound,
    # and a plan that holds k-1 names while the policy reports k is the "count
    # is a lie" failure this module exists to prevent — `max_holdings` and the
    # trader would both count something the policy never said. Refuse loudly:
    # the caller has to choose a different k or a different bound, and only the
    # caller can decide which.
    delivered = sum(1 for weight in targets.values()
                    if weight > HOLDING_TOLERANCE)
    if delivered != k:
        raise ValueError(
            f"cardinality policy selected {k} names but the long-only "
            f"minimum-variance solve funded {delivered} of them above the "
            f"{HOLDING_TOLERANCE:g} holding threshold; a plan that delivers "
            f"a different count than it claims is not a k-of-N plan"
        )

    if mandate is not None:
        mandate.check_targets(targets)
    return targets


def _constraints_for(mandate, k: int) -> Constraints:
    """Box constraints that already respect the mandate's per-asset bounds.

    Concentrating into ``k`` names is precisely the move that pushes a weight
    through ``max_weight_per_asset``. Bounding the solve is not a substitute for
    ``check_targets`` — that still runs — it just means the honest answer under
    the cap is found instead of a violation being reported.

    Feasibility is checked here rather than left to the solver: ``k`` names
    capped below ``1/k`` cannot sum to one however they are allocated, and the
    solver would report only "budget violated", which says nothing about which
    of the two numbers to change.
    """
    if mandate is None:
        return Constraints()
    max_weight = float(getattr(mandate, "max_weight_per_asset", 1.0))
    min_weight = float(getattr(mandate, "min_weight_per_asset", 0.0))
    if k * max_weight < 1.0 - HOLDING_TOLERANCE:
        raise ValueError(
            f"k={k} names capped at max_weight_per_asset={max_weight:g} can "
            f"hold at most {k * max_weight:g} of the budget; raise k or the cap"
        )
    if k * min_weight > 1.0 + HOLDING_TOLERANCE:
        raise ValueError(
            f"k={k} names floored at min_weight_per_asset={min_weight:g} "
            f"require {k * min_weight:g} of the budget; lower k or the floor"
        )
    return Constraints(
        long_only=bool(getattr(mandate, "long_only", True)),
        min_weight=min_weight,
        max_weight=max_weight,
    )
