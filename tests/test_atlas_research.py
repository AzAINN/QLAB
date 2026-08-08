"""Atlas research mode: registered templates only, authority before work (P6)."""

from __future__ import annotations

import importlib
import itertools
import threading

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
    atlas.set_mode("observe")   # no longer the default; this test is about it
    facts = _facts()
    facts["regime"]["flip"] = True
    atlas.observe(facts, trading_date="2026-07-24")
    # `today` is pinned to the trading date: this test is about the mode gate,
    # and a hardcoded date that silently ages into "stale" would quietly stop
    # testing that. (Which is the very bug max_task_age_days exists for.)
    startable = atlas.startable_tasks(facts, today="2026-07-24")
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
    # The task stays queued and carries the reason. It used to be written
    # "blocked" — the status an exhausted retry budget earns — which nothing
    # moves back to queued, so the refusal was permanent: raising the mode to
    # Propose could not recover it, and the day's trigger could not be
    # recreated because its dedupe key was unchanged.
    row = reg.get_atlas_task(task_id)
    assert row["status"] == "queued"
    assert "Propose mode" in (row["error"] or "")


def test_a_drifting_drift_does_not_re_key_its_own_trigger(reg):
    # The payload carried the live drift value and the dedupe key hashed the
    # payload, so ordinary price movement minted a fresh key every tick: three
    # tasks for one breach, the daily workflow budget gone, and a genuine
    # drawdown trigger refused for the rest of the day.
    atlas = _atlas(reg)
    created = []
    for drift in (0.0512, 0.0517, 0.0523, 0.0641):
        facts = _facts()
        facts["portfolio"]["drift"] = drift
        out = atlas.observe(facts, trading_date="2026-07-24")
        created += [t for t in out["created_tasks"]
                    if t["trigger"] == "drift_breach"]

    assert len(created) == 1, created
    # The exact drift is still reported to the operator, just not identifying.
    task = reg.get_atlas_task(created[0]["task_id"])
    assert task["trigger_payload"]["drift"] == 0.0512


def test_a_running_task_keeps_the_desk_reported_as_coordinating(reg):
    # start_task wrote COORDINATING and the next observe tick overwrote it with
    # OBSERVING, so the desk reported itself idle while a workforce run it had
    # launched was still executing.
    atlas = _atlas(reg)
    facts = _facts()
    facts["portfolio"]["drift"] = 0.5
    out = atlas.observe(facts, trading_date="2026-07-24")
    task_id = out["created_tasks"][0]["task_id"]
    atlas.set_mode("propose")
    assert atlas.start_task(task_id, facts)["started"] is True

    observed = atlas.observe(facts, trading_date="2026-07-24")
    assert observed["state"] == "coordinating"

    # And once the task resolves, the desk goes back to observing.
    reg.update_atlas_task(task_id, status="completed", conclusion={"ok": True})
    assert atlas.observe(
        facts, trading_date="2026-07-24")["state"] == "observing"


def test_raising_the_mode_recovers_a_task_refused_on_authority(reg):
    atlas = _atlas(reg)
    facts = _facts()
    facts["portfolio"]["drift"] = 0.5
    out = atlas.observe(facts, trading_date="2026-07-24")
    task_id = out["created_tasks"][0]["task_id"]

    atlas.set_mode("research")
    assert atlas.start_task(task_id, facts)["blocked_by"] == "authority"

    # The refusal was about the mode, so the mode is what changes it.
    atlas.set_mode("propose")
    assert any(entry["task_id"] == task_id
               for entry in atlas.startable_tasks(facts, today="2026-07-24"))
    assert atlas.start_task(task_id, facts)["started"] is True


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
            for c in self.atlas.startable_tasks(facts, today="2020-01-02"):
                if c.get("startable") and len(out) < limit:
                    self.atlas.start_task(c["task_id"], facts,
                                          runner=self.atlas_workflow_runner)
                    out.append(c)
            return out

    import threading

    atlas = _atlas(reg)
    atlas.set_mode("observe")   # no longer the default; the first leg needs it
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


