"""The Ollama role harness: a local model arguing through the owner's tools.

Fully offline in both directions. The daemon is a scripted stand-in — a list of
assistant messages, one per ``/api/chat`` turn — and the owner is a real
``ThreadingHTTPServer`` on an ephemeral loopback port, so the tests drive the
route the harness actually crosses rather than a patched function (B1's lesson:
constructor-level tests missed every shape of the URL family).

No test reaches a live Ollama, none opens ``.lab/registry.duckdb``, and none
bills a token.
"""

from __future__ import annotations

import inspect
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from qlab.operator import ollama_role
from qlab.operator.coordinator import _RECORDED_KINDS, CoordinatorDriver
from qlab.operator.llm_backends import OllamaBackend
from qlab.operator.ollama_role import (
    DEADLINE_S,
    MAX_TURNS,
    OllamaRoleError,
    OllamaRoleRunner,
    session_factory,
)
from qlab.state.registry import Registry

# One row is enough: these tests are about the harness's boundaries, not about
# what the registry returns.
_OWNER_RESULT = json.dumps({"result": [{"decision_id": "dec-1", "kind": "news"}]})


# -- doubles ------------------------------------------------------------------

@pytest.fixture
def owner():
    """The owner runtime's ``POST /api/lab/<tool>`` route on a loopback port.

    It answers any POST path, so one server can also stand in for a daemon's
    ``/api/chat`` when a test wants the whole wire rather than a double:
    ``replies`` is a fixed answer per path, ``script`` a queue consumed in
    order.
    """
    seen: list[dict] = []
    replies: dict[str, tuple[int, str]] = {}
    script: dict[str, list[tuple[int, str]]] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):        # keep pytest output clean
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            seen.append({"path": self.path, "body": json.loads(raw)})
            queued = script.get(self.path)
            if queued:
                status, body = queued.pop(0)
            else:
                status, body = replies.get(self.path, (200, _OWNER_RESULT))
            out = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            url=f"http://127.0.0.1:{httpd.server_address[1]}",
            seen=seen, replies=replies, script=script)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


class Clock:
    """A wall clock the script moves, so a deadline test needs no sleep.

    ``step`` advances it on every reading, which is how a test makes time pass
    *inside* one turn — where a per-turn check cannot see it.
    """

    def __init__(self, step: float = 0.0) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


class ScriptedOllama:
    """A daemon that answers each turn from a fixed script."""

    name = "ollama"

    def __init__(self, *replies, clock: Clock | None = None,
                 per_turn: float = 0.0):
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.clock = clock
        self.per_turn = per_turn

    def chat(self, messages, model, *, tools=None, timeout=None):
        self.calls.append({"messages": [dict(m) for m in messages],
                           "tools": tools, "timeout": timeout, "model": model})
        if self.clock is not None:
            self.clock.now += self.per_turn
        if not self.replies:
            raise AssertionError(
                "the harness asked for more turns than the script holds")
        return self.replies.pop(0)


def _assistant(content="", *calls):
    """One assistant message in Ollama's shape (verified against a live 0.31.2).

    A call given as a dict is passed through untouched, so a test can hand the
    harness a shape a well-behaved daemon would never send.
    """
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [
            call if isinstance(call, dict)
            else {"function": {"name": call[0], "arguments": call[1]}}
            for call in calls]
    return message


def _recent(**args):
    return ("registry_recent_decisions", args or {"limit": 3})


def _runner(daemon, owner, events=None, **over):
    kwargs = dict(backend=daemon, model="granite3.3:8b", role="news-analyst",
                  owner_url=owner.url)
    kwargs.update(over)
    return OllamaRoleRunner(
        (events.append if events is not None else None), **kwargs)


# -- the happy path -----------------------------------------------------------

