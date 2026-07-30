"""The owner drives its own coordinator (closing the autonomous loop).

Atlas could dispatch a workflow but nothing walked its phases, so an
unattended tick produced a run parked at phase one with no error anywhere.
These cover the driver's boundaries: it starts one coordinator, refuses a
second, reports every refusal, and never turns a refusal into a task failure.
"""

from __future__ import annotations

import pytest

from qlab.operator.coordinator import CoordinatorDriver, resume_prompt
from qlab.state.registry import Registry


class FakeSession:
    """A ClaudeSession stand-in: the driver is testable without a real CLI."""

    instances: list["FakeSession"] = []

    def __init__(self, on_event, *, cwd=None, runtime_url="", offline=True,
                 fast=None):
        self.on_event = on_event
        self.cwd = cwd
        self.runtime_url = runtime_url
        self.offline = offline
        self.fast = fast
        self.running = False
        self.prompt = ""
        self.governed = None
        self.last_error = ""
        self.stopped = False
        self.start_result = True
        FakeSession.instances.append(self)

    def start(self, prompt, *, governed=False, **_):
        self.prompt = prompt
        self.governed = governed
        self.running = bool(self.start_result)
        return self.start_result

    def stop(self):
        self.stopped = True
        self.running = False


class Event:
    def __init__(self, kind, text="", agent="", tool=""):
        self.kind, self.text, self.agent, self.tool = kind, text, agent, tool


@pytest.fixture(autouse=True)
def _clear_instances(monkeypatch):
    FakeSession.instances = []
    # The real availability check asks PATH for `claude`; the driver's own
    # refusal path is covered separately below.
    monkeypatch.setattr("qlab.tui.claude.resolve_claude_executable",
                        lambda: "/usr/bin/claude")
    monkeypatch.delenv("QLAB_ATLAS_DRIVE", raising=False)


def _driver(reg=None, **over):
    kwargs = dict(runtime_url="http://127.0.0.1:9999",
                  record_event=reg.record_event if reg else None,
                  session_factory=FakeSession)
    kwargs.update(over)
    return CoordinatorDriver(**kwargs)


def test_driving_starts_a_governed_coordinator_on_the_owners_url():
    driver = _driver()
    out = driver.drive("wf-1", "regime re-read")
    assert out["driving"] is True and out["reason"] == ""
    session = FakeSession.instances[-1]
    # governed=True is the whole authority story: the allowlisted five roles,
    # no filesystem, no shell, no execution tool.
    assert session.governed is True
    assert session.runtime_url == "http://127.0.0.1:9999"
    assert driver.busy is True and driver.current_workflow_id == "wf-1"


def test_the_prompt_names_the_existing_workflow():
    # Without the id the coordinator opens a second workflow for one trigger,
    # and then two runs compete for a single referee verdict.
    prompt = resume_prompt("wf-7", "risk event")
    assert "RESUME_WORKFLOW_ID: wf-7" in prompt
    assert "do not create a new workflow" in prompt
    driver = _driver()
    driver.drive("wf-7", "risk event")
    assert "RESUME_WORKFLOW_ID: wf-7" in FakeSession.instances[-1].prompt


def test_a_second_dispatch_is_refused_while_one_is_running():
    driver = _driver()
    driver.drive("wf-1", "first")
    second = driver.drive("wf-2", "second")
    assert second["driving"] is False
    assert "already driving workflow wf-1" in second["reason"]
    # Refused, never queued: N concurrent Claude trees on one desk is a cost
    # incident, not autonomy.
    assert len(FakeSession.instances) == 1


def test_a_finished_coordinator_frees_the_slot():
    driver = _driver()
    driver.drive("wf-1", "first")
    FakeSession.instances[-1].running = False
    assert driver.busy is False and driver.current_workflow_id == ""
    assert driver.drive("wf-2", "second")["driving"] is True


def test_drive_disabled_by_env_reports_why():
    driver = _driver()
    import os
    os.environ["QLAB_ATLAS_DRIVE"] = "0"
    try:
        out = driver.drive("wf-1", "goal")
    finally:
        del os.environ["QLAB_ATLAS_DRIVE"]
    assert out["driving"] is False
    assert "QLAB_ATLAS_DRIVE=0" in out["reason"]
    assert FakeSession.instances == []


