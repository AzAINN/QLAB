"""BobTheQuant deterministic supervisor: triggers, dedupe, budgets, states (P4)."""

from __future__ import annotations

import itertools

import pytest

from qlab.operator.bob import (
    BLOCKED,
    BobConfig,
    BobSupervisor,
    DEGRADED,
    OBSERVING,
    PAUSED,
)
from qlab.state.registry import Registry


@pytest.fixture
def reg():
    r = Registry(":memory:")
    yield r
    r.close()


def _bob(reg, *, coordinator=True, config=None):
    counter = itertools.count(1)
    return BobSupervisor(
        reg, coordinator_available=lambda: coordinator,
        config=config or BobConfig(),
        id_gen=lambda: f"task-{next(counter)}")


def _healthy_facts():
    return {
        "universe": ["ACWI", "BNDW"],
        "data": {"provider": "alpaca", "blocked": False,
                 "eligible_for_paper_proposal": True},
        "portfolio": {"equity": 10000.0, "drawdown": 0.01, "drawdown_tier": "none",
                      "halted": False, "gross_exposure": 1.0, "drift": 0.0},
        "regime": {"robust_state": "calm", "flip": False},
        "open_workflows": 0, "pending_approvals": 0,
    }


def test_starts_in_observe_mode():
    reg = Registry(":memory:")
    try:
        bob = _bob(reg)
        assert bob.status()["mode"] == "observe"
    finally:
        reg.close()


def test_healthy_tick_is_observing_with_a_brief_and_no_tasks(reg):
    bob = _bob(reg)
    out = bob.observe(_healthy_facts(), trading_date="2026-07-24")
    assert out["state"] == OBSERVING
    assert out["created_tasks"] == []
    assert out["brief"]["data"]["provider"] == "alpaca"
    assert out["brief"]["book"]["drawdown"] == 0.01


def test_no_llm_needed_note_health_polling_creates_no_tasks(reg):
    # Repeated unchanged healthy ticks never create tasks (no LLM churn).
    bob = _bob(reg)
    for _ in range(3):
        out = bob.observe(_healthy_facts(), trading_date="2026-07-24")
    assert reg.list_bob_tasks() == []
    assert out["state"] == OBSERVING


def test_blocked_data_moves_bob_to_blocked(reg):
    bob = _bob(reg)
    facts = _healthy_facts()
    facts["data"] = {"provider": "alpaca", "blocked": True,
                     "reason": "alpaca outage"}
    out = bob.observe(facts, trading_date="2026-07-24")
    assert out["state"] == BLOCKED
    assert bob.status()["blocked_reason"]


def test_coordinator_unavailable_degrades_not_fails(reg):
    bob = _bob(reg, coordinator=False)
    out = bob.observe(_healthy_facts(), trading_date="2026-07-24")
    # Degraded, not blocked, not an owner failure — owner/data/book still fine.
    assert out["state"] == DEGRADED
    assert out["coordinator_available"] is False


def test_drawdown_control_creates_a_deduped_workflow_task(reg):
    bob = _bob(reg)
    facts = _healthy_facts()
    facts["portfolio"]["drawdown_tier"] = "control"
    facts["portfolio"]["drawdown"] = 0.11
    out1 = bob.observe(facts, trading_date="2026-07-24")
    assert any(t["trigger"] == "drawdown_control" for t in out1["created_tasks"])
    # Same tier, same day, same state -> deduped, no second task.
    out2 = bob.observe(facts, trading_date="2026-07-24")
    assert out2["created_tasks"] == []
    kinds = [t["trigger_kind"] for t in reg.list_bob_tasks()]
    assert kinds.count("drawdown_control") == 1


def test_daily_workflow_budget_blocks_further_launches(reg):
    bob = _bob(reg, config=BobConfig(max_autonomous_workflows_per_day=1))
    # Day 1: a drift breach launches one workflow task.
    facts = _healthy_facts()
    facts["portfolio"]["drift"] = 0.2
    bob.observe(facts, trading_date="2026-07-24")
    # A different workflow trigger the same day exceeds the budget -> blocked.
    facts2 = _healthy_facts()
    facts2["regime"]["flip"] = True
    out = bob.observe(facts2, trading_date="2026-07-24")
    assert out["state"] == BLOCKED
    assert "budget" in (bob.status()["blocked_reason"] or "")


def test_paused_mode_creates_no_tasks_but_keeps_monitoring(reg):
    bob = _bob(reg)
    bob.pause()
    facts = _healthy_facts()
    facts["portfolio"]["drift"] = 0.2  # would normally launch a workflow
    out = bob.observe(facts, trading_date="2026-07-24")
    assert out["state"] == PAUSED
    assert out["created_tasks"] == []
    assert out["brief"] is not None  # monitoring/brief still available


def test_order_anomaly_records_a_pause_proposals_task(reg):
    bob = _bob(reg)
    facts = _healthy_facts()
    facts["order_anomaly"] = True
    out = bob.observe(facts, trading_date="2026-07-24")
    assert any(t["trigger"] == "order_anomaly" for t in out["created_tasks"])


def test_bob_has_no_execution_or_proposal_authority(reg):
    bob = _bob(reg)
    for forbidden in ("execute", "execute_plan", "propose", "propose_rebalance",
                      "submit_order", "place_order"):
        assert not hasattr(bob, forbidden), f"Bob must not expose {forbidden}"


def test_set_mode_rejects_unknown_mode(reg):
    bob = _bob(reg)
    with pytest.raises(ValueError, match="mode must be one of"):
        bob.set_mode("yolo")