def test_a_tool_call_reaches_the_owner_and_a_plain_reply_ends_the_session(owner):
    daemon = ScriptedOllama(
        _assistant("", _recent(kind="news_analyst", limit=3)),
        _assistant("The record is thin: one publisher, nothing primary."),
    )
    registry = Registry(":memory:")
    events: list = []
    out = _runner(daemon, owner, events, registry=registry).run("Read the week.")

    assert "record is thin" in out
    # The owner saw the call on the route the combined server would have used,
    # with the session's offline flag attached the way the proxy attaches it.
    assert owner.seen[0]["path"] == "/api/lab/registry.recent_decisions"
    assert owner.seen[0]["body"] == {
        "kind": "news_analyst", "limit": 3, "offline": True}
    # Only the role's own two tools were ever declared to the model.
    declared = {t["function"]["name"] for t in daemon.calls[0]["tools"]}
    assert declared == {"registry_recent_decisions", "registry_log_decision"}
    # The answer went back as a tool message, so the model saw its own result.
    fed_back = daemon.calls[1]["messages"][-1]
    assert fed_back["role"] == "tool"
    assert fed_back["tool_name"] == "registry_recent_decisions"
    assert "dec-1" in fed_back["content"]
    kinds = [e.kind for e in events]
    assert kinds[0] == "session" and kinds[-1] == "result"
    assert "tool_start" in kinds
    # The invocation row names the provider that actually served the role.
    row = registry.list_model_invocations()[0]
    assert row["backend"] == "ollama" and row["role"] == "news-analyst"
    assert row["resolved_model"] == "granite3.3:8b" and row["status"] == "ok"
    # A fact about the desk, not about this run: `news-analyst` is a registered
    # workflow phase (templates.news_read) with no entry in ROLE_TIER, so every
    # route it takes carries a note. E2 pinned that note being published as
    # `model.fallback_used` although nothing fell back; E3 split the kind off a
    # structural note, and this is the honest half of that pin.
    assert row["fallback_reason"] == "role 'news-analyst' has no configured tier"
    kinds = [e["kind"] for e in registry.read_events(20)]
    assert "model.route_unregistered" in kinds and "model.fallback_used" not in kinds


# -- the authority boundary ---------------------------------------------------

def test_a_tool_off_the_roles_allowlist_is_refused_and_the_loop_continues(owner):
    """The allowlist is the boundary, and a refusal is a turn, not a crash."""
    daemon = ScriptedOllama(
        # Two calls in one turn, which a real daemon does: one asking for a
        # tool this role does not hold, one whose shape is not a tool call.
        _assistant("", ("algorithms_solve", {"objective_id": "o1",
                                             "algorithm_id": "a1"}),
                   {"function": "execute_plan"}),
        _assistant("I have no optimizer; the record alone says nothing new."),
    )
    events: list = []
    out = _runner(daemon, owner, events).run("Read the week.")

    assert "record alone" in out
    # Nothing reached the owner: the gate is above the HTTP call, not beside it.
    assert owner.seen == []
    results = [m for m in daemon.calls[1]["messages"] if m["role"] == "tool"]
    assert len(results) == 2
    assert "allowlist" in results[0]["content"]
    assert "algorithms_solve" in results[0]["content"]
    assert results[1]["content"].startswith("REFUSED:")
    # And the refusal is on the audit stream, not only in the model's context.
    noted = [e.text for e in events if e.kind == "tool_result"]
    assert any("algorithms_solve" in text and "allowlist" in text
               for text in noted)


def test_a_reply_with_neither_a_conclusion_nor_a_tool_call_is_a_loud_failure(
        owner):
    daemon = ScriptedOllama(_assistant("   "))
    with pytest.raises(OllamaRoleError) as exc:
        _runner(daemon, owner).run("Read the week.")
    assert "nothing to report" in str(exc.value)


def test_the_approval_gate_cannot_be_asked_to_run_on_ollama(owner):
    """A3's pin, read where a runner is built rather than re-declared here."""
    daemon = ScriptedOllama(_assistant("no."))
    with pytest.raises(ValueError) as exc:
        _runner(daemon, owner, role="referee")
    assert "referee" in str(exc.value) and "pinned to claude" in str(exc.value)
    # The same construction for the piloted role is fine.
    assert _runner(daemon, owner).role == "news-analyst"


def test_a_role_whose_grants_the_harness_cannot_declare_is_refused(owner):
    """Silently narrowing a role's authority is worse than refusing to run it."""
    daemon = ScriptedOllama(_assistant("no."))
    with pytest.raises(ValueError) as exc:
        _runner(daemon, owner, role="moments-analyst")
    assert "no schema for" in str(exc.value)


# -- foreign input ------------------------------------------------------------

@pytest.mark.parametrize("bad, expected", [
    ({"limit": "three"}, "limit"),                 # wrong type
    ({"limit": True}, "boolean"),                  # bool is an int in Python
    ({"since": "2026-01-01"}, "since"),            # not an argument at all
    ({"limit": 5000}, "50"),                       # past the declared ceiling
    ({"limit": 0}, "at least 1"),                  # and below its floor
])
def test_arguments_that_fail_the_schema_never_reach_the_owner(owner, bad,
                                                              expected):
    daemon = ScriptedOllama(
        _assistant("", ("registry_recent_decisions", bad)),
        _assistant("I could not read the record."),
    )
    out = _runner(daemon, owner).run("Read the week.")

    assert "could not read" in out
    assert owner.seen == []
    complaint = daemon.calls[1]["messages"][-1]
    assert complaint["role"] == "tool" and expected in complaint["content"]


