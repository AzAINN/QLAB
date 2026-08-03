"""The Ollama role harness: one governed role, argued through the owner's tools.

The Claude coordinator gets its authority from a protocol it already speaks —
an agent file, an MCP allowlist, a CLI that enforces both. A local model speaks
none of that, so running a role on Ollama means rebuilding the *only* part that
matters: the tool boundary. This module is that rebuild and nothing else. It
loads the role from ``agents/<role>.md`` (the same file the Claude adapter is
generated from — never a copied prompt), declares exactly that role's tools to
the model, and executes a tool call only after it has passed one gate.

Four rules the module is built around:

* **The allowlist is the boundary, and it is one place.** ``_route_for`` is the
  single function that turns a model-named tool into an owner route, and
  ``_owner_post`` is reachable from nowhere else. A call outside the role's
  grants is refused and *recorded*, never executed and never negotiated. This
  is the same argument ``llm_backends._head`` makes about redaction: a boundary
  decided per call site is a boundary that has already leaked once.
* **Every argument is foreign input.** The model's ``arguments`` object is
  validated against the declared schema before the owner is touched. A bad one
  is not a crash and not a silent drop — the complaint goes back as that tool's
  result, so the model can correct itself inside its remaining turns.
* **The caps are loud.** ``MAX_TURNS`` and ``DEADLINE_S`` end a run with an
  error, never with the text of the last turn presented as a conclusion. A
  truncated answer that reads like a finished one is the failure mode a
  research desk can least afford (invariant 4).
* **It widens no authority.** The role's grants come from the role file, the
  provider pin comes from ``model_routing.resolve_route`` rather than from a
  constant re-declared here, and the runner holds no execution tool because the
  piloted role holds none. Swapping the provider must not move the gate.

**The caller (invariant 10, clock closed).** ``CoordinatorDriver`` constructs a
runner for a dispatch whose graph is one role, on a desk whose workforce names
this backend; every other graph is walked by the Claude coordinator, and the
driver records why. Nothing else may construct one — a second caller would be
the second execution path this repo has spent four tasks refusing to grow.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from qlab.agents.loader import parse_agent, tool_base_name
from qlab.core.llm_config import SurfaceModel
# The bounding/redaction gate and the size discipline are imported rather than
# re-implemented. B1's diagnosis, in its own words: "per-call-site reasoning
# about credentials was the actual defect" — a second `_head` here would be
# exactly that, one module later.
from qlab.operator.llm_backends import (
    LlmBackendError,
    OllamaBackend,
    _head,
    _Oversize,
    _read_bounded,
    _safe_url,
)
from qlab.operator.model_routing import record_invocation, resolve_route
from qlab.paths import data_path

OLLAMA_BACKEND = "ollama"

# A judgment role reads a few records and answers. Eight turns is generous for
# that and far short of a loop; the point of the number is that there IS one.
MAX_TURNS = 8
# The whole session's wall budget. A governed phase that has not concluded in
# two minutes on a local model is not close to concluding.
DEADLINE_S = 120.0
# No single turn may hold a thread longer than the session has left, and never
# longer than this even at the start.
TURN_TIMEOUT_S = 90.0
# The owner is in-process-adjacent (loopback) and every route the piloted role
# reaches is a registry read or one insert.
OWNER_TIMEOUT_S = 30.0
# How many tool calls one turn may make. A reply carries a LIST of tool calls
# and the list is the model's, so a turn cap alone bounds nothing: a single
# hallucinated reply asking for five hundred `registry.log_decision` calls would
# have written five hundred rows inside one "turn", each with its own owner
# timeout — the session's wall budget is checked between turns and could not
# see it. Five is the number because a news read needs two (read the record,
# log the judgment) and a schema refusal costs a retry; anything past that is
# not a judgment role working.
MAX_CALLS_PER_TURN = 5

# What one tool result may be worth to the model. Past this the answer is not
# refused silently and not truncated silently: the model is told the size and
# told to narrow the request, which is a turn it can act on.
_MAX_TOOL_RESULT_CHARS = 20_000


class OllamaRoleError(RuntimeError):
    """A run that could not be finished. Never a truncated success."""


# The tools this harness knows how to declare, by their owner-side name.
#
# These schemas are hand-written, and the honest reason is worth stating: the
# real declarations live on FastMCP decorators (``qlab/mcp/tui_proxy.py``,
# ``qlab/mcp/quant_lab.py``) and reading them at runtime would make the owner
# import the optional ``mcp`` extra to answer a question about its own tools.
# So they are minimal-honest copies — names, types, one sentence each — and
# ``tests/test_ollama_role.py`` pins every one of them against the proxy
# function's actual signature, so the copy cannot drift from the original
# without a test saying so.
#
# The bounds are the harness's own, not the owner's: a limit the model may pick
# is an input this module is responsible for, and "1 to 50" is a range a
# judgment role has no reason to leave.
TOOL_SCHEMAS: dict[str, dict] = {
    "registry.recent_decisions": {
        "description": (
            "Read the desk's recent governed judgments and the reflections "
            "recorded against them."),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "description": "Decision kind to filter by; empty for all.",
                },
                "limit": {
                    "type": "integer", "minimum": 1, "maximum": 50,
                    "description": "How many of the most recent to return.",
                },
            },
            "required": [],
        },
    },
    "registry.log_decision": {
        "description": (
            "Record one governed judgment and the reasoning behind it."),
        "parameters": {
            "type": "object",
            "properties": {
                "as_of": {
                    "type": "string",
                    "description": "The point-in-time date, YYYY-MM-DD.",
                },
                "kind": {
                    "type": "string",
                    "description": "The decision kind you are permitted to log.",
                },
                "choice": {
                    "type": "object",
                    "description": "The judgment itself, as an object.",
                },
                "rationale": {
                    "type": "string",
                    "description": "Why, citing the records you actually read.",
                },
                "challenger_view": {
                    "type": "string",
                    "description": "The opposing reading, if one was put to you.",
                },
            },
            "required": ["as_of", "kind", "choice", "rationale"],
        },
    },
}

# How the desk's operating rules reach a model that has never seen a role file.
# Appended to the role's own prompt rather than replacing any of it: the role
# file is the source of truth for what the role IS, and this says only how the
# session it is running in ends.
_HARNESS_NOTE = """