def test_the_beat_drives_an_approval_that_landed_while_the_slot_was_busy():
    """The sweep needs a caller or it is a method nothing runs.

    Deliberately NOT gated on autonomy: a human already approved this task, and
    the dispatch that registered its workflow could not drive it. Nothing else
    comes back for it, so an unattended desk would leave approved work parked
    at phase one forever.
    """
    from qlab.operator.heartbeat import build_owner_tick
    from qlab.ui.server import UISession

    session = UISession(offline_default=True, registry=Registry(":memory:"))
    drove: list[str] = []
    session.drive_workflow = lambda wid, goal, roles=(): (
        drove.append(wid) or {"driving": True})
    workflow_id = session.registry.start_workflow(
        "portfolio_review", {"goal": "[news_read] read the window"},
        phases=("news-analyst",))["workflow_id"]
    session.registry.create_atlas_task(
        "task-parked", "proposal:news_read|2026-08-06|SPY|news_read",
        "proposal:news_read", {}, "news_read", origin="proposal")
    session.registry.update_atlas_task("task-parked", status="running",
                                       workflow_id=workflow_id)

    result = build_owner_tick(session, threading.Lock(), offline=True)()

    assert drove == [workflow_id]
    assert result["driven"] == [{"task_id": "task-parked",
                                 "workflow_id": workflow_id, "driving": True}]


def test_an_unattended_owner_reaps_the_workflow_it_would_otherwise_respawn():
    """The reaper was reachable only from the snapshot path and GET
    /api/workflows — both client polls. An owner running headless never reaped,
    so a workflow whose coordinator died stayed `running` forever, and the sweep
    respawns a coordinator for every running workflow it finds. The beat is the
    one caller that is always there.
    """
    from qlab.operator.heartbeat import build_owner_tick
    from qlab.ui.server import UISession

    session = UISession(offline_default=True, registry=Registry(":memory:"))
    drove: list[str] = []
    session.drive_workflow = lambda wid, goal, roles=(): (
        drove.append(wid) or {"driving": True})
    workflow_id = session.registry.start_workflow(
        "portfolio_review", {"goal": "g"},
        phases=("news-analyst",))["workflow_id"]
    # A coordinator that died without saying so: the row still reads `running`
    # and has not been touched since long before the lease could have expired.
    session.registry.con.execute(
        "UPDATE workflows SET updated_at=? WHERE workflow_id=?",
        ["2000-01-01T00:00:00+00:00", workflow_id])
    session.registry.create_atlas_task(
        "task-stale", "proposal:news_read|2026-08-06|SPY|stale",
        "proposal:news_read", {}, "news_read", origin="proposal")
    session.registry.update_atlas_task("task-stale", status="running",
                                       workflow_id=workflow_id)

    result = build_owner_tick(session, threading.Lock(), offline=True)()

    assert [row["workflow_id"] for row in result["reaped"]] == [workflow_id]
    assert session.registry.get_workflow(workflow_id)["status"] == "interrupted"
    # One tick closes the loop: reaped, reconciled, and therefore not respawned.
    assert session.registry.get_atlas_task("task-stale")["status"] == "failed"
    assert drove == []


def test_owner_tick_keeps_external_news_fetch_outside_dispatch_lock():
    """Slow providers must not freeze every owner API request."""
    from qlab.operator.heartbeat import build_owner_tick

    class TrackingLock:
        held = False

        def __enter__(self):
            assert not self.held
            self.held = True

        def __exit__(self, *exc):
            self.held = False

    lock = TrackingLock()
    calls = []

    class Session:
        autonomous = False

        def fetch_desk_news(self, offline):
            assert lock.held is False
            calls.append("fetch")
            return {"items": [], "provider_name": "synthetic", "error": None}

        def compose_desk_read(self, offline, *, prefetched_news):
            assert lock.held is True
            calls.append("compose")
            return {}

        def atlas_observe(self, offline):
            assert lock.held is True
            calls.append("observe")
            return {"state": "observing"}

    result = build_owner_tick(
        Session(),
        lock,
        offline=True,
    )()

    assert calls == ["fetch", "compose", "observe"]
    assert result["state"] == "observing"


def test_a_failing_heartbeat_reports_why_instead_of_looking_healthy():
    # The only channel for a tick failure was a print to a stdout that
    # `qlab tui` points at DEVNULL, and the status error count was rendered
    # nowhere — so a supervisor whose every tick raised rendered exactly like
    # a healthy desk with nothing to report.
    from qlab.operator.heartbeat import AtlasHeartbeat

    def always_fails():
        raise RuntimeError("desk read is unreachable")

    beat = AtlasHeartbeat(always_fails, interval_s=0.01)
    assert beat.tick_once() is None

    status = beat.status()
    assert status["errors"] == 1
    assert "unreachable" in status["last_error"]
    assert status["last_error_at"]


