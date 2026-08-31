"""The cardinality policy: exact k-of-N selection, then min-variance on the k.

The point of these tests is that ``k`` is a *hard* count, not a penalty that a
solver may trade away, and that the mandate still gets the last word — a policy
that concentrates is exactly the kind that can breach ``max_weight_per_asset``
or ``max_holdings``, so the check runs on the finished target map.
"""

from __future__ import annotations

import inspect
from datetime import date

import numpy as np
import pytest

from qlab.algorithms.cardinal import solve_cardinal_min_variance
from qlab.algorithms.catalog import get_algorithm, require_operational_stage
from qlab.core.selection import MAX_EXACT_ASSETS, select_k_of_n
from qlab.core.types import MomentSet
from qlab.trader.mandate import Mandate, MandateViolation

TICKERS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]


def _moment_set(n: int = 6, seed: int = 11) -> MomentSet:
    """A well-conditioned covariance with genuinely different asset vols."""
    rng = np.random.default_rng(seed)
    loadings = rng.normal(size=(n, 3))
    cov = loadings @ loadings.T + np.diag(np.linspace(0.5, 2.0, n))
    cov = 0.5 * (cov + cov.T) * 1e-4
    return MomentSet(tickers=TICKERS[:n], as_of=date(2024, 6, 28), cov=cov)


def _mandate(**overrides) -> Mandate:
    kwargs = {
        "universe_whitelist": list(TICKERS),
        "max_weight_per_asset": 1.0,
        "paper_capital": 10_000.0,
    }
    kwargs.update(overrides)
    return Mandate(**kwargs)


# ---------------------------------------------------------------------------
# the count is exact
# ---------------------------------------------------------------------------
def test_exactly_k_names_are_funded_and_the_rest_are_exactly_zero():
    ms = _moment_set()
    targets = solve_cardinal_min_variance(ms, 3, _mandate())

    assert set(targets) == set(ms.tickers), "every ticker must be reported"
    funded = [t for t, w in targets.items() if w > 0.0]
    assert len(funded) == 3
    # Not "small" — exactly 0.0. An unselected name must be unambiguously out,
    # so nothing downstream can round a residual into an order.
    for ticker, weight in targets.items():
        if ticker not in funded:
            assert weight == 0.0
    assert sum(targets.values()) == pytest.approx(1.0, abs=1e-6)


def test_the_funded_set_is_exactly_select_k_of_n_s_basket():
    ms = _moment_set()
    expected = select_k_of_n(ms.tickers, 4, covariance=ms.cov).selected

    targets = solve_cardinal_min_variance(ms, 4, _mandate())
    funded = sorted(t for t, w in targets.items() if w > 0.0)
    assert funded == sorted(expected)


def test_the_result_is_deterministic_for_one_moment_set():
    ms = _moment_set()
    first = solve_cardinal_min_variance(ms, 3, _mandate())
    second = solve_cardinal_min_variance(ms, 3, _mandate())
    assert first == second


def test_selection_can_only_see_the_moment_set_at_this_rebalance():
    """A look-ahead guard by signature, not by comment.

    The selection is the step that could cheat: choosing the k names with
    hindsight would make the arm look wonderful. The function takes a moment
    set, a count, and a mandate — there is no snapshot, no price panel and no
    date range to reach a later observation through.
    """
    params = list(inspect.signature(solve_cardinal_min_variance).parameters)
    assert params[:3] == ["ms", "k", "mandate"]
    forbidden = {"snapshot", "prices", "returns", "start", "end", "as_of"}
    assert forbidden.isdisjoint(params)


# ---------------------------------------------------------------------------
# refusals — fail loud
# ---------------------------------------------------------------------------
def test_refuses_k_above_the_exact_enumeration_ceiling():
    ms = _moment_set()
    with pytest.raises(ValueError, match=str(MAX_EXACT_ASSETS)):
        solve_cardinal_min_variance(ms, MAX_EXACT_ASSETS + 1, _mandate())


def test_refuses_k_above_the_universe_size():
    ms = _moment_set(n=6)
    with pytest.raises(ValueError, match="7"):
        solve_cardinal_min_variance(ms, 7, _mandate())