def test_a_required_argument_that_is_missing_is_named_rather_than_guessed(owner):
    daemon = ScriptedOllama(
        _assistant("", ("registry_log_decision", {"kind": "news_analyst"})),
        _assistant("I logged nothing."),
    )
    _runner(daemon, owner).run("Log your read.")
    assert owner.seen == []
    complaint = daemon.calls[1]["messages"][-1]["content"]
    assert "as_of" in complaint and "rationale" in complaint


def test_what_the_owner_says_is_bounded_and_redacted_on_its_way_to_an_event(owner):
    """The model gets the record; the audit row gets a bounded, safe excerpt."""
    secret = "https://desk:s3cr3t@feed.internal/wire"
    owner.replies["/api/lab/registry.recent_decisions"] = (200, json.dumps(
        {"result": [{"rationale": secret + " " + "x" * 900}]}))
    daemon = ScriptedOllama(
        _assistant("", _recent()), _assistant("Read."))
    events: list = []
    _runner(daemon, owner, events).run("Read the week.")

    answered = [e.text for e in events if e.kind == "tool_result"
                and "answered" in e.text][0]
    assert "s3cr3t@" not in answered and len(answered) <= 241
    # The model is not the audit row: it still sees the whole record it asked
    # for, because a bounded tool result is a wrong answer rather than a safe one.
    fed_back = daemon.calls[1]["messages"][-1]["content"]
    assert secret in fed_back and len(fed_back) > 900


def test_a_tool_name_the_model_invented_is_bounded_in_the_recorded_row(
        owner, monkeypatch):
    """The reviewer's probe, asserted where the string lands: the event row.

    ``CoordinatorDriver._on_event`` copies ``tool`` whole into a durable
    ``atlas_coordinator_event`` payload, so a field this module leaves
    unbounded is a 10KB row per call.
    """
    monkeypatch.setattr("qlab.tui.claude.resolve_claude_executable",
                        lambda: "/usr/bin/claude")
    monkeypatch.delenv("QLAB_ATLAS_DRIVE", raising=False)
    daemon = ScriptedOllama(
        _assistant("", ("x" * 10_000, {})), _assistant("Nothing to read."))
    recorded: list[tuple[str, dict]] = []
    driver = CoordinatorDriver(
        runtime_url=owner.url,
        record_event=lambda kind, payload: recorded.append((kind, payload)),
        session_factory=session_factory(backend=daemon, model="granite3.3:8b",
                                        role="news-analyst"))
    assert driver.drive("wf-1", "read the record")["driving"] is True
    driver._session.join(timeout=10)

    tools = [p["tool"] for kind, p in recorded
             if kind == "atlas_coordinator_event" and p["tool"]]
    assert tools, "the refused call was never recorded"
    assert all(len(name) <= 241 for name in tools)


def test_an_owner_answer_too_large_to_reason_over_is_refused_not_truncated(owner):
    owner.replies["/api/lab/registry.recent_decisions"] = (200, json.dumps(
        {"result": "y" * (ollama_role._MAX_TOOL_RESULT_CHARS + 10)}))
    daemon = ScriptedOllama(_assistant("", _recent()), _assistant("Too much."))
    _runner(daemon, owner).run("Read the week.")

    fed_back = daemon.calls[1]["messages"][-1]["content"]
    assert fed_back.startswith("REFUSED:") and "yyyy" not in fed_back


def test_an_owner_refusal_comes_back_as_the_tools_result_so_a_retry_is_possible(
        owner):
    owner.replies["/api/lab/registry.recent_decisions"] = (
        400, json.dumps({"error": "limit must be positive"}))
    daemon = ScriptedOllama(_assistant("", _recent()), _assistant("Noted."))
    assert "Noted" in _runner(daemon, owner).run("Read the week.")
    complaint = daemon.calls[1]["messages"][-1]["content"]
    assert "REFUSED by the qlab owner (400)" in complaint
    assert "limit must be positive" in complaint


