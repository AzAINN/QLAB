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


# The roles behind `desk_rebalance_review`'s phases — a graph no one-role
# harness can walk, and the one that carries the referee.
_REVIEW_ROLES = ("moments-analyst", "challenger", "optimization-runner",
                 "referee", "reporter")


@pytest.fixture(autouse=True)
def _clear_instances(monkeypatch):
    FakeSession.instances = []
    # The real availability check asks PATH for `claude`; the driver's own
    # refusal path is covered separately below.
    monkeypatch.setattr("qlab.tui.claude.resolve_claude_executable",
                        lambda: "/usr/bin/claude")
    monkeypatch.delenv("QLAB_ATLAS_DRIVE", raising=False)
    # `fast` defaults to the operator's env, and a desk with it set would route
    # the judgment roles differently — the recorded rows below would change.
    monkeypatch.delenv("QLAB_LLM_FAST", raising=False)


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


def test_an_ollama_workforce_composes_both_halves_of_its_readiness():
    """E1 refused every non-claude workforce by name. The harness exists now.

    What replaces that refusal is a composition, not a blanket yes: the
    configured provider has to be up for the graphs it serves, and the CLI has
    to be there for the ones it does not — the referee's, and every multi-role
    graph's.
    """
    from qlab.core.llm_config import SurfaceModel

    ollama = SurfaceModel("ollama", "granite3.3:8b")
    up = _driver(workforce=ollama, backend_status=lambda name: (True, "ok"))
    assert up.available() == (True, "")

    down = _driver(workforce=ollama,
                   backend_status=lambda name: (False, "ollama is not running"))
    ok, reason = down.available()
    # The backend's own sentence, not a second opinion composed here.
    assert ok is False and reason == "ollama is not running"

    # The other side of that comparison: a backend with no harness cannot stop
    # a graph it was never going to serve. Refusing the desk because an
    # unusable provider is down would be a refusal about nothing.
    no_harness = _driver(workforce=SurfaceModel("up", "m-1"),
                         backend_status=lambda name: (False, "up is not running"))
    assert no_harness.available() == (True, "")

    # And the same argument one graph in: a stopped daemon must not strand a
    # review the coordinator walks entirely on claude. The gate asks about the
    # provider THIS dispatch uses, which is the half that decides it.
    assert down.available(roles=_REVIEW_ROLES) == (True, "")


def test_claude_roles_still_need_the_cli_under_an_ollama_workforce(monkeypatch):
    from qlab.core.llm_config import SurfaceModel

    monkeypatch.setattr("qlab.tui.claude.resolve_claude_executable", lambda: None)
    driver = _driver(workforce=SurfaceModel("ollama", "granite3.3:8b"),
                     backend_status=lambda name: (True, "ok"))
    # A multi-role graph is walked by the claude coordinator whatever the
    # workforce says, so its absence is still a refusal.
    ok, reason = driver.available(roles=_REVIEW_ROLES)
    assert ok is False and "not on PATH" in reason
    # A one-role graph the harness serves does not need the CLI at all.
    assert driver.available(roles=("news-analyst",)) == (True, "")
    # With no graph named the question is the desk's general readiness, and the
    # honest answer is the strict one: this desk runs both kinds of graph.
    assert driver.available()[0] is False


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
        text = payloads[0]["payload"]["text"]
        # The event bus is a durable table, not a scrollback buffer — and a
        # truncated audit row has to say that it was truncated.
        assert len(text) == 1001 and text.endswith("…")
    finally:
        reg.close()


