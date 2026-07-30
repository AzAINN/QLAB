"""Atlas deterministic supervisor: triggers, dedupe, budgets, states (P4)."""

from __future__ import annotations

import itertools

import pytest

from qlab.operator.atlas import (
    BLOCKED,
    AtlasConfig,
    AtlasSupervisor,
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


def _atlas(reg, *, coordinator=True, config=None):
    counter = itertools.count(1)
    return AtlasSupervisor(
        reg, coordinator_available=lambda: coordinator,
        config=config or AtlasConfig(),
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


def test_starts_in_research_mode():
    # A fresh desk starts in Research, not Observe. Observe permits no workflow
    # at all, so the desk shipped inert. Research is the highest mode that still
    # cannot create a paper plan, so this widens what Atlas researches without
    # moving the execution boundary.
    reg = Registry(":memory:")
    try:
        atlas = _atlas(reg)
        assert atlas.status()["mode"] == "research"
    finally:
        reg.close()


def test_healthy_tick_is_observing_with_a_brief_and_no_tasks(reg):
    atlas = _atlas(reg)
    out = atlas.observe(_healthy_facts(), trading_date="2026-07-24")
    assert out["state"] == OBSERVING
    assert out["created_tasks"] == []
    assert out["brief"]["data"]["provider"] == "alpaca"
    assert out["brief"]["book"]["drawdown"] == 0.01


def test_no_llm_needed_note_health_polling_creates_no_tasks(reg):
    # Repeated unchanged healthy ticks never create tasks (no LLM churn).
    atlas = _atlas(reg)
    for _ in range(3):
        out = atlas.observe(_healthy_facts(), trading_date="2026-07-24")
    assert reg.list_atlas_tasks() == []
    assert out["state"] == OBSERVING


def test_blocked_data_moves_atlas_to_blocked(reg):
    atlas = _atlas(reg)
    facts = _healthy_facts()
    facts["data"] = {"provider": "alpaca", "blocked": True,
                     "reason": "alpaca outage"}
    out = atlas.observe(facts, trading_date="2026-07-24")
    assert out["state"] == BLOCKED
    assert atlas.status()["blocked_reason"]


def test_coordinator_unavailable_degrades_not_fails(reg):
    atlas = _atlas(reg, coordinator=False)
    out = atlas.observe(_healthy_facts(), trading_date="2026-07-24")
    # Degraded, not blocked, not an owner failure — owner/data/book still fine.
    assert out["state"] == DEGRADED
    assert out["coordinator_available"] is False


def test_drawdown_control_creates_a_deduped_workflow_task(reg):
    atlas = _atlas(reg)
    facts = _healthy_facts()
    facts["portfolio"]["drawdown_tier"] = "control"
    facts["portfolio"]["drawdown"] = 0.11
    out1 = atlas.observe(facts, trading_date="2026-07-24")
    assert any(t["trigger"] == "drawdown_control" for t in out1["created_tasks"])
    # Same tier, same day, same state -> deduped, no second task.
    out2 = atlas.observe(facts, trading_date="2026-07-24")
    assert out2["created_tasks"] == []
    kinds = [t["trigger_kind"] for t in reg.list_atlas_tasks()]
    assert kinds.count("drawdown_control") == 1


def test_daily_workflow_budget_blocks_further_launches(reg):
    atlas = _atlas(reg, config=AtlasConfig(max_autonomous_workflows_per_day=1))
    # Day 1: a drift breach launches one workflow task.
    facts = _healthy_facts()
    facts["portfolio"]["drift"] = 0.2
    atlas.observe(facts, trading_date="2026-07-24")
    # A different workflow trigger the same day exceeds the budget -> blocked.
    facts2 = _healthy_facts()
    facts2["regime"]["flip"] = True
    out = atlas.observe(facts2, trading_date="2026-07-24")
    assert out["state"] == BLOCKED
    assert "budget" in (atlas.status()["blocked_reason"] or "")


def test_daily_budget_counts_the_trading_date_not_the_wall_clock(reg):
    """The budget must survive UTC rollover.

    Tasks are stamped with created_at in wall-clock UTC, which rolls at 00:00
    while a trading date does not. Counting by created_at silently dropped the
    budget for anything recorded after midnight UTC; the trading date in the
    dedupe key is the authority.
    """
    atlas = _atlas(reg, config=AtlasConfig(max_autonomous_workflows_per_day=1))
    facts = _healthy_facts()
    facts["portfolio"]["drift"] = 0.2
    atlas.observe(facts, trading_date="2020-01-02")  # a date that is never "today"

    facts2 = _healthy_facts()
    facts2["regime"]["flip"] = True
    out = atlas.observe(facts2, trading_date="2020-01-02")
    assert out["state"] == BLOCKED
    assert "budget" in (atlas.status()["blocked_reason"] or "")

    # A different trading date has its own budget.
    facts3 = _healthy_facts()
    facts3["regime"]["flip"] = True
    out3 = atlas.observe(facts3, trading_date="2020-01-03")
    assert out3["created_tasks"]


def test_paused_mode_creates_no_tasks_but_keeps_monitoring(reg):
    atlas = _atlas(reg)
    atlas.pause()
    facts = _healthy_facts()
    facts["portfolio"]["drift"] = 0.2  # would normally launch a workflow
    out = atlas.observe(facts, trading_date="2026-07-24")
    assert out["state"] == PAUSED
    assert out["created_tasks"] == []
    assert out["brief"] is not None  # monitoring/brief still available


def test_order_anomaly_records_a_pause_proposals_task(reg):
    atlas = _atlas(reg)
    facts = _healthy_facts()
    facts["order_anomaly"] = True
    out = atlas.observe(facts, trading_date="2026-07-24")
    assert any(t["trigger"] == "order_anomaly" for t in out["created_tasks"])


def test_atlas_has_no_execution_or_proposal_authority(reg):
    atlas = _atlas(reg)
    for forbidden in ("execute", "execute_plan", "propose", "propose_rebalance",
                      "submit_order", "place_order"):
        assert not hasattr(atlas, forbidden), f"Atlas must not expose {forbidden}"


def test_set_mode_rejects_unknown_mode(reg):
    atlas = _atlas(reg)
    with pytest.raises(ValueError, match="mode must be one of"):
        atlas.set_mode("yolo")