def test_the_error_timestamp_is_a_clock_reading_never_a_string():
    # `last_error_at` was born as "" and became `clock()` on the first failing
    # tick. A field that changes JSON type mid-run poisons the whole snapshot
    # for a typed client: one tick error and every subsequent /api/tui poll
    # failed to parse until the owner restarted. A timestamp is a number or
    # None — in every state, including "no error yet".
    from qlab.operator.heartbeat import AtlasHeartbeat

    def always_fails():
        raise RuntimeError("desk read is unreachable")

    beat = AtlasHeartbeat(always_fails, interval_s=0.01,
                          clock=lambda: 80444.857107791)
    assert beat.status()["last_error_at"] is None
    beat.tick_once()
    assert beat.status()["last_error_at"] == 80444.857107791


def test_a_failed_recompose_stops_the_stale_window_gating_a_news_template():
    # The heartbeat swallows a read failure so the supervisor keeps observing.
    # It used to leave the previous tick's window cached, and `atlas_facts`
    # derives `news_window_items` from it -- so the news precondition passed on
    # evidence that was no longer current.
    from qlab.operator.heartbeat import build_owner_tick
    from qlab.operator.templates import TemplateNotAllowed, check_startable
    from qlab.ui.server import UISession

    session = UISession(offline_default=True, registry=Registry(":memory:"))
    # A composed read with a real window, as a healthy tick would leave behind.
    session._desk_read = {"grounding": {"hashes": ["h1", "h2"]},
                          "observations": ["prior read"]}
    assert session.atlas_facts(True)["news_window_items"] == 2

    def explode(offline, *, prefetched_news):
        raise RuntimeError("grounding rejected a malformed record")

    session.compose_desk_read = explode
    session.fetch_desk_news = lambda offline: {
        "items": [], "provider_name": "synthetic", "error": None}

    build_owner_tick(session, threading.Lock(), offline=True)()

    facts = session.atlas_facts(True)
    assert facts["news_window_items"] == 0
    with pytest.raises(TemplateNotAllowed, match="non-empty grounded news"):
        check_startable("news_read", "research", facts)
    assert "STALE" in " ".join(session._desk_read["observations"])


def test_a_cold_desk_read_never_fetches_under_the_dispatch_lock():
    # tui_snapshot, /api/atlas/read, /api/atlas/startable and POST
    # /api/atlas/observe all reach desk_read while the owner holds its dispatch
    # lock. On a cold cache that used to run six RSS feeds at 5 s each, so a TUI
    # attaching to a fresh --online owner froze the whole desk. Fetching belongs
    # to fetch_desk_news, which the heartbeat calls outside the lock.
    from qlab.ui.server import UISession, handle_api

    session = UISession(offline_default=True, registry=Registry(":memory:"))

    def forbidden(offline):
        raise AssertionError("desk_read fetched news under the dispatch lock")

    session.fetch_desk_news = forbidden

    assert session._desk_read is None                       # cold cache
    read = session.desk_read(True)
    # Loud about the window it does not have: a desk that has not fetched yet
    # must not read as a desk with nothing to report.
    assert read["grounding"]["hashes"] == []
    assert "not fetched yet" in read["news_error"]
    assert any("UNAVAILABLE" in o for o in read["observations"])

    for method, path, query, body in (
        ("GET", "/api/atlas/read", {"offline": ["1"]}, {}),
        ("GET", "/api/atlas/read", {"offline": ["1"], "refresh": ["1"]}, {}),
        ("GET", "/api/atlas/startable", {"offline": ["1"]}, {}),
        ("POST", "/api/atlas/observe", {}, {"offline": True}),
    ):
        session._desk_read = None                           # cold again
        status, _ = handle_api(session, method, path, query, body)
        assert status == 200
    session._desk_read = None
    session.tui_snapshot(True, event_limit=5)


def test_a_window_fetched_outside_the_lock_is_what_desk_read_composes():
    from qlab.ui.server import UISession

    session = UISession(offline_default=True, registry=Registry(":memory:"))
    session.fetch_desk_news(True)                # what the heartbeat does first
    assert session._desk_news is not None
    fetched = len(session._desk_news["items"])

    read = session.desk_read(True)               # composed under the lock
    assert "news_error" not in read
    assert read["grounding"]["item_count"] == fetched


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


# --- a dispatched workflow is not a finished task (P1) -----------------------

def _dispatch_task(reg, atlas):
    """Queue one regime_review task and put Atlas in a mode that may start it."""
    facts = _facts()
    facts["regime"]["flip"] = True
    out = atlas.observe(facts, trading_date="2026-07-24")
    atlas.set_mode("research")
    return out["created_tasks"][0]["task_id"], facts