def test_the_sink_bounds_every_field_it_copies_from_either_producer():
    """A 10KB tool name reached a durable row whole, from both directions.

    The producers were fixed one at a time and the sink bounded nothing at
    all, which is the same per-call-site reasoning that leaked a credential
    twice: the gate belongs where the row is written, and at each boundary a
    foreign string crosses.
    """
    from qlab.operator.ollama_role import RoleEvent
    from qlab.tui.claude import parse_stream_line
    import json as _json

    reg = Registry(":memory:")
    try:
        driver = _driver(reg)
        driver.drive("wf-1", "goal")
        sink = FakeSession.instances[-1].on_event

        # Producer 1: the Claude stream. The tool name and the subagent name
        # are both the model's strings, bounded where they are parsed.
        line = _json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "n" * 10_000,
                                     "input": {"subagent_type": "a" * 10_000}}]},
        })
        claude_event = parse_stream_line(line)[0]
        assert len(claude_event.tool) < 300, "bounded at its own boundary"
        assert len(claude_event.agent) < 300

        # Producer 2: the ollama harness, whose own gate is already per-event.
        sink(RoleEvent(kind="tool", text="t", tool="n" * 10_000, agent="x"))
        # And the sink's own gate, which is what makes the row safe whatever a
        # producer forgot: every field it copies, not the ones it reasoned about.
        sink(Event("text", text="t" * 10_000, tool="n" * 10_000,
                   agent="a" * 10_000))

        rows = [e["payload"] for e in reg.read_events(50)
                if e["kind"] == "atlas_coordinator_event"]
        assert len(rows) == 2
        # `event_kind` and `workflow_id` are checked by the gate too, but they
        # are structurally bounded here — one is an allowlisted literal and the
        # other is the driver's own id — so asserting on them would be a green
        # light that cannot go red. The two foreign fields are the test.
        for row in rows:
            for field in ("agent", "tool"):
                assert len(row[field]) < 300, f"{field} rode the row unbounded"
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


# --- mixed pipelines ----------------------------------------------------------
#
# Mixing is per dispatch, not per phase. The driver builds ONE session for the
# graph it is handed: a one-role graph (templates.news_read) is served by the
# ollama harness, and every multi-role graph is walked by the claude
# coordinator, because nothing else speaks the phase protocol. The referee is
# therefore claude twice over — by the route's own pin, and by the fact that
# every graph carrying it is a graph the harness cannot walk.


class StubDaemon:
    """An Ollama backend that answers once, with a conclusion and no tools.

    `hold` makes the turn block until the test releases it, so a stop can land
    while a turn is genuinely in flight instead of racing it.
    """

    def __init__(self, answer="the record is thin; nothing primary.", hold=False):
        import threading

        self.answer = answer
        self.asked: list[dict] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        if not hold:
            self.release.set()

    def chat(self, messages, model, *, tools=None, timeout=None):
        self.asked.append({"model": model, "tools": tools})
        self.entered.set()
        assert self.release.wait(timeout=5), "the turn was never released"
        return {"role": "assistant", "content": self.answer}


def _ollama_driver(reg, daemon, **over):
    from qlab.core.llm_config import SurfaceModel

    kwargs = dict(workforce=SurfaceModel("ollama", "granite3.3:8b"),
                  registry=reg, backend_factory=lambda name: daemon,
                  backend_status=lambda name: (True, "ok"))
    kwargs.update(over)
    return _driver(reg, **kwargs)


def test_one_desk_runs_a_role_on_the_harness_and_a_graph_on_claude():
    reg, daemon = Registry(":memory:"), StubDaemon()
    try:
        driver = _ollama_driver(reg, daemon)

        # A one-role graph: the harness serves it, on the operator's model.
        assert driver.drive("wf-news", "read the week",
                            roles=("news-analyst",))["driving"] is True
        assert FakeSession.instances == [], "claude was spawned for a role it does not serve"
        driver._session.join(timeout=5)
        assert daemon.asked and daemon.asked[0]["model"] == "granite3.3:8b"

        # A multi-role graph on the same desk: the coordinator walks it.
        assert driver.drive("wf-review", "full review",
                            roles=_REVIEW_ROLES)["driving"] is True
        assert len(FakeSession.instances) == 1
        assert FakeSession.instances[-1].governed is True

        invocations = reg.list_model_invocations()
        rows = {row["role"]: row for row in invocations}
        # One row per role and no more: the runner records its own, so a driver
        # that also recorded the harness path would double-count it.
        assert len(invocations) == 1 + len(_REVIEW_ROLES)
        # The role that ran locally names the provider that served it...
        assert rows["news-analyst"]["backend"] == "ollama"
        assert rows["news-analyst"]["resolved_model"] == "granite3.3:8b"
        # ...and every role of the graph claude walked says claude, including
        # the ones the operator's workforce would otherwise have moved. Each
        # carries the reason it did not move.
        for role in _REVIEW_ROLES:
            assert rows[role]["backend"] == "claude_cli", role
            assert rows[role]["fallback_reason"], role
        assert "pinned to claude" in rows["referee"]["fallback_reason"]
        assert rows["referee"]["resolved_model"] == "inherit"
        assert "one role per session" in rows["reporter"]["fallback_reason"]
        kinds = [e["kind"] for e in reg.read_events(200)]
        assert "model.route_pinned" in kinds
        # Nothing in this run fell back, so nothing says it did.
        assert "model.fallback_used" not in kinds
    finally:
        reg.close()