def test_a_missing_claude_cli_is_a_reported_refusal_not_a_crash(monkeypatch):
    monkeypatch.setattr("qlab.tui.claude.resolve_claude_executable",
                        lambda: None)
    driver = _driver()
    out = driver.drive("wf-1", "goal")
    # The workflow stays registered and resumable by hand; the dispatch itself
    # must not fail just because this desk has no CLI installed.
    assert out["driving"] is False
    assert "not on PATH" in out["reason"]


def test_a_session_that_refuses_to_start_surfaces_its_own_error():
    driver = _driver()
    original = FakeSession.__init__

    def failing_init(self, *a, **kw):
        original(self, *a, **kw)
        self.start_result = False
        self.last_error = "Claude Code is not available on PATH."

    FakeSession.__init__ = failing_init
    try:
        out = driver.drive("wf-1", "goal")
    finally:
        FakeSession.__init__ = original
    assert out["driving"] is False
    assert out["reason"] == "Claude Code is not available on PATH."
    assert driver.busy is False


def test_a_raising_factory_is_reported_not_propagated():
    def boom(*a, **kw):
        raise RuntimeError("no tempdir")

    driver = _driver(session_factory=boom)
    out = driver.drive("wf-1", "goal")
    assert out["driving"] is False and "no tempdir" in out["reason"]


# --- the visibility half ------------------------------------------------------


def test_coordinator_events_reach_the_owner_audit_bus():
    reg = Registry(":memory:")
    try:
        driver = _driver(reg)
        driver.drive("wf-1", "regime re-read")
        session = FakeSession.instances[-1]
        session.on_event(Event("agent", "moments-analyst starting",
                               agent="moments-analyst"))
        session.on_event(Event("tool", "moments_estimate", tool="moments_estimate"))
        kinds = [e["kind"] for e in reg.read_events(50)]
        assert "atlas_coordinator_started" in kinds
        # This is what makes an unattended run legible instead of a black box.
        assert kinds.count("atlas_coordinator_event") == 2
    finally:
        reg.close()


def test_recorded_event_text_is_bounded():
    reg = Registry(":memory:")
    try:
        driver = _driver(reg)
        driver.drive("wf-1", "goal")
        FakeSession.instances[-1].on_event(Event("text", "x" * 5000))
        payloads = [e for e in reg.read_events(50)
                    if e["kind"] == "atlas_coordinator_event"]
        # The event bus is a durable table, not a scrollback buffer.
        assert len(payloads[0]["payload"]["text"]) == 1000
    finally:
        reg.close()


def test_a_recording_failure_does_not_break_the_coordinator():
    def explode(kind, payload):
        raise RuntimeError("registry gone")

    driver = _driver(record_event=explode)
    assert driver.drive("wf-1", "goal")["driving"] is True
    FakeSession.instances[-1].on_event(Event("text", "still fine"))


def test_stop_terminates_a_running_coordinator():
    driver = _driver()
    driver.drive("wf-1", "goal")
    session = FakeSession.instances[-1]
    driver.stop("owner stopped")
    assert session.stopped is True and driver.busy is False


def test_stop_on_an_idle_driver_is_safe_and_still_closes_it():
    # stop() is the owner's shutdown hook. An owner that is going away must not
    # allow a new coordinator just because none happened to be running when it
    # started shutting down.
    driver = _driver()
    driver.stop()          # must not raise
    out = driver.drive("wf-1", "goal")
    assert out["driving"] is False and "shutting down" in out["reason"]
    assert FakeSession.instances == []


# --- the owner seam -----------------------------------------------------------


def test_dispatch_drives_the_workflow_it_registered(monkeypatch):
    """The bug this closes: a dispatch that registered a workflow and stopped.

    Registering is not running. Phases advance only when a coordinator walks
    them, so before this an autonomous tick left a run parked at phase one with
    no error anywhere — the desk looked like a wrapper doing nothing.
    """
    from qlab.ui.server import UISession

    monkeypatch.setattr("qlab.tui.claude.ClaudeSession", FakeSession)
    session = UISession(offline_default=True, registry=Registry(":memory:"))
    try:
        session.atlas.set_mode("research")
        facts = session.atlas_facts(True)
        facts["regime"]["flip"] = True
        out = session.atlas.observe(facts, trading_date="2020-01-02")
        task_id = out["created_tasks"][0]["task_id"]

        result = session.atlas.start_task(
            task_id, facts, runner=session.atlas_workflow_runner)
        assert result["started"] is True
        assert FakeSession.instances, "the dispatch registered but drove nothing"
        driven = FakeSession.instances[-1]
        assert driven.governed is True
        # Pointed at this owner, so the coordinator reaches the registry over
        # HTTP rather than opening it as a second writer.
        assert driven.runtime_url == f"http://127.0.0.1:{session.port}"
        # And it resumes the workflow Atlas registered instead of opening a
        # second one for the same trigger.
        workflow_id = session.registry.list_workflows(5)[0]["workflow_id"]
        assert f"RESUME_WORKFLOW_ID: {workflow_id}" in driven.prompt
    finally:
        session.registry.close()