def test_an_owner_that_is_not_there_ends_the_run_rather_than_being_retried():
    daemon = ScriptedOllama(_assistant("", _recent()))
    runner = OllamaRoleRunner(backend=daemon, model="granite3.3:8b",
                              role="news-analyst",
                              owner_url="http://127.0.0.1:1")
    with pytest.raises(OllamaRoleError) as exc:
        runner.run("Read the week.")
    assert "unreachable" in str(exc.value)
    assert "retrying will not help" in str(exc.value)


# -- the hard caps ------------------------------------------------------------

def test_a_run_that_never_stops_calling_tools_fails_loudly_at_the_turn_cap(owner):
    """Both sides of the comparison: MAX_TURNS finishes, MAX_TURNS+1 does not."""
    finishing = ScriptedOllama(
        *[_assistant("", _recent()) for _ in range(MAX_TURNS - 1)],
        _assistant("Two publishers, nothing primary."),
    )
    assert "publishers" in _runner(finishing, owner).run("Read the week.")

    endless = ScriptedOllama(*[_assistant("", _recent())
                               for _ in range(MAX_TURNS + 2)])
    events: list = []
    with pytest.raises(OllamaRoleError) as exc:
        _runner(endless, owner, events).run("Read the week.")
    assert f"{MAX_TURNS} tool turns" in str(exc.value)
    assert len(endless.calls) == MAX_TURNS
    assert [e.kind for e in events][-1] == "error"


def test_a_run_that_outlives_its_deadline_fails_loudly(owner):
    """Both sides again, on the clock rather than the turn counter."""
    per_turn = DEADLINE_S / 2.4          # three turns fit, four do not
    clock = Clock()
    in_time = ScriptedOllama(
        _assistant("", _recent()), _assistant("Thin and one-sided."),
        clock=clock, per_turn=per_turn)
    assert "one-sided" in _runner(in_time, owner, clock=clock).run("Read.")

    clock = Clock()
    slow = ScriptedOllama(*[_assistant("", _recent()) for _ in range(MAX_TURNS)],
                          clock=clock, per_turn=per_turn)
    events: list = []
    with pytest.raises(OllamaRoleError) as exc:
        _runner(slow, owner, events, clock=clock).run("Read the week.")
    assert f"{DEADLINE_S:.0f}s" in str(exc.value)
    # The deadline fired, not the turn cap: fewer turns ran than the cap allows.
    assert len(slow.calls) < MAX_TURNS
    assert [e.kind for e in events][-1] == "error"


def test_one_reply_cannot_flood_the_owner_with_five_hundred_writes(owner):
    """The reviewer's probe: a turn cap bounds turns, not calls inside one."""
    flood = [("registry_log_decision",
              {"as_of": "2026-07-31", "kind": "news_analyst",
               "choice": {"i": n}, "rationale": "because"})
             for n in range(500)]
    daemon = ScriptedOllama(_assistant("", *flood), _assistant("Enough."))
    events: list = []
    assert "Enough" in _runner(daemon, owner, events).run("Read the week.")

    # Every call is answered — a tool_call with no result is a malformed
    # history — but only the cap's worth of them reached the owner.
    results = [m for m in daemon.calls[1]["messages"] if m["role"] == "tool"]
    assert len(results) == 500
    assert len(owner.seen) == ollama_role.MAX_CALLS_PER_TURN
    refused = [m for m in results if m["content"].startswith("REFUSED:")]
    assert len(refused) == 500 - ollama_role.MAX_CALLS_PER_TURN
    assert f"{ollama_role.MAX_CALLS_PER_TURN} tool calls" in refused[0]["content"]
    # And the refusals are on the audit stream, not only in the model's context.
    assert sum(1 for e in events if e.kind == "tool_result"
               and "tool calls" in e.text) == len(refused)


def test_the_call_cap_is_a_ceiling_not_a_floor(owner):
    """Both sides of the comparison: the cap's worth run, the next does not."""
    call = ("registry_recent_decisions", {"limit": 1})
    at_cap = ScriptedOllama(
        _assistant("", *[call] * ollama_role.MAX_CALLS_PER_TURN),
        _assistant("Read."))
    _runner(at_cap, owner).run("Read.")
    assert len(owner.seen) == ollama_role.MAX_CALLS_PER_TURN

    owner.seen.clear()
    over = ScriptedOllama(
        _assistant("", *[call] * (ollama_role.MAX_CALLS_PER_TURN + 1)),
        _assistant("Read."))
    _runner(over, owner).run("Read.")
    assert len(owner.seen) == ollama_role.MAX_CALLS_PER_TURN
    last = [m for m in over.calls[1]["messages"] if m["role"] == "tool"][-1]
    assert last["content"].startswith("REFUSED:")