---
Session mechanics (the desk's harness, not your role):

You reach the desk's records through the tools declared to you and through
nothing else. A tool you were not given does not exist here; asking for one is
refused and the refusal comes back to you as that tool's result.

Call the tools you need, then answer in prose. Your prose answer ENDS the
session, so do not write it until you have read what you need. You have at most
{turns} tool turns and {seconds:.0f} seconds in total."""


@dataclass(frozen=True)
class RoleEvent:
    """One event in the shape ``CoordinatorDriver._on_event`` reads.

    Duck-typed rather than ``ClaudeSession``'s ``ClaudeEvent`` on purpose:
    that class lives in ``qlab.tui``, whose package import pulls in Textual (an
    optional extra), and the owner must not grow a TUI dependency to run a
    role. The driver reads these fields with ``getattr``, so the two shapes
    only have to agree on names — which the event-kind parity test asserts by
    driving a real ``CoordinatorDriver``, not by comparing signatures.
    """

    kind: str
    text: str
    tool: str = ""
    agent: str = ""


def function_name(lab_name: str) -> str:
    """``registry.recent_decisions`` -> ``registry_recent_decisions``.

    The same transform ``agents/loader._write_claude`` applies when it emits a
    grant, so a role's tools are named identically to a model whichever
    orchestrator is running it.
    """
    return lab_name.replace(".", "_")


class OllamaRoleRunner:
    """One governed role, running on a local Ollama model, for one session.

    Constructed as ``CoordinatorDriver`` constructs a ``ClaudeSession`` (see
    ``session_factory``): ``start`` returns whether the session began, the run
    itself streams events, and ``stop`` is safe when idle.
    """

    def __init__(
        self,
        on_event: Callable[[RoleEvent], None] | None = None,
        *,
        backend: OllamaBackend,
        model: str,
        role: str,
        owner_url: str,
        offline: bool = True,
        registry=None,
        clock: Callable[[], float] = time.monotonic,
    ):
        # The provider pin is READ, not re-declared. `resolve_route` owns which
        # roles never leave Claude (A3's REQUIRED_CLAUDE_ROLES), and asking it
        # means a future pinned role is refused here without this file being
        # touched. A constant repeated here would be a second authority.
        decision = resolve_route(
            role, workforce=SurfaceModel(backend=OLLAMA_BACKEND, model=model))
        if decision.backend != OLLAMA_BACKEND:
            raise ValueError(
                f"{role} cannot run on the ollama role harness: "
                f"{decision.fallback_reason or 'it is pinned to claude'}")
        self.decision = decision
        self.role = role
        # The route's model, not the caller's. They are the same today; taking
        # the route's answer means they stay the same when routing changes.
        self.model = decision.resolved_model
        self.backend = backend
        self.owner_url = owner_url.rstrip("/")
        # Only ever printed in this form (llm_backends' rule, applied to the
        # other URL this desk holds).
        self.safe_owner_url = _safe_url(self.owner_url)
        self.offline = bool(offline)
        self.registry = registry
        self.on_event = on_event
        self._clock = clock

        definition = parse_agent(data_path("agents", f"{role}.md"))
        # The allowlist, in the vocabulary the model will use. Built once, from
        # the role file, and never added to.
        self._allowed: dict[str, str] = {}
        for grant in definition.tools:
            lab_name = tool_base_name(grant)
            if lab_name not in TOOL_SCHEMAS:
                # Fail loud rather than silently narrow the role: a role whose
                # grants this harness cannot declare would run with less
                # authority than its file gives it, and quietly.
                raise ValueError(
                    f"the ollama role harness has no schema for {lab_name!r}, "
                    f"which {role} is granted; declare it in TOOL_SCHEMAS "
                    "before running this role")
            self._allowed[function_name(lab_name)] = lab_name
        if not self._allowed:
            raise ValueError(f"{role} grants no tools; there is nothing to run")
        self._system = definition.body + _HARNESS_NOTE.format(
            turns=MAX_TURNS, seconds=DEADLINE_S)
        self._tools = [
            {"type": "function",
             "function": {"name": fn,
                          "description": TOOL_SCHEMAS[lab]["description"],
                          "parameters": TOOL_SCHEMAS[lab]["parameters"]}}
            for fn, lab in sorted(self._allowed.items())
        ]

        self.last_error = ""
        self.result = ""
        self.render_failures: list[str] = []
        self._thread: threading.Thread | None = None
        self._running = False
        self._stopped = ""

    # -- the session protocol -------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    def start(self, prompt: str, *, governed: bool = False, **_) -> bool:
        """Begin the run on a background thread. False means it did not begin.

        ``governed=False`` is refused rather than accepted and ignored. On a
        ``ClaudeSession`` that flag means "no MCP server, no tools at all"; a
        runner that shrugged at it would hand the model its full allowlist
        while its caller believed it had granted nothing.
        """
        self.last_error = ""
        if self._running:
            self.last_error = f"a {self.role} session is already running"
            return False
        if not governed:
            self.last_error = (
                "the ollama role harness has no ungoverned mode: it exists to "
                "hold a tool allowlist, so governed=True is the only start")
            return False
        self._running = True
        self._thread = threading.Thread(
            target=self._run_thread, args=(prompt,), daemon=True)
        self._thread.start()
        return True

    def join(self, timeout: float | None = None) -> None:
        """Wait for the run to finish. For a caller that needs the answer."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    def stop(self, reason: str = "operator requested stop") -> None:
        """Ask the run to end. Safe when idle.

        The stop is cooperative: the loop checks between turns and before each
        tool call, and an in-flight call is bounded by its own timeout rather
        than cancelled. There is no process to signal — this harness is HTTP in
        the owner's own threads — so the honest guarantee is "no further turn
        and no further tool call", which is the one that matters for authority.

        The consequence for a caller, named because E3 has to live with it:
        ``running`` stays True for that in-flight window, so a ``stop()``
        immediately followed by a ``start()`` is refused as "already running"
        until the current turn drains. The driver rebuilds a session per
        dispatch and so never restarts one, but E3's shutdown story should say
        this out loud rather than discover it.
        """
        if not self._running:
            return
        self._stopped = reason
        self._emit("session", f"{self.role} session stopping: {reason}")

    def _run_thread(self, prompt: str) -> None:
        try:
            self.result = self.run(prompt)
        except OllamaRoleError as exc:
            self.last_error = str(exc)
        except Exception as exc:            # pragma: no cover - defence in depth
            # `run` has already emitted for the failures it knows about; this
            # is the one path where nothing would reach the audit bus at all.
            self.last_error = _head(f"the {self.role} harness failed: {exc}")
            self._emit("error", self.last_error)
        finally:
            self._running = False

    # -- the loop -------------------------------------------------------------

    def run(self, prompt: str) -> str:
        """Drive the role to a prose answer, or fail loudly. Blocking."""
        started = self._clock()
        self._emit("session",
                   f"{self.role} started on ollama/{self.model}, "
                   f"{len(self._allowed)} tools")
        # `text`, not a private kind: the harness speaks the Claude parser's
        # vocabulary so `_RECORDED_KINDS` has one set of names for two
        # producers (the merge that unified them is where "tool"/"agent"
        # died — kinds no parser emitted, pinned against ever returning).
        self._emit("text", f"{self.role} is reading the desk's record",
                   agent=self.role)
        messages = [{"role": "system", "content": self._system},
                    {"role": "user", "content": prompt}]
        status = "error"
        try:
            answer = self._turns(messages, started)
            status = "ok"
            self._emit("result", answer)
            return answer
        except (OllamaRoleError, LlmBackendError) as exc:
            self._emit("error", str(exc))
            raise OllamaRoleError(str(exc)) from None
        finally:
            self._record(status, (self._clock() - started) * 1000.0)

    def _turns(self, messages: list[dict], started: float) -> str:
        for _ in range(MAX_TURNS):
            if self._stopped:
                raise OllamaRoleError(
                    f"the {self.role} session was stopped: {self._stopped}")
            message = self.backend.chat(
                messages, self.model, tools=self._tools,
                timeout=self._remaining(started))
            content = str(message.get("content") or "")
            calls = message.get("tool_calls")
            if not isinstance(calls, list) or not calls:
                if not content.strip():
                    raise OllamaRoleError(
                        f"{self.model} answered with neither a conclusion nor "
                        "a tool call; there is nothing to report")
                return content.strip()
            if content.strip():
                self._emit("text", content)
            requested = [self._named(call) for call in calls]
            messages.append({
                "role": "assistant", "content": content,
                # Rebuilt from what was read rather than echoed whole: what
                # goes back into a request is this module's sentence, not the
                # daemon's.
                "tool_calls": [{"function": {"name": name, "arguments": raw}}
                               for name, raw in requested],
            })
            # The clock is consulted per CALL, not per turn. Every entry still
            # gets a result — a tool_call the history answers with nothing is a
            # malformed conversation — but once the budget is gone the results
            # are refusals and the session ends loudly after them.
            spent = ""
            for index, (name, raw) in enumerate(requested):
                if index >= MAX_CALLS_PER_TURN:
                    result = self._refuse(name, (
                        f"one turn may make {MAX_CALLS_PER_TURN} tool calls "
                        f"and this is number {index + 1}; ask for less in one "
                        "turn"))
                elif spent or self._stopped:
                    spent = spent or (f"the {self.role} session was stopped: "
                                      f"{self._stopped}")
                    result = self._refuse(name, spent)
                else:
                    try:
                        self._remaining(started)
                    except OllamaRoleError as exc:
                        spent = str(exc)
                        result = self._refuse(name, spent)
                    else:
                        result = self._execute(name, raw)
                messages.append({"role": "tool", "tool_name": name,
                                 "content": result})
            if spent:
                raise OllamaRoleError(spent)
        raise OllamaRoleError(
            f"{self.role} did not reach a conclusion in {MAX_TURNS} tool "
            f"turns on {self.model}; a run that keeps calling tools has not "
            "answered, and the last turn is not a conclusion")

    @staticmethod
    def _named(call) -> tuple[str, object]:
        """The name and raw arguments of one tool call. Never trusts the shape."""
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict):
            return "", {}
        return str(function.get("name") or ""), function.get("arguments")

    def _remaining(self, started: float) -> float:
        """What is left of the session's budget; exhausting it is the failure."""
        left = DEADLINE_S - (self._clock() - started)
        if left <= 0:
            raise OllamaRoleError(
                f"the {self.role} session outlived its {DEADLINE_S:.0f}s "
                f"budget on {self.model}; what it had said by then is a "
                "partial read, not a conclusion")
        return min(left, TURN_TIMEOUT_S)

    # -- the boundary ---------------------------------------------------------

    def _route_for(self, name: str) -> str | None:
        """The owner route for ``name``, or None when the role may not call it.

        THE gate. Every tool call passes here and ``_owner_post`` is reachable
        from nowhere else, so "is this role allowed to do that" is answered in
        one place for every tool that exists and every tool that will.
        """
        return self._allowed.get(name)

    def _refuse(self, name: str, reason: str) -> str:
        """One refusal: recorded on the audit bus AND returned as the result.

        Every "no" this module says is assembled here, so a refusal cannot be
        recorded in one place and worded differently in another — and so the
        model always sees the same thing the audit row saw.
        """
        refusal = f"REFUSED: {reason}"
        self._emit("tool_result", refusal, tool=name)
        return refusal

    def _execute(self, name: str, raw_arguments) -> str:
        """Run one tool call and return what the model should see as its result.

        Refusals are results, not exceptions: the model gets the complaint and
        one of its remaining turns to act on it. Only a failure the model
        cannot fix — an owner that is not there — ends the run.
        """
        lab_name = self._route_for(name)
        if lab_name is None:
            return self._refuse(name, (
                f"{name!r} is not on {self.role}'s allowlist and was not "
                f"executed. This role holds: {', '.join(sorted(self._allowed))}."))

        arguments = _arguments(raw_arguments)
        if arguments is None:
            return self._refuse(name, (
                f"{name} takes a JSON object of arguments; got "
                f"{type(raw_arguments).__name__}."))
        problem = _schema_problem(arguments,
                                  TOOL_SCHEMAS[lab_name]["parameters"])
        if problem is not None:
            return self._refuse(name, f"{name} was not called — {problem}")

        self._emit("tool_start", f"calling {name}", tool=name)
        payload = dict(arguments)
        # The proxy attaches the session's own offline flag to every lab call;
        # a model may not choose whether the desk is online.
        payload["offline"] = self.offline
        result = self._owner_post(f"/api/lab/{lab_name}", payload)
        if len(result) > _MAX_TOOL_RESULT_CHARS:
            return self._refuse(name, (
                f"{name} answered with {len(result)} characters, past this "
                f"session's {_MAX_TOOL_RESULT_CHARS}; ask for less (a smaller "
                "limit) rather than reading it all."))
        self._emit("tool_result", f"{name} answered: {_head(result)}", tool=name)
        return result

    def _owner_post(self, path: str, payload: dict) -> str:
        """One owner call. Reachable only through ``_execute``'s gate.

        A refusal the owner explained comes back as the tool's result so one
        corrected retry is possible — ``tui_proxy.OwnerRefused``'s reasoning,
        which exists because a worker that cannot read the refusal repeats the
        identical call until its budget is gone. An owner that is not there is
        the opposite case and ends the run.
        """
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.owner_url + path, data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=OWNER_TIMEOUT_S) as response:
                raw = _read_bounded(response)
        except urllib.error.HTTPError as exc:
            try:
                detail = _head(_read_bounded(exc) or b"")
            except _Oversize as big:
                detail = f"body refused, {big}"
            return _head(f"REFUSED by the qlab owner ({exc.code}) on {path}: "
                         f"{detail}")
        except _Oversize as big:
            raise OllamaRoleError(_head(
                f"the qlab owner answered {path} with {big}; refusing to "
                "buffer it")) from None
        except OSError as exc:
            raise OllamaRoleError(_head(
                f"the qlab owner at {self.safe_owner_url} is unreachable "
                f"({exc}); the run cannot continue and retrying will not "
                "help")) from None
        try:
            decoded = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise OllamaRoleError(_head(
                f"the qlab owner answered {path} with a non-JSON body: "
                f"{_head(raw)}")) from None
        if not isinstance(decoded, dict):
            raise OllamaRoleError(_head(
                f"the qlab owner answered {path} with "
                f"{type(decoded).__name__}, not an object"))
        return json.dumps(decoded.get("result"), default=str)

    # -- recording ------------------------------------------------------------

    def _record(self, status: str, latency_ms: float) -> None:
        """One invocation row per session, when a registry was handed over.

        The backend name is NOT passed: ``record_invocation`` derives it from
        the route, which is A3's rule — a row cannot claim a provider the
        decision never chose. The row therefore reads ``ollama`` because the
        route says so, and would stop reading it the moment the route changed.

        No registry means no row. The owner is the single writer and this
        object runs inside it (the driver's own thread), so a caller with a
        registry to hand over is a caller that already holds the writer; the
        only path without one is a test.
        """
        if self.registry is None:
            return
        try:
            record_invocation(self.registry, self.decision, status=status,
                              latency_ms=latency_ms)
        except Exception as exc:
            # An audit write must not turn a completed read into a failure,
            # but a lost row must not be silent either.
            self.render_failures.append(_head(f"invocation row lost: {exc}"))

    def _emit(self, kind: str, text: str, *, tool: str = "",
              agent: str = "") -> None:
        """One event, with EVERY field bounded and redacted on the way out.

        Everything here is untrusted: a model's prose, an owner's body, a
        daemon's error — and a tool *name*, which is the model's string too and
        which ``CoordinatorDriver._on_event`` copies into a durable event row.
        An earlier version gated ``text`` alone and reasoned that the other
        fields were safe; a ten-thousand-character tool name went straight
        through it. So the gate is per-EVENT, not per-field: nothing leaves this
        method without passing ``llm_backends._head``, including ``agent`` and
        ``kind``, which this module composes itself and which therefore need no
        bounding — deciding that per field is the habit that let the last one
        out, and a docstring saying "every field" while one field was exempt is
        the same habit wearing a promise.
        """
        if self.on_event is None:
            return
        try:
            self.on_event(RoleEvent(kind=_head(kind), text=_head(text),
                                    tool=_head(tool), agent=_head(agent)))
        except Exception as exc:
            self.render_failures.append(_head(f"{kind}: {exc!r}"))