def test_an_undriven_dispatch_still_dispatches_and_says_why(monkeypatch):
    from qlab.ui.server import UISession

    monkeypatch.setattr("qlab.tui.claude.resolve_claude_executable",
                        lambda: None)
    session = UISession(offline_default=True, registry=Registry(":memory:"))
    try:
        session.atlas.set_mode("research")
        facts = session.atlas_facts(True)
        facts["regime"]["flip"] = True
        out = session.atlas.observe(facts, trading_date="2020-01-02")
        task_id = out["created_tasks"][0]["task_id"]
        result = session.atlas.start_task(
            task_id, facts, runner=session.atlas_workflow_runner)
        # The workflow is registered and resumable by hand. A desk with no CLI
        # installed must degrade to the old behaviour, not fail the task.
        assert result["started"] is True
        assert session.registry.list_workflows(5)
        status = session.coordinator_status()
        assert status["can_drive"] is False and "not on PATH" in status["reason"]
    finally:
        session.registry.close()


def test_atlas_status_reports_whether_claude_is_actually_working():
    from qlab.ui.server import UISession, handle_api

    session = UISession(offline_default=True, registry=Registry(":memory:"))
    try:
        status, out = handle_api(session, "GET", "/api/atlas/status", {}, {})
        assert status == 200
        # "A workflow exists" and "Claude is walking its phases" are different
        # facts, and only the second answers "is it working?".
        assert "coordinator" in out and "driving" in out["coordinator"]
        coord = out["coordinator"]
        # The property worth holding: a refusal is never silent. Either it can
        # drive, or it says why not — never False with an empty reason, which is
        # exactly the shape that read as a black box.
        assert coord["can_drive"] is True or coord["reason"]
        assert out["autonomous"] is True
    finally:
        session.registry.close()


# --- fast mode reachability ---------------------------------------------------


def test_fast_mode_is_reachable_and_exempts_the_referee(monkeypatch):
    """Fast mode existed in model_routing but nothing called it with fast=True.

    Routing tables that no surface reaches are the same latent-dead-code shape
    as an unreachable adjudicator: configurable on paper, inert in practice.
    """
    from qlab.tui.claude import _routed_model, build_workforce_agents

    # Judgment roles drop to the quick model...
    assert _routed_model("moments-analyst", "inherit", fast=True) == "sonnet"
    # ...but the approval gate never does. A PASS must never mean "passed on
    # the fast model".
    assert _routed_model("referee", "inherit", fast=True) == \
        _routed_model("referee", "inherit", fast=False)
    # An explicit model in the agent source still wins over both.
    assert _routed_model("moments-analyst", "opus", fast=True) == "opus"

    fast_agents = build_workforce_agents("rebalance", fast=True)
    slow_agents = build_workforce_agents("rebalance", fast=False)
    assert fast_agents["referee"]["model"] == slow_agents["referee"]["model"]
    changed = [name for name in fast_agents
               if fast_agents[name]["model"] != slow_agents[name]["model"]]
    assert changed, "fast mode changed no role's model — it is still inert"
    assert "referee" not in changed


def test_fast_mode_env_switch_is_off_by_default(monkeypatch):
    from qlab.tui.claude import fast_mode_enabled

    monkeypatch.delenv("QLAB_LLM_FAST", raising=False)
    # Off by default: the deep tier is what makes a review worth reading.
    assert fast_mode_enabled() is False
    monkeypatch.setenv("QLAB_LLM_FAST", "1")
    assert fast_mode_enabled() is True


