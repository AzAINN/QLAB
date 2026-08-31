"""Persisted, exact-plan-bound, expiring human approvals (P7, invariant #14)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from qlab.governance.approval import (
    book_revision,
    build_approval_request,
    build_universe_change_request,
    check_approval_for_execution,
    plan_digest,
)
from qlab.state.registry import Registry
from qlab.ui.server import UISession, handle_api


@pytest.fixture
def session():
    return UISession(offline_default=True, registry=Registry(":memory:"))


def _checked_plan(session):
    from qlab.trader.broker import get_broker
    from qlab.trader.plan import build_plan

    reg, mandate = session.registry, session.mandate
    broker = get_broker(reg, offline=True, starting_cash=mandate.paper_capital,
                        universe=mandate.universe_whitelist)
    targets = {t: 1.0 / len(mandate.universe_whitelist)
               for t in mandate.universe_whitelist}
    plan = build_plan(reg, broker, mandate, targets, "dec-appr")
    reg.log_verdict("dec-appr", "PASS", [], "deterministic", targets=targets)
    return plan


# --- unit: digests + binding -------------------------------------------------


def test_plan_digest_changes_when_legs_change():
    p1 = {"plan_id": "P", "decision_id": "d", "targets": {"ACWI": 1.0},
          "legs": [{"client_order_id": "c1", "ticker": "ACWI", "side": "buy",
                    "notional": 1000.0}]}
    p2 = {**p1, "legs": [{"client_order_id": "c1", "ticker": "ACWI",
                          "side": "buy", "notional": 2000.0}]}
    assert plan_digest(p1) != plan_digest(p2)


def test_book_revision_ignores_zero_positions():
    assert book_revision({"ACWI": {"qty": 0.0}}) == book_revision({})
    assert book_revision({"ACWI": {"qty": 1.0}}) != book_revision({})


def test_check_refuses_unapproved_expired_and_drifted():
    plan = {"plan_id": "P", "decision_id": "d", "state": "checked",
            "targets": {"ACWI": 1.0}, "legs": []}
    rev = book_revision({})
    approval = build_approval_request(
        plan, broker="simulated_paper", data_permit_id=None,
        current_book_revision=rev, summary={}, ttl_seconds=900,
        now=datetime(2026, 7, 24, tzinfo=timezone.utc))
    approval["status"] = "pending"
    # pending -> refused
    assert any("not 'approved'" in r for r in check_approval_for_execution(
        approval, plan, current_book_revision=rev, now_iso="2026-07-24T00:00:00+00:00"))
    approval["status"] = "approved"
    # expired -> refused
    assert any("expired" in r for r in check_approval_for_execution(
        approval, plan, current_book_revision=rev, now_iso="2027-01-01T00:00:00+00:00"))
    # book moved -> refused
    assert any("book moved" in r for r in check_approval_for_execution(
        approval, plan, current_book_revision="different",
        now_iso="2026-07-24T00:01:00+00:00"))
    # all good -> empty
    assert check_approval_for_execution(
        approval, plan, current_book_revision=rev,
        now_iso="2026-07-24T00:01:00+00:00") == []


# --- owner integration -------------------------------------------------------


def test_full_approval_execution_flow(session):
    plan = _checked_plan(session)
    _, created = handle_api(session, "POST", "/api/approvals", {},
                            {"plan_id": plan.plan_id, "offline": True})
    aid = created["approval_id"]

    # A pending approval cannot execute — a boolean cannot stand in for approval.
    _, blocked = handle_api(session, "POST", f"/api/plans/{plan.plan_id}/execute",
                            {}, {"approval_id": aid, "offline": True})
    assert blocked["executed"] is False and blocked["blocked_by"] == "approval"

    handle_api(session, "POST", f"/api/approvals/{aid}/approve", {}, {})
    _, done = handle_api(session, "POST", f"/api/plans/{plan.plan_id}/execute",
                         {}, {"approval_id": aid, "offline": True})
    assert done["executed"] is True and done["state"] == "reconciled"

    # Consumed: it cannot be replayed for a second execution.
    _, again = handle_api(session, "POST", f"/api/plans/{plan.plan_id}/execute",
                          {}, {"approval_id": aid, "offline": True})
    assert again["executed"] is False


def test_approval_invalidated_when_book_moves(session):
    plan = _checked_plan(session)
    _, created = handle_api(session, "POST", "/api/approvals", {},
                            {"plan_id": plan.plan_id, "offline": True})
    aid = created["approval_id"]
    handle_api(session, "POST", f"/api/approvals/{aid}/approve", {}, {})

    # The book moves after approval — the approval no longer covers the plan.
    session.registry.apply_fill("GLD", 1.0, 100.0, -100.0)
    _, out = handle_api(session, "POST", f"/api/plans/{plan.plan_id}/execute",
                        {}, {"approval_id": aid, "offline": True})
    assert out["executed"] is False
    assert any("book moved" in r for r in out["reasons"])
    _, ap = handle_api(session, "GET", f"/api/approvals/{aid}", {}, {})
    assert ap["status"] == "invalidated"


def test_rejected_approval_cannot_execute(session):
    plan = _checked_plan(session)
    _, created = handle_api(session, "POST", "/api/approvals", {},
                            {"plan_id": plan.plan_id, "offline": True})
    aid = created["approval_id"]
    handle_api(session, "POST", f"/api/approvals/{aid}/reject", {}, {})
    _, out = handle_api(session, "POST", f"/api/plans/{plan.plan_id}/execute",
                        {}, {"approval_id": aid, "offline": True})
    assert out["executed"] is False and out["blocked_by"] == "approval"


def test_expiry_sweep_marks_pending_expired(session):
    plan = _checked_plan(session)
    _, created = handle_api(session, "POST", "/api/approvals", {},
                            {"plan_id": plan.plan_id, "offline": True})
    aid = created["approval_id"]
    # Force the expiry into the past, then sweep via the list endpoint.
    session.registry.transition_approval(aid, "pending")
    session.registry.con.execute(
        "UPDATE approval_requests SET expires_at=? WHERE approval_id=?",
        ["2000-01-01T00:00:00+00:00", aid])
    _, listed = handle_api(session, "GET", "/api/approvals", {}, {})
    statuses = {a["approval_id"]: a["status"] for a in listed["approvals"]}
    assert statuses[aid] == "expired"


# --- the universe_change kind: an approval that can never book ---------------


def test_universe_change_request_carries_its_kind_and_no_plan():
    request = build_universe_change_request("XLK", memo_decision_id="dec-scout")
    assert request["kind"] == "universe_change"
    assert request["plan_id"] is None
    assert request["plan_digest"] is None
    assert request["targets_hash"] is None
    assert request["summary"] == {"ticker": "XLK",
                                  "memo_decision_id": "dec-scout"}


def test_plan_approvals_still_carry_kind_plan():
    plan = {"plan_id": "P", "decision_id": "d", "state": "checked",
            "targets": {"ACWI": 1.0}, "legs": []}
    request = build_approval_request(
        plan, broker="simulated_paper", data_permit_id=None,
        current_book_revision=book_revision({}), summary={})
    assert request["kind"] == "plan"


def test_execution_refuses_a_universe_change_approval_outright():
    """It binds no plan, so nothing it says could ever cover one."""
    request = build_universe_change_request("XLK", memo_decision_id="d")
    request["status"] = "approved"
    plan = {"plan_id": "P", "decision_id": "d", "state": "checked",
            "targets": {"ACWI": 1.0}, "legs": []}
    reasons = check_approval_for_execution(
        request, plan, current_book_revision=book_revision({}),
        now_iso="2026-01-01T00:00:00+00:00")
    assert any("universe_change" in reason for reason in reasons)


def test_kind_migration_is_idempotent_and_old_rows_read_plan(tmp_path):
    """A pre-`kind` row is a plan approval; re-opening the file re-migrates."""
    path = tmp_path / "registry.duckdb"
    reg = Registry(str(path))
    reg.con.execute(
        "INSERT INTO approval_requests (approval_id, plan_id, status) "
        "VALUES ('old', 'plan-1', 'pending')")
    reg.close()

    reopened = Registry(str(path))          # the migration runs a second time
    try:
        row = reopened.get_approval_request("old")
        assert row["kind"] == "plan"
        listed = {r["approval_id"]: r
                  for r in reopened.list_approval_requests(10)}
        assert listed["old"]["kind"] == "plan"
    finally:
        reopened.close()


def test_the_mandate_refuses_a_single_name_override_at_load(tmp_path, monkeypatch):
    """The door is not the only gate: a hand-edited override file must not put
    a name past the tier the mandate permits either."""
    import json

    from qlab.core.universe import load_universe
    from qlab.paths import state_path
    from qlab.trader.mandate import load_mandate

    single_name = load_universe().stock_tickers[0]
    path = state_path("mandate_overrides.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"universe_add": [single_name]}),
                    encoding="utf-8")
    with pytest.raises(ValueError, match="promotion"):
        load_mandate()