def test_the_gate_never_reaches_the_harness_even_alone_in_a_graph():
    """The referee's two independent claims to claude, one of them alone.

    A one-role graph is the shape the harness serves, so a referee-only
    dispatch is the case where "every graph carrying it is multi-role" stops
    being the reason and the route's own pin has to be.
    """
    reg, daemon = Registry(":memory:"), StubDaemon()
    try:
        driver = _ollama_driver(reg, daemon)
        assert driver.drive("wf-gate", "check it",
                            roles=("referee",))["driving"] is True
        assert len(FakeSession.instances) == 1, "the gate reached the harness"
        assert daemon.asked == []
        row = reg.list_model_invocations()[0]
        assert row["role"] == "referee" and row["backend"] == "claude_cli"
        # The plan's own sentence, which `resolve_route` then overrides with
        # the identical one — so this branch has no downstream observable and
        # would drift unnoticed. A 1-phase graph is exactly what the harness
        # serves; naming the graph here would contradict the row above.
        from qlab.operator.model_routing import pinned_to_claude_reason

        assert driver._plan(("referee",)).pinned_reason == \
            pinned_to_claude_reason("referee", "ollama")
    finally:
        reg.close()


def test_the_recorded_row_names_the_model_the_cli_was_configured_with():
    """The row is a claim about what the coordinator was given, so pin it.

    `build_workforce_agents` is the authority for the claude path's models;
    this driver resolves the same routes independently to record them. Two
    computations of one fact drift, so their agreement is asserted rather than
    assumed — including for a future agent file that names a concrete model.
    """
    from qlab.agents.loader import load_agents
    from qlab.operator.model_routing import resolve_route
    from qlab.tui.claude import _routed_model

    sources = {source.name: source.model for source in load_agents()}
    for role in _REVIEW_ROLES:
        for fast in (False, True):
            assert _routed_model(role, sources[role], fast=fast) == \
                resolve_route(role, fast=fast).resolved_model, role


def test_a_cooperative_stop_still_refuses_the_next_dispatch():
    """The harness cannot be killed mid-turn, only asked to stop.

    So `running` stays True for the in-flight window, and a driver that
    reopened its slot on stop() would have a second session spawned against a
    runtime that is going away. `_closed` is what makes that unreachable, and
    the composition — not either half — is the guarantee.
    """
    reg, daemon = Registry(":memory:"), StubDaemon(hold=True)
    try:
        driver = _ollama_driver(reg, daemon)
        driver.drive("wf-news", "read the week", roles=("news-analyst",))
        session = driver._session
        assert daemon.entered.wait(timeout=5), "the turn never started"
        driver.stop("owner shutting down")
        # Cooperative: the runner is asked, never signalled, so it is still
        # running with a turn in flight at exactly this moment.
        assert session._stopped and session.running is True
        out = driver.drive("wf-2", "goal", roles=("news-analyst",))
        assert out["driving"] is False and "shutting down" in out["reason"]
    finally:
        daemon.release.set()
        session.join(timeout=5)
        reg.close()


# --- one dispatch, one workforce ----------------------------------------------
#
# The owner reassigns `driver.workforce` on EVERY access of its
# `coordinator_driver` property — including the snapshot poll, unlocked and
# without ever taking the driver's lock. So a POST /api/llm can land while a
# dispatch is between its plan and its recorded rows. `drive` captures the
# surface once for exactly that reason; these are the two readers a later
# reading used to reach.