def test_the_driver_passes_fast_through_to_the_session():
    driver = _driver(fast=True)
    driver.drive("wf-1", "goal")
    assert FakeSession.instances[-1].fast is True


# --- the operator-facing switch -----------------------------------------------


def test_fast_mode_is_togglable_at_runtime_and_reaches_the_next_dispatch():
    """The switch existed only as an env var read before launch.

    A setting you can only change by restarting is not a setting the desk owns.
    """
    from qlab.ui.server import UISession, handle_api

    session = UISession(offline_default=True, registry=Registry(":memory:"))
    try:
        assert session.fast_mode is False
        status, out = handle_api(session, "POST", "/api/workforce/fast", {},
                                 {"enabled": True})
        assert status == 200 and out["fast"] is True
        # The exemption is stated, not just applied: an operator turning this on
        # needs to know what it does not cheapen.
        assert out["exempt_roles"] == ["referee"]
        # Reaches the driver without an owner restart.
        assert session.coordinator_driver.fast is True
        assert session.tui_snapshot(True)["atlas_heartbeat"]["fast"] is True

        assert handle_api(session, "POST", "/api/workforce/fast", {},
                          {"enabled": False})[1]["fast"] is False
        assert session.coordinator_driver.fast is False
    finally:
        session.registry.close()


def test_fast_mode_route_rejects_a_non_boolean():
    from qlab.ui.server import UISession, handle_api

    session = UISession(offline_default=True, registry=Registry(":memory:"))
    try:
        status, out = handle_api(session, "POST", "/api/workforce/fast", {},
                                 {"enabled": "yes"})
        assert status == 400 and "true or false" in out["error"]
    finally:
        session.registry.close()


# --- concurrency ---------------------------------------------------------------
#
# The owner is a ThreadingHTTPServer with a heartbeat thread beside it, so every
# guarantee here is a concurrent guarantee or it is not one.


def test_the_owner_hands_out_exactly_one_driver_under_concurrency():
    """A lazy build without a lock handed out one driver per caller.

    Each had its own lock and its own session slot, which is precisely the
    "one coordinator at a time" guarantee gone: N threads, N Claude trees.
    """
    import threading

    from qlab.ui.server import UISession

    session = UISession(offline_default=True, registry=Registry(":memory:"))
    try:
        seen, barrier = [], threading.Barrier(16)

        def grab():
            barrier.wait()          # maximise the overlap
            seen.append(id(session.coordinator_driver))

        threads = [threading.Thread(target=grab) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(set(seen)) == 1
    finally:
        session.registry.close()


def test_concurrent_dispatch_spawns_exactly_one_coordinator():
    import threading
    import time

    spawned, guard = [], threading.Lock()

    class Slow(FakeSession):
        def start(self, prompt, *, governed=False, **kw):
            time.sleep(0.02)        # spawning a real Claude tree is not instant
            with guard:
                spawned.append(1)
            return super().start(prompt, governed=governed, **kw)

    driver = _driver(session_factory=Slow)
    barrier = threading.Barrier(12)

    def go(i):
        barrier.wait()
        driver.drive(f"wf-{i}", "goal")

    threads = [threading.Thread(target=go, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(spawned) == 1


def test_shutdown_is_terminal_so_no_tree_outlives_the_owner():
    """stop() released the slot before the process was gone.

    A dispatch landing in that window spawned a second coordinator — and when
    the stop came from owner shutdown, that second tree outlived the runtime it
    was talking to and kept billing against a dead URL.
    """
    import threading
    import time

    spawned, guard = [], threading.Lock()

    class Slow(FakeSession):
        def start(self, prompt, *, governed=False, **kw):
            time.sleep(0.02)
            with guard:
                spawned.append(1)
            return super().start(prompt, governed=governed, **kw)

        def stop(self):
            time.sleep(0.02)        # teardown is not instant either
            super().stop()

    driver = _driver(session_factory=Slow)
    driver.drive("wf-a", "goal")
    spawned.clear()

    t1 = threading.Thread(target=lambda: driver.stop("owner stopped"))
    t2 = threading.Thread(target=lambda: (time.sleep(0.005),
                                          driver.drive("wf-b", "goal")))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert spawned == []

    # And it stays refused afterwards, with a reason.
    out = driver.drive("wf-c", "goal")
    assert out["driving"] is False and "shutting down" in out["reason"]
