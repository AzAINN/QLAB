"""One current proposal: the newest checked plan, and only that one.

A desk that asks two questions at once has no answer. A newer checked plan
supersedes an older pending one — the older approval is invalidated with the
reason that names its successor, the chat says so once, and
``GET /api/desk/proposal`` serves the single thing the desk wants answered.
"""

from __future__ import annotations

import pytest

from qlab.governance.proposal import current_proposal, supersede
from qlab.state.registry import Registry
from qlab.ui.server import UISession, handle_api


@pytest.fixture
def session():
    # isolated in-memory paper book per test
    return UISession(offline_default=True, registry=Registry(":memory:"))


def _checked_plan(session, tilt: float = 0.0) -> str:
    """A referee-PASSed persisted checked plan — the approval path's own seam.

    The same construction ``tests/test_ui.py`` uses for the approval tests:
    a logged decision, a PASS bound to those exact targets, then the owner's
    ``rebalance_preview``. Plans are content-addressed, so ``tilt`` perturbs
    the targets when a test needs a genuinely different plan.
    """
    from datetime import date

    from qlab.core.types import Decision

    tickers = session.mandate.universe_whitelist
    even = 1.0 / len(tickers)
    targets = {ticker: even for ticker in tickers}
    if tilt:
        first, last = tickers[0], tickers[-1]
        targets[first] = even + tilt
        targets[last] = even - tilt
    decision_id = session.registry.log_decision(Decision(
        as_of=date.today(), kind="rebalance_gate",
        choice={"targets": targets}, rationale="configured HRP policy",
    ))
    session.registry.log_verdict(
        decision_id, "PASS", ["within mandate"], source="referee-agent",
        targets=targets)
    _, preview = handle_api(
        session, "POST", "/api/rebalance_preview", {},
        {"offline": True, "decision_id": decision_id, "targets": targets})
    assert preview["accepted"] is True
    return preview["plan_id"]


def _states(session) -> dict:
    return {row["plan_id"]: row["status"]
            for row in session.registry.list_approval_requests(50)}


def _said(session) -> str:
    return " ".join(
        str(event.get("payload", {}).get("text"))
        for event in session.registry.read_events_of_kind("atlas_message", 50))


def test_no_plan_no_proposal():
    reg = Registry(":memory:")
    try:
        assert current_proposal(reg) is None
    finally:
        reg.close()


def test_the_newest_checked_plan_is_the_proposal_and_older_pending_are_superseded(
        session):
    older = _checked_plan(session)
    session.announce_desk_work(True, [])          # opens approval for older
    newer = _checked_plan(session, tilt=0.02)
    session.announce_desk_work(True, [])          # opens newer, supersedes older

    proposal = current_proposal(session.registry)
    assert proposal["plan_id"] == newer
    assert proposal["approval_state"] == "pending"
    assert proposal["superseded"] == [older]
    assert proposal["targets"] and proposal["targets_hash"]
    assert proposal["created_at"]

    states = _states(session)
    assert states[older] == "invalidated"
    assert states[newer] == "pending"

    invalidated = [row for row in session.registry.list_approval_requests(50)
                   if row["plan_id"] == older]
    assert invalidated[0]["invalidated_reason"] == f"superseded by {newer}"
    assert "superseded" in _said(session)


def test_a_superseded_plan_is_announced_once(session):
    older = _checked_plan(session)
    session.announce_desk_work(True, [])
    newer = _checked_plan(session, tilt=0.02)
    session.announce_desk_work(True, [])

    def supersession_lines() -> list[str]:
        return [line for line in _said(session).split("⚑")
                if "supersedes" in line]

    assert len(supersession_lines()) == 1
    # A plan superseded once must not be announced again on the next tick, and
    # a terminal invalidation must never be re-driven.
    session.announce_desk_work(True, [])
    session.announce_desk_work(True, [])
    assert len(supersession_lines()) == 1
    assert _states(session)[older] == "invalidated"
    assert current_proposal(session.registry)["plan_id"] == newer


def test_a_plan_whose_request_is_gone_is_not_the_proposal(session):
    older = _checked_plan(session)
    session.announce_desk_work(True, [])
    newer = _checked_plan(session, tilt=0.02)
    session.announce_desk_work(True, [])

    approval = [row for row in session.registry.list_approval_requests(50)
                if row["plan_id"] == newer][0]["approval_id"]
    session.registry.transition_approval(
        approval, "invalidated", invalidated_reason="book moved")

    # The older plan's request was invalidated by the supersession, so with the
    # newest one gone too the desk is asking nothing — not falling back to a
    # question it already withdrew.
    assert current_proposal(session.registry) is None
    assert older  # named, so the supersession is not silent


def test_an_approved_request_is_still_the_proposal(session):
    plan_id = _checked_plan(session)
    session.announce_desk_work(True, [])
    approval = [row for row in session.registry.list_approval_requests(50)
                if row["plan_id"] == plan_id][0]["approval_id"]
    handle_api(session, "POST", f"/api/approvals/{approval}/approve", {}, {})

    proposal = current_proposal(session.registry)
    assert proposal["plan_id"] == plan_id
    assert proposal["approval_id"] == approval
    assert proposal["approval_state"] == "approved"
    assert proposal["superseded"] == []


def test_the_referee_verdict_for_the_plans_hash_is_included(session):
    plan_id = _checked_plan(session)
    session.announce_desk_work(True, [])
    proposal = current_proposal(session.registry)
    referee = proposal["referee"]
    assert referee is not None
    assert referee["verdict"] == "PASS"
    # Bound to this plan's exact targets, never merely to its decision.
    assert referee["targets_hash"] == proposal["targets_hash"]


def test_supersede_names_the_keeper_and_leaves_the_keeper_alone(session):
    older = _checked_plan(session)
    session.announce_desk_work(True, [])
    newer = _checked_plan(session, tilt=0.02)
    session.announce_desk_work(True, [])

    # Idempotent: the invalidation already happened, so a second call has
    # nothing to name and reports nothing.
    assert supersede(session.registry, newer) == []
    assert _states(session)[newer] == "pending"

    # And it never invalidates the plan it was told to keep.
    assert supersede(session.registry, older) == [newer]
    assert _states(session)[newer] == "invalidated"


def test_the_proposal_route_serves_the_same_object(session):
    status, out = handle_api(session, "GET", "/api/desk/proposal", {}, {})
    assert status == 200 and out["proposal"] is None

    plan_id = _checked_plan(session)
    session.announce_desk_work(True, [])
    status, out = handle_api(session, "GET", "/api/desk/proposal", {}, {})
    assert status == 200
    assert out["proposal"] == current_proposal(session.registry)
    assert out["proposal"]["plan_id"] == plan_id