def test_a_dispatched_workflow_leaves_the_task_running(reg):
    # The bug this pins: creating a durable workflow row was reported as the
    # task having completed, so Atlas claimed research it had not done.
    from qlab.operator.atlas import Dispatched

    atlas = _atlas(reg)
    task_id, facts = _dispatch_task(reg, atlas)
    workflow = reg.start_workflow("portfolio_review", {"goal": "regime re-read"})

    def runner(task, template_id):
        return Dispatched(workflow_id=workflow["workflow_id"])

    result = atlas.start_task(task_id, facts, runner=runner)

    assert result["completed"] is False
    assert result["dispatched"] is True
    assert result["workflow_id"] == workflow["workflow_id"]
    stored = reg.get_atlas_task(task_id)
    assert stored["status"] == "running"
    assert stored["workflow_id"] == workflow["workflow_id"]


def test_a_deterministic_conclusion_still_completes_the_task(reg):
    # Templates that need no coordinator (desk_brief) genuinely finish inline.
    atlas = _atlas(reg)
    task_id, facts = _dispatch_task(reg, atlas)

    result = atlas.start_task(
        task_id, facts, runner=lambda task, tid: {"summary": "read only"})

    assert result["completed"] is True
    assert reg.get_atlas_task(task_id)["status"] == "completed"


def test_reconcile_completes_a_task_whose_workflow_finished(reg):
    from qlab.operator.atlas import Dispatched

    atlas = _atlas(reg)
    task_id, facts = _dispatch_task(reg, atlas)
    # A single-phase run reaches 'complete' without standing in for the referee
    # gate: this test is about reconciliation, not about the approval chain,
    # and driving a full portfolio_review here would mean persisting a bound
    # PASS verdict that has nothing to do with what is under test.
    workflow = reg.start_workflow(
        "portfolio_review", {"goal": "regime re-read"}, phases=("analyst",))
    atlas.start_task(task_id, facts,
                     runner=lambda t, tid: Dispatched(workflow["workflow_id"]))

    reg.update_workflow_phase(
        workflow["workflow_id"], "analyst", "done", summary="ok",
        artifacts={"moment_set_id": "m1", "objective_id": "o1",
                   "decision_id": "d1", "regime": "calm",
                   "regime_summary": "calm and quiet"})

    moved = atlas.reconcile_tasks()

    assert [m["task_id"] for m in moved] == [task_id]
    stored = reg.get_atlas_task(task_id)
    assert stored["status"] == "completed"
    assert stored["conclusion"]["workflow_status"] == "complete"


def test_reconcile_fails_a_task_whose_workflow_was_interrupted(reg):
    from qlab.operator.atlas import Dispatched

    atlas = _atlas(reg)
    task_id, facts = _dispatch_task(reg, atlas)
    workflow = reg.start_workflow("portfolio_review", {"goal": "regime re-read"})
    atlas.start_task(task_id, facts,
                     runner=lambda t, tid: Dispatched(workflow["workflow_id"]))

    reg.interrupt_workflow(workflow["workflow_id"], reason="operator stopped it")
    atlas.reconcile_tasks()

    stored = reg.get_atlas_task(task_id)
    assert stored["status"] == "failed"
    assert "interrupted" in (stored["error"] or "")


def test_reconcile_leaves_a_still_running_workflow_alone(reg):
    from qlab.operator.atlas import Dispatched

    atlas = _atlas(reg)
    task_id, facts = _dispatch_task(reg, atlas)
    workflow = reg.start_workflow("portfolio_review", {"goal": "regime re-read"})
    atlas.start_task(task_id, facts,
                     runner=lambda t, tid: Dispatched(workflow["workflow_id"]))

    assert atlas.reconcile_tasks() == []
    assert reg.get_atlas_task(task_id)["status"] == "running"


def test_reconcile_fails_loud_when_the_bound_workflow_is_gone(reg):
    # A task bound to a workflow that does not exist must not sit running
    # forever, and must never be reported as complete.
    from qlab.operator.atlas import Dispatched

    atlas = _atlas(reg)
    task_id, facts = _dispatch_task(reg, atlas)
    atlas.start_task(task_id, facts,
                     runner=lambda t, tid: Dispatched("wf-that-never-existed"))

    atlas.reconcile_tasks()

    stored = reg.get_atlas_task(task_id)
    assert stored["status"] == "failed"
    assert "wf-that-never-existed" in (stored["error"] or "")


