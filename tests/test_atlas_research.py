"""Atlas research mode: registered templates only, authority before work (P6)."""

from __future__ import annotations

import itertools

import pytest

from qlab.operator.atlas import BLOCKED, AtlasConfig, AtlasSupervisor
from qlab.operator.templates import (
    TEMPLATES,
    TemplateNotAllowed,
    check_startable,
    get_template,
    template_for_trigger,
)
from qlab.state.registry import Registry


@pytest.fixture
def reg():
    r = Registry(":memory:")
    yield r
    r.close()


def _atlas(reg, config=None):
    counter = itertools.count(1)
    return AtlasSupervisor(reg, coordinator_available=lambda: True,
                         config=config or AtlasConfig(),
                         id_gen=lambda: f"task-{next(counter)}")


def _facts(**over):
    facts = {
        "universe": ["ACWI", "BNDW"],
        "data": {"provider": "alpaca", "blocked": False,
                 "eligible_for_paper_proposal": True},
        "portfolio": {"equity": 10000.0, "drawdown": 0.01,
                      "drawdown_tier": "none", "halted": False,
                      "gross_exposure": 1.0, "drift": 0.0},
        "regime": {"robust_state": "calm", "flip": False},
        "open_workflows": 0, "pending_approvals": 0,
    }
    facts.update(over)
    return facts


# --- the template registry ---------------------------------------------------


def test_only_registered_templates_exist():
    with pytest.raises(TemplateNotAllowed, match="unknown workflow template"):
        get_template("do_whatever_you_want")
    assert "regime_review" in TEMPLATES


def test_observe_mode_may_not_start_any_workflow():
    with pytest.raises(TemplateNotAllowed, match="Observe mode"):
        check_startable("regime_review", "observe", _facts())


def test_paused_mode_starts_nothing():
    with pytest.raises(TemplateNotAllowed, match="paused"):
        check_startable("regime_review", "paused", _facts())


def test_research_mode_may_not_start_a_plan_creating_template():
    # The authority boundary: creating a paper plan needs Propose mode.
    with pytest.raises(TemplateNotAllowed, match="requires Propose mode"):
        check_startable("desk_rebalance_review", "research", _facts())
    # And it is allowed in propose mode.
    assert check_startable("desk_rebalance_review", "propose",
                           _facts()).creates_plan is True


def test_blocked_data_refuses_research_templates():
    facts = _facts(data={"provider": "alpaca", "blocked": True})
    with pytest.raises(TemplateNotAllowed, match="data plane is blocked"):
        check_startable("regime_review", "research", facts)


def test_news_template_needs_operator_supplied_excerpts():
    with pytest.raises(TemplateNotAllowed, match="operator-pasted excerpts"):
        check_startable("news_risk_review", "research", _facts())
    ok = check_startable("news_risk_review", "research",
                         _facts(operator_excerpts=["pasted text"]))
    assert ok.template_id == "news_risk_review"


def test_authority_is_checked_before_data_preconditions():
    # A mode violation must not be masked by a data problem.
    facts = _facts(data={"provider": "alpaca", "blocked": True})
    with pytest.raises(TemplateNotAllowed, match="Observe mode"):
        check_startable("regime_review", "observe", facts)


def test_trigger_template_map_covers_the_workflow_triggers():
    assert template_for_trigger("regime_flip") == "regime_review"
    assert template_for_trigger("drawdown_control") == "risk_event"
    assert template_for_trigger("user_chitchat") is None


# --- supervisor dispatch -----------------------------------------------------


def test_startable_tasks_explains_refusals_in_observe(reg):
    atlas = _atlas(reg)
    facts = _facts()
    facts["regime"]["flip"] = True
    atlas.observe(facts, trading_date="2026-07-24")
    startable = atlas.startable_tasks(facts)
    assert startable and all(not s["startable"] for s in startable)
    assert any("Observe mode" in s["reason"] for s in startable)


def test_research_mode_runs_a_registered_template(reg):
    atlas = _atlas(reg)
    facts = _facts()
    facts["regime"]["flip"] = True
    out = atlas.observe(facts, trading_date="2026-07-24")
    task_id = out["created_tasks"][0]["task_id"]
    atlas.set_mode("research")

    seen = {}

    def runner(task, template_id):
        seen.update({"task_id": task["task_id"], "template_id": template_id})
        return {"summary": "regime re-read; no change recommended"}

    result = atlas.start_task(task_id, facts, runner=runner)
    assert result["completed"] is True
    assert seen["template_id"] == "regime_review"
    stored = reg.get_atlas_task(task_id)
    assert stored["status"] == "completed"
    assert stored["conclusion"]["summary"].startswith("regime re-read")


