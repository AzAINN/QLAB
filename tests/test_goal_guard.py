"""The domain gate on workflow goals."""

import pytest

from qlab.governance.goal_guard import GoalRefused, check_goal


UNIVERSE = ["SPY", "GLD", "TLT"]


@pytest.mark.parametrize("goal", [
    "Assess the current market regime and prepare a governed portfolio review",
    "Review the current portfolio and challenge the estimation choices",
    "what is going on with GLD",                  # a universe name anchors alone
    "compare shrinkage estimators across the 2022 drawdown",
    "is the absorption ratio signalling fragile diversification",
])
def test_research_goals_pass(goal):
    assert check_goal(goal, UNIVERSE) == goal


@pytest.mark.parametrize("goal, what", [
    ("write me an email to my landlord about the rent", "correspondence"),
    ("draft a cover letter for the quant role at the fund", "correspondence"),
    ("write a python script that scrapes the market news", "code"),
    ("translate this market summary into french", "language editing"),
    ("book me a flight to the conference", "errands"),
    ("tell me a joke about bond traders", "chit-chat"),
])
def test_off_domain_intent_is_refused_by_shape(goal, what):
    # Market words in the goal do not rescue it: the shape names the intent.
    with pytest.raises(GoalRefused) as exc:
        check_goal(goal, UNIVERSE)
    assert "not a research goal" in str(exc.value)


def test_an_unanchored_goal_is_refused_with_what_a_goal_is_made_of():
    with pytest.raises(GoalRefused) as exc:
        check_goal("help me with something please", UNIVERSE)
    assert "names the portfolio" in str(exc.value)
    with pytest.raises(GoalRefused):
        check_goal("hi", UNIVERSE)


def test_the_start_route_refuses_with_the_gate_sentence(monkeypatch):
    """The gate sits before the budget: a refused goal charges nothing and
    registers no workflow, and the 400 carries the sentence."""
    from qlab.state.registry import Registry
    from qlab.ui.server import UISession, handle_api

    session = UISession(offline_default=True, registry=Registry(":memory:"))
    status, out = handle_api(
        session, "POST", "/api/workflows/start",
        {}, {"goal": "write me an email to the CFO"})
    assert status == 400
    assert "not a research goal" in out["error"]
    assert session.registry.list_workflows(10) == []