def test_refuses_k_above_the_mandate_holdings_cap():
    ms = _moment_set()
    with pytest.raises(ValueError, match="max_holdings"):
        solve_cardinal_min_variance(ms, 5, _mandate(max_holdings=3))


def test_a_cap_equal_to_k_is_allowed():
    ms = _moment_set()
    targets = solve_cardinal_min_variance(ms, 3, _mandate(max_holdings=3))
    assert sum(1 for w in targets.values() if w > 0.0) == 3


def test_refuses_a_non_integer_k():
    ms = _moment_set()
    with pytest.raises(TypeError):
        solve_cardinal_min_variance(ms, 3.0, _mandate())


# ---------------------------------------------------------------------------
# the mandate still decides
# ---------------------------------------------------------------------------
def test_the_mandate_checks_the_finished_target_map():
    """A name outside the whitelist must stop the plan, not be silently kept."""
    ms = _moment_set()
    narrow = _mandate(universe_whitelist=["AAA", "BBB"])
    with pytest.raises(MandateViolation):
        solve_cardinal_min_variance(ms, 3, narrow)


def test_concentration_respects_the_per_asset_cap():
    """k=2 out of 6 would blow through a 0.40 cap without a bound on the solve."""
    ms = _moment_set()
    targets = solve_cardinal_min_variance(ms, 2, _mandate(max_weight_per_asset=0.6))
    assert max(targets.values()) <= 0.6 + 1e-4


def test_a_missing_mandate_must_be_stated_not_defaulted():
    """Research callers pass None deliberately; the argument is not optional."""
    ms = _moment_set()
    with pytest.raises(TypeError):
        solve_cardinal_min_variance(ms, 3)
    targets = solve_cardinal_min_variance(ms, 3, None)
    assert sum(1 for w in targets.values() if w > 0.0) == 3


# ---------------------------------------------------------------------------
# the staged boundary
# ---------------------------------------------------------------------------
def test_the_catalog_entry_enters_at_research_stage():
    spec = get_algorithm("cardinal_min_variance")
    assert spec.category == "allocation"
    assert spec.stage == "research"
    assert spec.agent_tool is None
    assert spec.agent_usable is False


def test_the_staged_surface_refuses_the_research_entry():
    with pytest.raises(PermissionError, match="research"):
        require_operational_stage("cardinal_min_variance")


# ---------------------------------------------------------------------------
# the ablation arm
# ---------------------------------------------------------------------------
def _spec() -> dict:
    import yaml

    from qlab.paths import workspace_root

    return yaml.safe_load(
        (workspace_root() / "configs/specs/ablation_v1.yaml").read_text())


def test_arm_a6_varies_only_the_cardinality_against_a1():
    spec = _spec()
    arms = {a["id"]: a for a in spec["arms"]}
    a1, a6 = arms["A1"], arms["A6"]
    assert a6["objective"] == a1["objective"]
    assert a6["solver"] == a1["solver"]
    assert set(a6.get("params", {})) == {"cardinality"}
    k = int(a6["params"]["cardinality"])
    # An arm asking for more names than the pinned panel holds would refuse at
    # every rebalance and measure nothing.
    assert 1 <= k < len(spec["data"]["tickers"])


def test_solve_arm_routes_a_cardinality_arm_through_the_cardinal_policy():
    from qlab.arms import Arm, solve_arm
    from qlab.core import data as market

    snapshot = market.snapshot(TICKERS[:6], "2020-12-31",
                               start="2015-01-01", offline=True, seed=7)
    arm = Arm("A6t", "min_variance", "classical", params={"cardinality": 3})
    weights, diag = solve_arm(arm, snapshot)

    values = weights.as_series()
    assert sum(1 for v in values if v > 0.0) == 3
    assert diag["cardinality"] == 3
    assert diag["selected"] == sorted(
        t for t, v in values.items() if v > 0.0)


def test_a_cardinality_arm_must_be_a_min_variance_arm():
    from qlab.arms import Arm, solve_arm
    from qlab.core import data as market

    snapshot = market.snapshot(TICKERS[:6], "2020-12-31",
                               start="2015-01-01", offline=True, seed=7)
    arm = Arm("bad", "mvsk", "classical_multistart", params={"cardinality": 3})
    with pytest.raises(ValueError, match="cardinality"):
        solve_arm(arm, snapshot)
