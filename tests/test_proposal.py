"""One current proposal: the newest checked plan, and only that one.

A desk that asks two questions at once has no answer. A newer checked plan
supersedes an older pending one — the older approval is invalidated with the
reason that names its successor, the chat says so once, and
``GET /api/desk/proposal`` serves the single thing the desk wants answered.
"""

from __future__ import annotations

import pytest

from qlab.governance.proposal import (
    current_proposal,
    live_requests,
    supersede,
)
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
    # The older one is not merely gone: the record says what took its place.
    withdrawn = [row for row in session.registry.list_approval_requests(50)
                 if row["plan_id"] == older][0]
    assert withdrawn["invalidated_reason"] == f"superseded by {newer}"


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


def test_an_older_approved_but_unbooked_request_is_superseded_too(session):
    """A plan approved and never booked is exactly the stale allocation the
    operator must not be shown beside a newer one."""
    older = _checked_plan(session)
    session.announce_desk_work(True, [])
    approval = [row for row in session.registry.list_approval_requests(50)
                if row["plan_id"] == older][0]["approval_id"]
    handle_api(session, "POST", f"/api/approvals/{approval}/approve", {}, {})
    assert _states(session)[older] == "approved"

    newer = _checked_plan(session, tilt=0.02)
    out = session.announce_desk_work(True, [])

    assert out["superseded"] == [older]
    assert _states(session)[older] == "invalidated"
    row = [r for r in session.registry.list_approval_requests(50)
           if r["plan_id"] == older][0]
    assert row["invalidated_reason"] == f"superseded by {newer}"

    proposal = current_proposal(session.registry)
    assert proposal["plan_id"] == newer
    assert proposal["superseded"] == [older]
    # The chat names the state it withdrew: an approved request going away is
    # a bigger fact than a pending one going away, and must not read the same.
    assert f"{older[:8]} (approved, unbooked)" in _said(session)


def test_a_consumed_request_is_never_superseded(session):
    """Terminal rows are untouched: a booked plan is history, not a question."""
    plan_id = _checked_plan(session)
    session.announce_desk_work(True, [])
    approval = [row for row in session.registry.list_approval_requests(50)
                if row["plan_id"] == plan_id][0]["approval_id"]
    handle_api(session, "POST", f"/api/approvals/{approval}/approve", {}, {})
    session.registry.transition_approval(
        approval, "consumed", consumed_at="2026-08-31T00:00:00+00:00")

    newer = _checked_plan(session, tilt=0.02)
    out = session.announce_desk_work(True, [])
    assert out["superseded"] == []
    assert _states(session)[plan_id] == "consumed"
    assert current_proposal(session.registry)["plan_id"] == newer