def test_the_deadline_is_read_between_calls_not_only_between_turns(owner):
    """Time spent inside one turn ends the session, and ends it loudly."""
    # Each clock reading costs half the budget: the turn's own reading, then
    # two calls, and the third call finds nothing left.
    clock = Clock(step=DEADLINE_S / 4)
    call = ("registry_recent_decisions", {"limit": 1})
    daemon = ScriptedOllama(_assistant("", *[call] * 5), _assistant("unreached"))
    events: list = []
    with pytest.raises(OllamaRoleError) as exc:
        _runner(daemon, owner, events, clock=clock).run("Read the week.")

    assert f"{DEADLINE_S:.0f}s" in str(exc.value)
    # Not the whole turn, and not none of it: what ran before the budget went,
    # ran — and the rest never reached the owner.
    assert 0 < len(owner.seen) < ollama_role.MAX_CALLS_PER_TURN
    assert len(daemon.calls) == 1          # the second turn was never asked for
    # Each unexecuted call was refused by name, with the reason, on the bus.
    refusals = [e for e in events if e.kind == "tool_result"
                and f"{DEADLINE_S:.0f}s" in e.text]
    assert len(refusals) == 5 - len(owner.seen)
    assert [e.kind for e in events][-1] == "error"


def test_a_budget_spent_on_the_last_turn_reports_the_deadline_not_the_turn_cap(
        owner, monkeypatch):
    """Two caps can be true at once; the reported one must be the one that fired.

    Found by mutation: deleting the end-of-turn raise left every other test
    green, because on turns 1..N-1 the top-of-turn check re-raises the same
    sentence one moment later. The one turn where it cannot is the last, and
    that is the case this pins — a deadline reported as "did not finish in 8
    turns" sends an operator to raise the wrong number.
    """
    monkeypatch.setattr(ollama_role, "MAX_TURNS", 1)
    clock = Clock(step=DEADLINE_S / 2)
    daemon = ScriptedOllama(
        _assistant("", ("registry_recent_decisions", {"limit": 1})))
    with pytest.raises(OllamaRoleError) as exc:
        _runner(daemon, owner, clock=clock).run("Read the week.")

    assert f"{DEADLINE_S:.0f}s" in str(exc.value)
    assert "tool turns" not in str(exc.value)
    assert owner.seen == []


def test_a_stop_mid_turn_refuses_the_calls_that_have_not_run(owner):
    """The cooperative stop reaches inside a turn, on the threaded path."""
    call = ("registry_recent_decisions", {"limit": 1})
    daemon = ScriptedOllama(_assistant("", *[call] * 3), _assistant("unreached"))
    events: list = []
    runner = None

    def watch(event):
        events.append(event)
        if event.kind == "tool_start" and event.text.startswith("calling"):
            runner.stop("the owner is shutting down")

    runner = OllamaRoleRunner(watch, backend=daemon, model="granite3.3:8b",
                              role="news-analyst", owner_url=owner.url)
    assert runner.start("Read the week.", governed=True) is True
    runner.join(timeout=10)

    assert runner.running is False
    assert "was stopped" in runner.last_error
    # The call already in flight finished; the two behind it never left.
    assert len(owner.seen) == 1
    assert len(daemon.calls) == 1


def test_the_backends_deadline_is_the_sessions_remaining_time(owner):
    """A turn may never be given more time than the session has left."""
    clock = Clock()
    daemon = ScriptedOllama(_assistant("", _recent()), _assistant("Thin."),
                            clock=clock, per_turn=DEADLINE_S / 2)
    _runner(daemon, owner, clock=clock).run("Read.")
    assert daemon.calls[1]["timeout"] == pytest.approx(DEADLINE_S / 2)


# -- the driver's protocol ----------------------------------------------------

