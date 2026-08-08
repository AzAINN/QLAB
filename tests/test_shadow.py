"""Shadow-rollout scorecard: evidence for a human decision, never a grant (P9)."""

from __future__ import annotations

import pytest

from qlab.operator.shadow import shadow_scorecard
from qlab.state.registry import Registry


@pytest.fixture
def reg():
    r = Registry(":memory:")
    yield r
    r.close()


def _task(reg, task_id, trigger, status, *, action_taken=None,
          origin="trigger"):
    reg.create_atlas_task(task_id, f"{task_id}-key", trigger, {},
                          "regime_review", origin=origin)
    conclusion = None if action_taken is None else {"action_taken": action_taken}
    reg.update_atlas_task(task_id, status=status, conclusion=conclusion)


def _approval(reg, approval_id, status):
    reg.create_approval_request({
        "approval_id": approval_id, "plan_id": f"plan-{approval_id}",
        "plan_digest": "d", "decision_id": "dec", "targets_hash": "t",
        "data_permit_id": None, "broker": "simulated_paper",
        "book_revision": "r", "expected_cost": {}, "summary": {},
        "expires_at": "2030-01-01T00:00:00+00:00"})
    if status != "pending":
        reg.transition_approval(approval_id, status)


def test_empty_history_is_not_sufficient_evidence(reg):
    card = shadow_scorecard(reg)
    assert card["tasks"]["total"] == 0
    assert card["readiness"]["sufficient_evidence"] is False
    assert any("too little history" in b for b in card["readiness"]["blockers"])


def test_task_counts_and_false_trigger_rate(reg):
    _task(reg, "t1", "regime_flip", "completed", action_taken=True)
    _task(reg, "t2", "regime_flip", "completed", action_taken=False)
    _task(reg, "t3", "drift_breach", "failed")
    card = shadow_scorecard(reg)
    tasks = card["tasks"]
    assert tasks["total"] == 3
    assert tasks["completed"] == 2 and tasks["failed"] == 1
    assert tasks["no_action_conclusions"] == 1
    assert tasks["false_trigger_rate"] == pytest.approx(0.5)
    assert tasks["by_trigger"]["regime_flip"] == 2


def test_proposal_validity_counts_only_decided_proposals(reg):
    _approval(reg, "a1", "approved")
    _approval(reg, "a2", "rejected")
    _approval(reg, "a3", "expired")        # never ruled on
    _approval(reg, "a4", "invalidated")    # never ruled on
    card = shadow_scorecard(reg)["approvals"]
    assert card["total"] == 4
    assert card["decided"] == 2
    assert card["accepted"] == 1
    assert card["acceptance_rate"] == pytest.approx(0.5)
    assert card["expired"] == 1 and card["invalidated"] == 1


def test_model_cost_aggregates_tokens_and_fallbacks(reg):
    reg.record_model_invocation({
        "invocation_id": "m1", "role": "reporter", "requested_tier": "quick",
        "resolved_model": "sonnet", "backend": "claude_cli", "status": "ok",
        "tokens": 1000, "fallback_reason": None})
    reg.record_model_invocation({
        "invocation_id": "m2", "role": "referee", "requested_tier": "deep",
        "resolved_model": "inherit", "backend": "claude_cli", "status": "ok",
        "tokens": 4000, "fallback_reason": "deep tier unavailable"})
    cost = shadow_scorecard(reg)["model_cost"]
    assert cost["invocations"] == 2
    assert cost["tokens"] == 5000
    assert cost["by_tier"] == {"quick": 1, "deep": 1}
    assert cost["fallbacks"] == 1


def test_invalidated_proposals_block_readiness(reg):
    for index in range(12):
        _task(reg, f"t{index}", "regime_flip", "completed", action_taken=True)
    _approval(reg, "a1", "approved")
    _approval(reg, "a2", "approved")
    _approval(reg, "a3", "invalidated")
    card = shadow_scorecard(reg)
    assert card["readiness"]["sufficient_evidence"] is False
    assert any("went stale" in b for b in card["readiness"]["blockers"])


def test_clean_history_reports_sufficient_evidence_but_grants_nothing(reg):
    for index in range(12):
        _task(reg, f"t{index}", "regime_flip", "completed", action_taken=True)
    for index in range(3):
        _approval(reg, f"a{index}", "approved")
    card = shadow_scorecard(reg)
    assert card["readiness"]["sufficient_evidence"] is True
    # Even with clean evidence, this is never a promotion.
    assert "separate design review" in card["readiness"]["note"]
    assert "grant" not in card["tasks"]


def test_an_approved_proposal_does_not_move_the_false_trigger_rate(reg):
    """The measurement that governs authority promotion is about UNATTENDED
    judgment. A proposal is attended by construction — a human asked for it and
    a human approved it — so an approved proposal concluding `action_taken:
    False` counted here would be scored as a trigger that fired without earning
    its wake, when no trigger fired at all."""
    _task(reg, "t1", "regime_flip", "completed", action_taken=True)
    clean = shadow_scorecard(reg)["tasks"]
    assert clean["false_trigger_rate"] == pytest.approx(0.0)

    _task(reg, "p1", "proposal:regime_review", "completed", action_taken=False,
          origin="proposal")
    card = shadow_scorecard(reg)["tasks"]

    assert card["false_trigger_rate"] == pytest.approx(0.0)
    assert card["total"] == 1
    assert card["no_action_conclusions"] == 0
    # And it does not invent a trigger kind either: `by_trigger` is what an
    # operator reads to see which conditions wake this desk up.
    assert "proposal:regime_review" not in card["by_trigger"]


def test_since_filter_narrows_the_window(reg):
    _task(reg, "t1", "regime_flip", "completed", action_taken=True)
    assert shadow_scorecard(reg, since="2000-01-01")["tasks"]["total"] == 1
    assert shadow_scorecard(reg, since="2999-01-01")["tasks"]["total"] == 0