def test_a_partial_supersede_still_announces_what_it_withdrew(session,
                                                              monkeypatch):
    """A row that refuses to move must not swallow the ones that already did.

    The first version built its whole announcement from a single return value,
    so a raise on the second row discarded the first row's *completed*
    invalidation: an approval was dead in the registry and the chat never said
    so — the exact silent withdrawal this task exists to prevent.
    """
    for tilt in (0.0, 0.01, 0.02):
        _checked_plan(session, tilt=tilt)

    real = session.registry.transition_approval
    calls = {"n": 0}

    def flaky(approval_id, status, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("duckdb write failed")
        return real(approval_id, status, **kwargs)

    monkeypatch.setattr(session.registry, "transition_approval", flaky)
    out = session.announce_desk_work(True, [])

    assert len(out["approvals_opened"]) == 3
    keeper = current_proposal(session.registry)["plan_id"]
    assert len(out["superseded"]) == 1
    assert len(out["supersede_failures"]) == 1
    withdrawn = out["superseded"][0]
    stuck = out["supersede_failures"][0]["plan_id"]
    assert {withdrawn, stuck} == {
        plan for plan in _states(session) if plan != keeper}

    states = _states(session)
    assert states[withdrawn] == "invalidated"
    assert states[stuck] == "pending"

    said = _said(session)
    assert f"supersedes {withdrawn[:8]}" in said
    # And the one it could not withdraw is named as such — never silently.
    assert f"could not be withdrawn" in said and stuck[:8] in said


def test_a_proposal_outside_the_plan_window_is_still_served_and_superseded(
        session):
    """Liveness drives the search, not a plan-table window.

    Scanning the newest N plans meant a busy research desk pushed a live
    approved request out of view: the route said "no proposal" while the
    approval stayed bookable, and the tick — which only superseded when it
    found a proposal — never withdrew it either.
    """
    older = _checked_plan(session)
    session.announce_desk_work(True, [])
    approval = [row for row in session.registry.list_approval_requests(50)
                if row["plan_id"] == older][0]["approval_id"]
    handle_api(session, "POST", f"/api/approvals/{approval}/approve", {}, {})

    # Sixty plans that were never checked — noise the desk is not asking about.
    for i in range(60):
        session.registry.create_plan(
            f"refused-{i:03d}", "dec-noise", {"ACWI": 1.0}, {"n_legs": 1})
        session.registry.set_plan_state(f"refused-{i:03d}", "refused")

    _, payload = handle_api(session, "GET", "/api/desk/proposal", {}, {})
    assert payload["proposal"]["plan_id"] == older
    assert payload["proposal"]["approval_state"] == "approved"

    newer = _checked_plan(session, tilt=0.02)
    out = session.announce_desk_work(True, [])
    assert out["superseded"] == [older]
    assert _states(session)[older] == "invalidated"
    assert current_proposal(session.registry)["plan_id"] == newer


def test_a_superseded_approval_cannot_book(session):
    """The governance consequence, stated as a test: withdrawn means unbookable."""
    older = _checked_plan(session)
    session.announce_desk_work(True, [])
    approval = [row for row in session.registry.list_approval_requests(50)
                if row["plan_id"] == older][0]["approval_id"]
    handle_api(session, "POST", f"/api/approvals/{approval}/approve", {}, {})
    _checked_plan(session, tilt=0.02)
    session.announce_desk_work(True, [])

    status, result = handle_api(
        session, "POST", f"/api/plans/{older}/execute", {},
        {"offline": True, "approval_id": approval, "human_confirmed": True})
    assert status == 200
    assert result["executed"] is False
    assert result["blocked_by"] == "approval"


def test_an_expired_request_is_not_the_proposal(session):
    """Read-only expiry: a lapsed request is not an open question, and the
    route must not have to write to notice that."""
    plan_id = _checked_plan(session)
    session.announce_desk_work(True, [])
    session.registry.con.execute(
        "UPDATE approval_requests SET expires_at = ? WHERE plan_id = ?",
        ["2000-01-01T00:00:00+00:00", plan_id])

    assert current_proposal(session.registry) is None
    # The row itself is untouched — sweeping is the owner's job, not the read's.
    assert _states(session)[plan_id] == "pending"


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
    # nothing to name and reports nothing — and nothing failed.
    assert supersede(session.registry, newer) == ([], [])
    assert _states(session)[newer] == "pending"

    # And it never invalidates the plan it was told to keep.
    assert supersede(session.registry, older) == ([newer], [])
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


def test_a_live_request_on_a_plan_no_longer_checked_is_withdrawn(session):
    """An orphan has no keeper, so supersession never reaches it.

    Supersede runs only when there IS a current proposal. A desk whose only
    live request sits on a plan that was refused after the approval opened has
    none — so the request stayed live, bookable, with nothing on screen asking
    about it.
    """
    plan_id = _checked_plan(session)
    session.announce_desk_work(True, [])
    approval_id = next(iter(live_requests(session.registry).values()))["approval_id"]
    handle_api(session, "POST", f"/api/approvals/{approval_id}/approve", {}, {})
    # The plan is refused after the human approved it.
    session.registry.set_plan_state(plan_id, "refused")

    session.announce_desk_work(True, [])

    assert session.registry.get_approval_request(approval_id)["status"] == (
        "invalidated")
    assert session.registry.get_approval_request(approval_id)[
        "invalidated_reason"] == "plan no longer checked"
    assert live_requests(session.registry) == {}
    assert current_proposal(session.registry) is None
    said = _said(session)
    assert "no longer checked" in said and plan_id[:8] in said


def test_withdrawing_orphans_is_announced_once(session):
    plan_id = _checked_plan(session)
    session.announce_desk_work(True, [])
    session.registry.set_plan_state(plan_id, "refused")
    session.announce_desk_work(True, [])
    once = _said(session).count("no longer checked")
    session.announce_desk_work(True, [])
    assert (once, _said(session).count("no longer checked")) == (1, 1)


def test_a_mid_execution_plan_keeps_its_approval(session):
    """`submitted` is what `execute_plan` writes before it iterates legs, and
    it accepts a `submitted` plan again so a crash mid-execution replays by
    `client_order_id` without double-booking. Withdrawing the approval here
    would strand a half-filled book with no authority to finish it."""
    from qlab.governance.proposal import (MID_EXECUTION_EVENT,
                                          withdraw_orphans)

    plan_id = _checked_plan(session)
    _, opened = handle_api(session, "POST", "/api/approvals", {},
                           {"plan_id": plan_id})
    approval_id = opened["approval_id"]
    session.registry.set_plan_state(plan_id, "submitted")

    withdrawn, failures = withdraw_orphans(session.registry)

    assert (withdrawn, failures) == ([], [])
    assert session.registry.get_approval_request(
        approval_id)["status"] == "pending"
    # Said once, however many sweeps pass: the sweep re-reaches a stuck
    # `submitted` plan every tick.
    withdraw_orphans(session.registry)
    withdraw_orphans(session.registry)
    noted = session.registry.read_events_of_kind(MID_EXECUTION_EVENT, limit=20)
    assert [e["payload"]["approval_id"] for e in noted] == [approval_id]


def test_a_checked_plan_that_moves_to_any_other_state_still_withdraws(session):
    """The other side: only `submitted` is exempt. `refused` is still an
    orphan, and its approval is still a live authority to trade something the
    desk refused."""
    from qlab.governance.proposal import withdraw_orphans

    plan_id = _checked_plan(session)
    _, opened = handle_api(session, "POST", "/api/approvals", {},
                           {"plan_id": plan_id})
    session.registry.set_plan_state(plan_id, "refused")

    withdrawn, failures = withdraw_orphans(session.registry)

    assert failures == []
    assert [row["plan_id"] for row in withdrawn] == [plan_id]
    assert session.registry.get_approval_request(
        opened["approval_id"])["status"] == "invalidated"


def test_an_orphan_does_not_withdraw_the_live_proposal_beside_it(session):
    # The withdrawal is scoped to orphans; the desk's real question survives.
    orphan = _checked_plan(session)
    session.announce_desk_work(True, [])
    session.registry.set_plan_state(orphan, "refused")
    keeper = _checked_plan(session, tilt=0.02)

    session.announce_desk_work(True, [])

    assert (current_proposal(session.registry) or {}).get("plan_id") == keeper
    assert list(live_requests(session.registry)) == [keeper]


def test_a_universe_change_request_is_not_one_of_the_desks_proposals(session):
    """F1 reads plan approvals. A universe question has no plan, so it must
    neither become the current proposal nor be withdrawn by one."""
    from qlab.governance.approval import build_universe_change_request

    universe_change = build_universe_change_request(
        "XLK", memo_decision_id="dec-scout")
    session.registry.create_approval_request(universe_change)
    approval_id = universe_change["approval_id"]

    assert approval_id not in {row["approval_id"]
                               for row in live_requests(session.registry).values()}

    plan_id = _checked_plan(session)
    _, opened = handle_api(session, "POST", "/api/approvals", {},
                           {"plan_id": plan_id})
    assert opened["approval_id"] != approval_id
    proposal = current_proposal(session.registry)
    assert proposal["plan_id"] == plan_id

    withdrawn, failures = supersede(session.registry, plan_id)
    assert failures == []
    assert withdrawn == []
    assert session.registry.get_approval_request(
        approval_id)["status"] == "pending"


def test_withdraw_orphans_leaves_a_universe_change_alone(session):
    """An orphan is a live request whose PLAN went away. A universe question
    has no plan to lose, and withdrawing it would drop a question nothing
    replaced."""
    from qlab.governance.approval import build_universe_change_request
    from qlab.governance.proposal import withdraw_orphans

    request = build_universe_change_request("XLK", memo_decision_id="dec-scout")
    session.registry.create_approval_request(request)
    withdrawn, failures = withdraw_orphans(session.registry)
    assert (withdrawn, failures) == ([], [])
    assert session.registry.get_approval_request(
        request["approval_id"])["status"] == "pending"