def test_a_workforce_change_mid_dispatch_cannot_rewrite_the_rows():
    """The rows name the surface the dispatch ran on, not the one now set.

    Before this, `_plan` and `_record_routes` were two reads of a mutable
    attribute: an operator switching the picker while the coordinator spawned
    made every unpinned role's invocation row claim backend=ollama for a role
    the Claude coordinator actually ran. An audit row that names a provider
    that served nothing is worse than no row.
    """
    from qlab.core.llm_config import SurfaceModel

    reg = Registry(":memory:")
    other = SurfaceModel("ollama", "granite3.3:8b")
    try:
        driver = None

        def flipping_factory(*args, **kwargs):
            # Stands in for a POST /api/llm landing between the plan and the
            # rows — the property write the owner makes, at the moment it can
            # do damage.
            driver.workforce = other
            return FakeSession(*args, **kwargs)

        driver = _driver(reg, registry=reg, session_factory=flipping_factory)
        assert driver.workforce is None, "this desk starts unconfigured"
        assert driver.drive("wf-flip", "full review",
                            roles=_REVIEW_ROLES)["driving"] is True
        assert driver.workforce is other, "the flip never landed"

        rows = {row["role"]: row for row in reg.list_model_invocations()}
        assert len(rows) == len(_REVIEW_ROLES)
        for role in _REVIEW_ROLES:
            # The claude coordinator walked all five, so all five say so.
            assert rows[role]["backend"] == "claude_cli", role
            assert rows[role]["resolved_model"] != other.model, role
            # An unconfigured desk pins nothing: a reason here would mean the
            # rows were derived from a workforce this dispatch never had.
            assert not rows[role]["fallback_reason"], role

        # ...and the capture is per dispatch, not a freeze. The next one reads
        # the surface the operator actually left set, and says the graph could
        # not follow it.
        driver.stop("done")
        driver._closed = False           # stop() is terminal by design
        assert driver.drive("wf-next", "full review",
                            roles=_REVIEW_ROLES)["driving"] is True
        # Ordered by a shared-second timestamp, so compared as a set: one row
        # per dispatch, and only the second one carries the operator's choice.
        reasons = [row["fallback_reason"] or "" for row
                   in reg.list_model_invocations() if row["role"] == "reporter"]
        assert len(reasons) == 2 and "" in reasons
        assert any("one role per session" in reason for reason in reasons)
    finally:
        reg.close()


def test_a_reader_handed_a_surface_answers_about_that_surface():
    """The two readers whose divergence has no seam a test can hook.

    `available` and `_plan` are both called from inside one dispatch, back to
    back, so no injectable callback fires between the capture and their reads
    — a flip there is real in the owner's threads but unreachable from a
    scripted run. What is testable is the property the capture depends on:
    each reader answers about the surface it is HANDED, and still reads the
    live attribute when handed nothing (the snapshot path's whole contract).
    """
    from qlab.core.llm_config import SurfaceModel

    other = SurfaceModel("ollama", "granite3.3:8b")
    driver = _driver(workforce=None,
                     backend_status=lambda name: (False, "ollama is not running"))

    assert driver._plan(_REVIEW_ROLES, workforce=other).pinned_reason
    assert driver._plan(_REVIEW_ROLES).pinned_reason == "", "the live read broke"
    # A down daemon is a refusal for the graph `other` would serve...
    assert driver.available(roles=("news-analyst",), workforce=other) == \
        (False, "ollama is not running")
    # ...and no refusal at all for the unconfigured desk this driver still is.
    assert driver.available(roles=("news-analyst",)) == (True, "")


def test_a_workforce_cleared_mid_dispatch_does_not_break_the_harness_run():
    """The other reader: the model the harness session is built with.

    `_build_session` read the live surface for its model, so a picker cleared
    between the plan and the session construction turned a valid ollama
    dispatch into `coordinator failed to start` — and a picker *switched*
    would have handed the daemon a model belonging to another backend.
    """
    reg, daemon = Registry(":memory:"), StubDaemon()
    session = None
    try:
        driver = None

        def flipping_backend(name):
            # `_build_backend` is called while the session's arguments are
            # evaluated, immediately before the model is read.
            driver.workforce = None
            return daemon

        driver = _ollama_driver(reg, daemon, backend_factory=flipping_backend)
        out = driver.drive("wf-news", "read the week", roles=("news-analyst",))
        assert out["driving"] is True, out["reason"]
        assert driver.workforce is None, "the flip never landed"
        session = driver._session
        session.join(timeout=5)
        # The dispatch ran on the model it planned with, not on whatever the
        # attribute held by the time the arguments were assembled.
        assert daemon.asked and daemon.asked[0]["model"] == "granite3.3:8b"
    finally:
        if session is not None:
            session.join(timeout=5)
        reg.close()


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
        # The template's graph reached the driver as roles, and the routes the
        # coordinator was configured with are on the record. Without the roles
        # this dispatch would look identical and audit to nothing.
        roles = {row["role"] for row in session.registry.list_model_invocations()}
        assert {"moments-analyst", "referee", "reporter"} <= roles
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