def test_reconcile_ignores_a_running_task_with_no_workflow_binding(reg):
    # A deterministic template that is mid-flight has no workflow to read; it
    # must not be swept into a failure by reconciliation.
    atlas = _atlas(reg)
    task_id, facts = _dispatch_task(reg, atlas)
    reg.update_atlas_task(task_id, status="running")

    assert atlas.reconcile_tasks() == []
    assert reg.get_atlas_task(task_id)["status"] == "running"


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


# --- stale queued tasks ------------------------------------------------------


def test_a_queued_task_from_a_past_trading_day_is_not_offered_as_startable(reg):
    """The live desk had 15 queued `drift_breach` tasks, one per trading day
    back to 2026-07-19, all still offered as startable work.

    A drift breach is a statement about a portfolio on a given day. Fifteen
    days later the weights that raised it are gone, so starting it would run a
    rebalance review against a drift that no longer exists. The dedupe key
    holds the trading date, so staleness is knowable without a clock guess.
    """
    atlas = _atlas(reg)
    atlas.set_mode("propose")   # so the fresh one clears the mode gate too
    facts = _facts()
    facts["portfolio"]["drift"] = 0.9
    atlas.observe(facts, trading_date="2026-07-19")
    atlas.observe(facts, trading_date="2026-08-03")

    entries = atlas.startable_tasks(facts, today="2026-08-03")
    assert len(entries) == 2
    stale = [e for e in entries if not e["startable"]
             and "stale" in (e.get("reason") or "")]
    assert len(stale) == 1, entries
    # Invariant 4: a refusal states its reason, and the reason names the age.
    assert "2026-07-19" in stale[0]["reason"]
    assert stale[0]["stale"] is True
    # ...and the fresh one is still offered, so the cutoff did not eat the desk.
    assert [e for e in entries if e["startable"]]


def test_staleness_is_judged_against_the_trading_date_not_wall_clock(reg):
    """`today` is injected, never read from the clock: a deterministic surface
    that changes answer at midnight UTC cannot be tested or reproduced."""
    atlas = _atlas(reg)
    atlas.set_mode("research")
    facts = _facts()
    facts["portfolio"]["drift"] = 0.9
    atlas.observe(facts, trading_date="2026-07-19")
    fresh = atlas.startable_tasks(facts, today="2026-07-20")
    assert all(not e.get("stale") for e in fresh), fresh
    stale = atlas.startable_tasks(facts, today="2026-09-01")
    assert all(e.get("stale") for e in stale), stale


def test_startable_never_answers_stale_and_startable_at_once(reg):
    """Whatever else is true, a stale entry is never offered as startable."""
    atlas = _atlas(reg)
    atlas.set_mode("research")
    facts = _facts()
    facts["portfolio"]["drift"] = 0.9
    for day in ("2026-07-19", "2026-07-27", "2026-08-01", "2026-08-03"):
        atlas.observe(facts, trading_date=day)
    for today in ("2026-08-03", "2026-08-10", "2027-01-01"):
        for entry in atlas.startable_tasks(facts, today=today):
            assert not (entry.get("stale") and entry.get("startable")), entry
            # An absence is named: every entry answers the staleness question.
            assert "stale" in entry


def test_a_task_with_an_unparsable_dedupe_key_is_not_silently_called_fresh(reg):
    """Unknown age must not read the same as known-fresh."""
    atlas = _atlas(reg)
    atlas.set_mode("research")
    reg.create_atlas_task("t-odd", "no-date-here", "drift_breach", {},
                          "desk_rebalance_review")
    entry = atlas.startable_tasks(_facts(), today="2026-08-03")[0]
    assert entry["stale"] is None
    assert entry["startable"] is False
    assert "unknown" in entry["reason"].lower()


def test_the_null_trial_count_is_reachable_from_the_tool_that_runs_the_board():
    """Every other search knob is tunable; this one decides if a verdict exists.

    `run_predictor_board` takes `null_trials`, and below 19 the test cannot
    reach alpha at all, so the trial count is the difference between a board
    that can establish a champion and one that structurally cannot. Exposing
    models/alphas/map_weights/n_splits but not this leaves a caller able to
    tune everything except whether the answer is obtainable.
    """
    import inspect
    from qlab.research.board import run_predictor_board

    assert "null_trials" in inspect.signature(run_predictor_board).parameters
    for module_name in ("qlab.mcp.quant_lab", "qlab.mcp.tui_proxy"):
        module = importlib.import_module(module_name)
        source = inspect.getsource(module)
        assert "null_trials" in source, (
            f"{module_name} runs the board but cannot set null_trials")