def session_factory(*, backend: OllamaBackend, model: str, role: str,
                    registry=None) -> Callable[..., OllamaRoleRunner]:
    """A ``CoordinatorDriver.session_factory`` bound to one backend and role.

    The driver builds a session with ``(on_event, cwd=, runtime_url=, offline=,
    fast=)`` — a signature written for a subprocess. ``cwd`` is meaningless
    here (there is no process to isolate; the isolation IS the allowlist) and
    ``fast`` is a Claude tier trade with no counterpart: the ollama route's
    model is the operator's picked one and there is no cheaper tier under it.
    Both are accepted and dropped deliberately, named here rather than absorbed
    silently, because a factory that raised on them would be incompatible with
    the caller E3 has to use.
    """

    def build(on_event, *, cwd=None, runtime_url="", offline=True, fast=None):
        return OllamaRoleRunner(
            on_event, backend=backend, model=model, role=role,
            owner_url=runtime_url, offline=offline, registry=registry)

    return build


# ---------------------------------------------------------------------------
# foreign input
# ---------------------------------------------------------------------------

def _arguments(raw) -> dict | None:
    """The model's arguments as an object, or None.

    A dict is what a live daemon sends. A JSON *string* is tolerated for the
    same reason ``reasoner._object`` tolerates a code fence: it is a formatting
    habit every local model has sooner or later, and it is not a different
    answer. Nothing past those two is guessed at.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except ValueError:
            return None
        return decoded if isinstance(decoded, dict) else None
    return None


_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
}


def _schema_problem(arguments: dict, schema: dict) -> str | None:
    """What is wrong with ``arguments``, in a sentence the model can act on.

    Strict in the direction that matters: an argument the tool does not take is
    a refusal rather than a silent drop, because a model that believed it had
    filtered a read and did not would report on the wrong record.
    """
    properties: dict = schema.get("properties", {})
    missing = [name for name in schema.get("required", [])
               if name not in arguments]
    if missing:
        return (f"it requires {', '.join(sorted(missing))}, which "
                f"{'were' if len(missing) > 1 else 'was'} not given")
    for name, value in arguments.items():
        spec = properties.get(name)
        if spec is None:
            return (f"{name!r} is not one of its arguments; it takes "
                    f"{', '.join(sorted(properties)) or 'none'}")
        wanted = _JSON_TYPES.get(str(spec.get("type")))
        if wanted is None:                  # pragma: no cover - declared types only
            continue
        # bool is an int in Python and would sail through an integer check.
        if isinstance(value, bool) and bool not in wanted:
            return f"{name} must be {spec['type']}, not a boolean"
        if not isinstance(value, wanted):
            return (f"{name} must be {spec['type']}, not "
                    f"{type(value).__name__} ({value!r})")
        low, high = spec.get("minimum"), spec.get("maximum")
        if low is not None and value < low:
            return f"{name} must be at least {low}; {value} is below it"
        if high is not None and value > high:
            return f"{name} must be at most {high}; {value} is above it"
    return None