def test_every_kind_the_runner_emits_is_one_the_driver_records(owner,
                                                               monkeypatch):
    """Driven through the real CoordinatorDriver, not through its signature."""
    monkeypatch.setattr("qlab.tui.claude.resolve_claude_executable",
                        lambda: "/usr/bin/claude")
    # conftest's blanket opt-out exists so no test forks a real, billed Claude
    # tree. This one injects its session factory, so nothing can be forked —
    # the same exemption test_atlas_coordinator.py has, scoped to one test.
    monkeypatch.delenv("QLAB_ATLAS_DRIVE", raising=False)
    daemon = ScriptedOllama(
        _assistant("Reading.", _recent()),
        _assistant("", ("algorithms_solve", {})),       # a refusal turn too
        _assistant("Two publishers, nothing primary."),
    )
    recorded: list[tuple[str, dict]] = []
    driver = CoordinatorDriver(
        runtime_url=owner.url,
        record_event=lambda kind, payload: recorded.append((kind, payload)),
        session_factory=session_factory(backend=daemon, model="granite3.3:8b",
                                        role="news-analyst"))
    assert driver.drive("wf-1", "read the qualitative record")["driving"] is True
    driver._session.join(timeout=10)

    republished = [p for kind, p in recorded if kind == "atlas_coordinator_event"]
    assert republished, "the driver recorded none of the runner's events"
    emitted = {p["event_kind"] for p in republished}
    assert emitted <= set(_RECORDED_KINDS)
    assert {"text", "tool_start", "tool_result", "result"} <= emitted
    # `session` is deliberately absent: emitted by the runner, excluded by
    # _RECORDED_KINDS — the same treatment ClaudeSession keepalives get.
    assert "session" not in emitted
    assert any(p["agent"] == "news-analyst" for p in republished)
    assert all(len(p["text"]) <= 1000 for p in republished)


def test_an_ungoverned_start_is_refused_rather_than_quietly_granted_tools(owner):
    """``governed=False`` means no tools to a ClaudeSession; here it has none."""
    runner = _runner(ScriptedOllama(_assistant("hi")), owner)
    assert runner.start("read", governed=False) is False
    assert "governed" in runner.last_error


# -- the wire -----------------------------------------------------------------

def test_the_whole_loop_runs_over_the_real_backend_and_real_sockets(owner):
    """No double anywhere: OllamaBackend on one path, the owner on another.

    The scripted doubles above pin the harness's decisions; this pins that the
    payload it builds is one an Ollama daemon actually answers, and that the
    reply shape it reads is the one a daemon actually sends. Both were captured
    from a live 0.31.2 daemon before being frozen here.
    """
    owner.script["/api/chat"] = [
        (200, json.dumps({"message": {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "call_1", "function": {
                "index": 0, "name": "registry_recent_decisions",
                "arguments": {"kind": "news_analyst", "limit": 3}}}]}})),
        (200, json.dumps({"message": {
            "role": "assistant",
            "content": "One decision on record, no primary source behind it."},
            "done_reason": "stop"})),
    ]
    runner = OllamaRoleRunner(
        backend=OllamaBackend(base_url=owner.url), model="granite3.3:8b",
        role="news-analyst", owner_url=owner.url)
    assert "no primary source" in runner.run("Read the week.")

    chat, lab = [s for s in owner.seen if s["path"] == "/api/chat"], [
        s for s in owner.seen if s["path"].startswith("/api/lab/")]
    assert len(chat) == 2 and len(lab) == 1
    assert chat[0]["body"]["stream"] is False
    declared = chat[0]["body"]["tools"][0]
    assert declared["type"] == "function"
    assert set(declared["function"]) == {"name", "description", "parameters"}
    # The daemon's own id field is not echoed back; what returns is this
    # module's sentence about what it read.
    assert chat[1]["body"]["messages"][-1] == {
        "role": "tool", "tool_name": "registry_recent_decisions",
        "content": json.dumps([{"decision_id": "dec-1", "kind": "news"}])}


# -- the schemas --------------------------------------------------------------

def test_the_declared_schemas_match_the_owner_proxys_own_signatures():
    """Hand-written schemas, pinned to the real tools so they cannot drift."""
    from qlab.mcp.tui_proxy import RuntimeClient, register_proxy_tools

    class StubApp:
        def __init__(self):
            self.tools: dict = {}

        def tool(self, *, name):
            def register(fn):
                self.tools[name] = fn
                return fn
            return register

    app = StubApp()
    register_proxy_tools(app, RuntimeClient("http://127.0.0.1:1"))
    assert ollama_role.TOOL_SCHEMAS, "the harness declares no tools at all"
    for lab_name, schema in ollama_role.TOOL_SCHEMAS.items():
        params = inspect.signature(
            app.tools[ollama_role.function_name(lab_name)]).parameters
        assert set(schema["parameters"]["properties"]) == set(params), lab_name
        assert set(schema["parameters"]["required"]) == {
            name for name, p in params.items()
            if p.default is inspect.Parameter.empty}, lab_name