def test_a_failed_task_retries_once_then_blocks(reg):
    atlas = _atlas(reg, config=AtlasConfig(max_task_attempts=2))
    facts = _facts()
    facts["regime"]["flip"] = True
    out = atlas.observe(facts, trading_date="2026-07-24")
    task_id = out["created_tasks"][0]["task_id"]
    atlas.set_mode("research")

    def boom(task, template_id):
        raise RuntimeError("coordinator died")

    first = atlas.start_task(task_id, facts, runner=boom)
    assert first["completed"] is False
    second = atlas.start_task(task_id, facts, runner=boom)
    assert second["completed"] is False
    # A third attempt is refused: no automatic loop after a second failure.
    third = atlas.start_task(task_id, facts, runner=boom)
    assert third["started"] is False and third["blocked_by"] == "retry_budget"
    assert reg.get_atlas_task(task_id)["status"] == "blocked"
    assert atlas.status()["state"] == BLOCKED


def test_plan_creating_template_is_refused_in_research_at_dispatch(reg):
    atlas = _atlas(reg)
    facts = _facts()
    facts["portfolio"]["drift"] = 0.5  # drift_breach -> desk_rebalance_review
    out = atlas.observe(facts, trading_date="2026-07-24")
    task_id = out["created_tasks"][0]["task_id"]
    atlas.set_mode("research")

    def runner(task, template_id):  # must never be called
        raise AssertionError("runner ran despite an authority refusal")

    result = atlas.start_task(task_id, facts, runner=runner)
    assert result["started"] is False and result["blocked_by"] == "authority"
    assert "Propose mode" in result["reason"]
    assert reg.get_atlas_task(task_id)["status"] == "blocked"


# --- autonomy (opt-in, never an authority widening) --------------------------


def test_autonomous_tick_runs_only_what_the_mode_permits(reg):
    """QLAB_ATLAS_AUTONOMOUS removes the button press, not the boundary."""
    from qlab.operator.heartbeat import build_owner_tick

    class _Session:
        def __init__(self, atlas):
            self.atlas = atlas
            self.started = []

        def atlas_observe(self, offline):
            facts = _facts()
            facts["regime"]["flip"] = True
            return self.atlas.observe(facts, trading_date="2020-01-02")

        def refresh_desk_read(self, offline):
            return {}

        def atlas_facts(self, offline):
            return _facts()

        def atlas_workflow_runner(self, task, template_id):
            self.started.append(template_id)
            return {"template_id": template_id, "action_taken": True}

        def atlas_run_startable(self, offline, limit=1):
            facts = self.atlas_facts(offline)
            out = []
            for c in self.atlas.startable_tasks(facts):
                if c.get("startable") and len(out) < limit:
                    self.atlas.start_task(c["task_id"], facts,
                                          runner=self.atlas_workflow_runner)
                    out.append(c)
            return out

    import threading

    atlas = _atlas(reg)
    session = _Session(atlas)
    tick = build_owner_tick(session, threading.Lock(), offline=True,
                            autonomous=True)

    # Observe mode: the trigger fires, but autonomy launches nothing.
    tick()
    assert session.started == []

    # Research mode: the same autonomy now runs the permitted template.
    atlas.set_mode("research")
    tick()
    assert session.started == ["regime_review"]


def test_autonomy_still_cannot_create_a_paper_plan(reg):
    """The plan-creation boundary survives autonomy in Research mode."""
    atlas = _atlas(reg)
    atlas.set_mode("research")
    facts = _facts()
    facts["portfolio"]["drift"] = 0.5      # -> desk_rebalance_review
    out = atlas.observe(facts, trading_date="2020-01-02")
    task_id = out["created_tasks"][0]["task_id"]

    def runner(task, template_id):
        raise AssertionError("a plan-creating template ran in Research mode")

    result = atlas.start_task(task_id, facts, runner=runner)
    assert result["started"] is False and result["blocked_by"] == "authority"


# --- declared template graphs must be runnable (P1 contract mismatch) --------

def test_every_template_declares_an_executable_phase_graph():
    # A template whose graph omits a dependency would create a workflow that
    # deadlocks and an Atlas task that waits on it forever. Declarations are
    # contracts, so they are checked here rather than discovered in production.
    from qlab.state.registry import validate_phase_graph

    for template_id, template in TEMPLATES.items():
        if not template.phases:
            assert not template.needs_coordinator, (
                f"{template_id} needs a coordinator but declares no phases")
            continue
        validate_phase_graph(template.phases)


def test_the_news_read_template_is_executable():
    # It previously named a phase the registry did not accept, so the one
    # template Atlas uses for qualitative reads could never have run.
    from qlab.state.registry import validate_phase_graph

    template = get_template("news_read")
    validate_phase_graph(template.phases)


def test_a_coordinator_template_declares_the_graph_it_actually_runs():
    # The four review templates previously declared a reduced graph while the
    # runner always created the standard one; the declaration was decorative.
    for template_id in ("regime_review", "research_review", "risk_event",
                        "news_risk_review"):
        template = get_template(template_id)
        assert "optimizer" in template.phases, (
            f"{template_id} declares a referee without the optimizer it depends on")
