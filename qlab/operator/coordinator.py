"""The owner's own coordinator: what actually drives an Atlas dispatch.

Atlas could always *dispatch* — pick a template, check authority, register a
governed workflow with its declared phase graph. Nothing then drove that
workflow. The phases advance only when a Claude coordinator walks them, and the
two places that spawn one are both human-initiated: the TUI's workforce panel
and ``qlab workforce run``. So an autonomous tick produced a workflow row stuck
at its first phase, forever, with no error anywhere — the desk looked like a
wrapper that did nothing, because unattended it *was* one.

This module is the missing half. It spawns the same governed coordinator a human
would, against the workflow Atlas just registered, and republishes its stream
onto the owner's event bus so the reasoning is visible rather than buried in a
subprocess pipe.

It widens no authority. The coordinator is the identical ``ClaudeSession`` with
the identical allowlist: five roles, no filesystem, no shell, no execution tool.
Every phase gate, the referee's ``targets_hash`` binding, and the human approval
on any fill apply exactly as they do when a person presses start. What changes is
who presses start.

**Mixing is per dispatch, not per phase.** A session here serves one workflow,
and the roles a Claude coordinator dispatches are subagents inside its own
process — invisible to this driver and unreachable by it. So the granularity at
which a provider can be chosen is the graph: a one-role graph
(``templates.news_read``) is served by ``OllamaRoleRunner`` on the operator's
configured backend, and every multi-role graph is walked by the Claude
coordinator, because nothing else speaks the phase protocol. Widening that
would mean this module becoming a phase walker of its own, which is a second
orchestration model and is refused.

The referee is therefore claude twice over: ``resolve_route`` pins it, and
every graph that carries it is a graph the one-role harness cannot walk. Both
facts are recorded per dispatch rather than assumed — a role that ran on claude
while the desk's workforce said otherwise says so in its own invocation row.

Bounded on purpose:

* **One at a time.** A second dispatch while one coordinator runs is refused with
  a reason, never queued — N concurrent Claude trees on one desk is a cost
  incident, not autonomy.
* **Opt-out.** ``QLAB_ATLAS_DRIVE=0`` leaves dispatch-only behaviour intact for
  anyone who wants to drive runs by hand.
* **Absence is reported, not absorbed.** No ``claude`` on PATH means the workflow
  stays registered and the reason says so, so a human can still resume it.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from qlab.core.llm_config import SurfaceModel
# The one bounding+redaction gate (B1's rule), reused rather than re-derived:
# this module writes durable rows out of two producers' foreign strings, which
# is exactly the call site that has leaked whenever it reasoned for itself.
from qlab.operator.llm_backends import _head
from qlab.operator.model_routing import (
    CLAUDE_BACKEND,
    pinned_to_claude_reason,
    record_invocation,
    resolve_route,
)
# The one backend a role harness exists for. Read from the harness rather than
# re-declared, so a second harness is wired by the module that implements it.
from qlab.operator.ollama_role import OLLAMA_BACKEND as _HARNESS_BACKEND

# The event kinds worth putting on the audit bus. A coordinator emits far more
# than a reader needs; recording every token would bury the desk's own events.
#
# `tool` and `agent` are the role harness's vocabulary — a ``ClaudeSession``
# emits `tool_start`/`tool_result`, which this deliberately does not record,
# because a governed run's tool traffic is TUI material and would outnumber the
# desk's own events on the bus by an order of magnitude.
_RECORDED_KINDS = ("text", "tool", "agent", "error", "session", "result")

# What one field of one event may be worth in a durable row. `text` is the
# reasoning a reader actually wants; every other field is a name.
_TEXT_CHARS = 1000

# "read the live attribute" — distinct from None, which is a real workforce
# value meaning the unconfigured desk. Callers on the snapshot path want the
# live read; one dispatch passes the surface it captured and gets that one.
_LIVE = object()


def drive_enabled() -> bool:
    """Whether the owner may spawn its own coordinator.

    Default on: an Atlas that dispatches work nothing runs is the bug this
    module exists to fix, so opting *out* is the deliberate act.
    """
    return os.environ.get("QLAB_ATLAS_DRIVE", "1") != "0"


@dataclass(frozen=True)
class SessionPlan:
    """Which provider serves one dispatch, and why it is not the configured one.

    ``role`` is set only on the harness path, where a session IS one role.
    ``pinned_reason`` is passed to ``resolve_route`` so every row the dispatch
    records carries the reason its provider was not the operator's choice —
    the mechanism REQUIRED_CLAUDE_ROLES already uses, extended from a policy
    pin to a capability one.
    """

    backend: str
    role: str = ""
    pinned_reason: str = ""


def no_role_harness_reason(backend: str) -> str:
    """Why a configured workforce backend serves no role at all.

    The picker accepts any backend the desk can reach; exactly one of them has
    a role harness. A backend without one is not hidden and not silently
    honoured — the graph runs on claude and every row it records says the
    operator's choice could not be served, and by what name.
    """
    return (f"the {backend.capitalize()} role harness is not built — "
            "workforce runs on claude")


def coordinator_walks_reason(backend: str, roles: tuple[str, ...]) -> str:
    """Why a multi-role graph ran on claude although the workforce named another.

    Recorded, never silent. A run that quietly ignored the operator's choice is
    the inference this whole feature refuses; a run that says which roles it
    could not move, and why, is the desk being honest about a capability it
    does not have.

    Only for graphs the harness could not serve *whatever* their roles were. A
    single role it declined is the route's own pin and says so in the route's
    own words — see ``_plan``.
    """
    return (f"the {backend} role harness serves one role per session; this "
            f"{len(roles)}-phase graph is walked by the claude coordinator")


def resume_prompt(workflow_id: str, goal: str) -> str:
    """The coordinator's brief for a workflow that already exists.

    Naming the workflow is not cosmetic. Without it the coordinator opens a
    second workflow for the same trigger, and then two runs compete for one
    referee verdict.
    """
    return (
        f"RESUME_WORKFLOW_ID: {workflow_id}\n"
        "Inspect workflow.status first. Continue at the first non-done phase; "
        "do not create a new workflow.\n"
        f"GOAL: {goal}"
    )


class CoordinatorDriver:
    """Runs at most one headless coordinator on the owner's behalf."""

    def __init__(
        self,
        *,
        runtime_url: str,
        cwd: Path | None = None,
        record_event: Callable[[str, dict], object] | None = None,
        offline: bool = True,
        fast: bool | None = None,
        workforce: SurfaceModel | None = None,
        session_factory: Callable[..., object] | None = None,
        registry=None,
        backend_factory: Callable[[str], object] | None = None,
        backend_status: Callable[[str], tuple[bool | None, str]] | None = None,
    ):
        self.runtime_url = runtime_url
        self.cwd = cwd
        self._record = record_event
        self.offline = offline
        # None defers to the operator's configured setting at spawn time, so a
        # toggle takes effect on the next run without rebuilding the driver.
        self.fast = fast
        # The configured workforce surface, re-read like `fast`. None means the
        # desk as it has always been: the Claude coordinator.
        self.workforce = workforce
        # Injected in tests so the CLAUDE half is exercisable without a real
        # Claude on PATH. Production passes nothing and gets the real session.
        # The harness half is injected through `backend_factory` instead, so a
        # mixed-pipeline test drives the real runner against a scripted daemon
        # rather than a second stand-in for it.
        self._session_factory = session_factory
        self._backend_factory = backend_factory
        # The LAST availability reading for a backend, never a probe: this is
        # read from `available()`, which `coordinator_status()` calls on the
        # snapshot path under the owner's dispatch lock. `(None, "")` means
        # nothing has been probed yet — not a refusal, because the run itself
        # fails loudly with the daemon's own sentence, which beats a guess.
        self._backend_status = backend_status
        # The owner's own registry handle, for the invocation rows this driver
        # can vouch for. Not a second writer: the driver runs inside the owner
        # process, so a caller with a registry to hand over already holds the
        # single writer (ollama_role._record makes the same argument).
        self.registry = registry
        self._lock = threading.Lock()
        self._session = None
        self._workflow_id = ""
        self.last_reason = ""
        # Set by stop(). Terminal: freeing the slot after the owner has begun
        # shutting down just lets the next dispatch spawn a tree that outlives
        # the runtime it talks to.
        self._closed = False

    # -- state ----------------------------------------------------------------

    @property
    def busy(self) -> bool:
        session = self._session
        return bool(session is not None and getattr(session, "running", False))

    @property
    def current_workflow_id(self) -> str:
        return self._workflow_id if self.busy else ""

    def _surface(self, workforce) -> SurfaceModel | None:
        """The captured workforce, or the live attribute when none was passed.

        The owner reassigns ``self.workforce`` on every property access, so a
        method that re-reads it is sampling a value that can change under it.
        Inside one dispatch every reader takes the surface ``drive`` captured;
        everywhere else the live read is the point.
        """
        return self.workforce if workforce is _LIVE else workforce

    def available(self, roles: tuple[str, ...] = (), *,
                  workforce=_LIVE) -> tuple[bool, str]:
        """Can a coordinator be spawned right now, and if not, why not.

        Returns the reason alongside the verdict because every caller needs to
        show it: a silent False is what made the old behaviour unreadable.

        ``roles`` names the graph being asked about. With no roles it is the
        desk's general readiness and the answer is the strict one — this desk
        runs both kinds of graph, so both providers have to be there. With a
        graph named only that session's provider matters: a five-phase review
        never touches the daemon, and a one-role news read never needs the CLI.

        Ordered by how durable each refusal is, so the reason shown is the one
        still true after the operator has fixed the others: shutdown is
        terminal, a provider that is down survives every toggle, install and
        wait below it.
        """
        if self._closed:
            return False, "the owner is shutting down"
        workforce = self._surface(workforce)
        backend = workforce.backend if workforce else CLAUDE_BACKEND
        plan = self._plan(roles, workforce=workforce)
        # Only the backend a harness exists for: refusing a claude graph
        # because an unusable backend is down would be a refusal about nothing.
        if backend == _HARNESS_BACKEND and (not roles or plan.backend == backend):
            ready, reason = self._backend_reading(backend)
            if ready is False:
                return False, reason
        if not drive_enabled():
            return False, "owner-driven coordination is off (QLAB_ATLAS_DRIVE=0)"
        if self.busy:
            return False, (f"a coordinator is already driving workflow "
                           f"{self._workflow_id}")
        if plan.backend == CLAUDE_BACKEND:
            try:
                from qlab.tui.claude import resolve_claude_executable
            except Exception as exc:  # pragma: no cover - import-time only
                return False, f"coordinator support unavailable: {exc}"
            if not resolve_claude_executable():
                return False, "the `claude` CLI is not on PATH"
        return True, ""

    def _plan(self, roles: tuple[str, ...], *, workforce=_LIVE) -> SessionPlan:
        """Which provider serves this graph.

        The harness serves a dispatch only when the dispatch IS one role: it
        holds a role's allowlist and no phase tools, so it cannot walk a graph.
        Whether that one role may leave claude is ``resolve_route``'s answer,
        not one re-derived here — a future pinned role is refused by this
        method without it being touched.

        Whether the harness has a *schema* for that role's grants is checked
        where the runner is constructed, and fails loudly there. Answering it
        here would mean parsing an agent file on the snapshot path, every tick,
        to refuse something the construction refuses anyway.
        """
        workforce = self._surface(workforce)
        backend = workforce.backend if workforce else CLAUDE_BACKEND
        if backend == CLAUDE_BACKEND:
            return SessionPlan(CLAUDE_BACKEND)
        if backend != _HARNESS_BACKEND:
            return SessionPlan(CLAUDE_BACKEND,
                               pinned_reason=no_role_harness_reason(backend))
        if len(roles) == 1:
            if resolve_route(roles[0],
                             workforce=workforce).backend == backend:
                return SessionPlan(backend, role=roles[0])
            # A one-role graph IS the shape the harness serves, so the graph is
            # not the reason — the role is. Saying "this 1-phase graph is walked
            # by the coordinator" here would contradict the row's own sentence
            # in the one case where both are written about the same dispatch.
            return SessionPlan(
                CLAUDE_BACKEND,
                pinned_reason=pinned_to_claude_reason(roles[0], backend))
        return SessionPlan(CLAUDE_BACKEND,
                           pinned_reason=coordinator_walks_reason(backend, roles))

    def workforce_note(self) -> str:
        """What the desk should say about a workforce choice it cannot honour.

        A backend with no role harness serves nothing, on any graph, until one
        is built — a standing fact about the desk rather than about a dispatch,
        so it is said once on the status card. It used to be said by refusing
        the desk outright, and retiring that refusal is what would otherwise
        have made an unhonoured operator choice silent.

        The harness backend gets no note here on purpose: what it serves
        depends on the graph, and a per-dispatch fact belongs on the rows that
        dispatch records, not on a card that cannot know which graph is next.
        """
        backend = self.workforce.backend if self.workforce else CLAUDE_BACKEND
        if backend in (CLAUDE_BACKEND, _HARNESS_BACKEND):
            return ""
        return no_role_harness_reason(backend)

    def _backend_reading(self, backend: str) -> tuple[bool | None, str]:
        """The last availability reading for ``backend``; never a probe."""
        if self._backend_status is None:
            return None, ""
        return self._backend_status(backend)

    # -- driving --------------------------------------------------------------

    def drive(self, workflow_id: str, goal: str,
              roles: tuple[str, ...] = ()) -> dict:
        """Spawn a coordinator for an already-registered workflow.

        ``roles`` are the workflow's phases as agent names — what decides which
        provider serves this dispatch. Empty (an unnamed graph) is the claude
        coordinator, which is what every caller got before there was a choice.

        Returns ``{"driving": bool, "reason": str}``. Never raises for an
        ordinary refusal — a dispatch that cannot be driven is still a valid
        dispatch a human can resume, so it must not fail the Atlas task.

        One dispatch reads one workforce. The owner reassigns the driver's
        ``workforce`` on every property access — including the snapshot poll —
        so a Settings change landing between the plan and the recorded routes
        used to make the rows name a backend that did not serve those roles.
        It is captured here and threaded through every reader below; the
        driver's own lock does not help, because the writer never takes it.
        ``fast`` has the identical shape and predates this — untouched here so
        the fix stays one claim, and it deserves the same capture.
        """
        with self._lock:
            workforce = self.workforce
            ok, reason = self.available(roles, workforce=workforce)
            if not ok:
                self.last_reason = reason
                self._emit("atlas_coordinator_skipped",
                           {"workflow_id": workflow_id, "reason": reason})
                return {"driving": False, "reason": reason}
            plan = self._plan(roles, workforce=workforce)
            try:
                session = self._build_session(workflow_id, plan,
                                              workforce=workforce)
                started = session.start(resume_prompt(workflow_id, goal),
                                        governed=True)
            except Exception as exc:
                self.last_reason = f"coordinator failed to start: {exc}"
                self._emit("atlas_coordinator_failed",
                           {"workflow_id": workflow_id,
                            "reason": self.last_reason})
                return {"driving": False, "reason": self.last_reason}
            if not started:
                self.last_reason = (getattr(session, "last_error", "")
                                    or "coordinator refused to start")
                self._emit("atlas_coordinator_failed",
                           {"workflow_id": workflow_id,
                            "reason": self.last_reason})
                return {"driving": False, "reason": self.last_reason}
            self._session = session
            self._workflow_id = str(workflow_id)
            self.last_reason = ""
            self._record_routes(plan, roles, workforce=workforce)
            self._emit("atlas_coordinator_started",
                       {"workflow_id": workflow_id, "goal": goal[:300],
                        "backend": plan.backend, "role": plan.role})
            return {"driving": True, "reason": ""}

    def stop(self, reason: str = "owner shutting down") -> None:
        """Terminate any running coordinator. Safe to call when idle.

        Holds the lock across the whole teardown, and clears the slot only once
        the process is actually gone. Releasing it earlier made `busy` report
        False while the tree was still dying, so a dispatch landing in that
        window spawned a second coordinator — and on owner shutdown, that second
        one outlives the runtime it was talking to.

        The two session types stop differently and the honest guarantee is per
        type. A ``ClaudeSession`` is a process group and this waits for it. The
        role harness has no process to signal — it is HTTP on the owner's own
        threads — so its ``stop`` is cooperative: no further turn and no further
        tool call, while one in-flight owner call drains under its own timeout.
        Its ``running`` therefore stays True for that window, which would make
        a restart-in-place refuse itself as "already running".

        Nothing here restarts in place, and the composition is what makes that
        safe rather than either half: this driver builds a session per dispatch
        and never reuses one, and ``_closed`` is terminal, so the cooperative
        window cannot admit a second session. The window's real cost is bounded
        and worth naming: the owner may outlive its stop() by one in-flight
        owner call. Blocking shutdown on that instead would trade a bounded
        overlap for an unbounded wait, on the path where the owner is leaving.
        """
        with self._lock:
            self._closed = True
            session = self._session
            if session is None:
                return
            self._emit("atlas_coordinator_stopped",
                       {"workflow_id": self._workflow_id, "reason": reason})
            try:
                session.stop()
            except Exception:
                # Shutdown must not raise. ClaudeSession's process-group
                # teardown reports its own failures through on_event.
                pass
            self._session = None

    # -- internals ------------------------------------------------------------

    def _build_session(self, workflow_id: str, plan: SessionPlan, *,
                       workforce=_LIVE):
        """The session for one dispatch — a coordinator, or one role.

        Both are constructed identically because ``OllamaRoleRunner`` was
        written to the signature this driver already used; the differences it
        cannot honour (``cwd``, ``fast``) are dropped in its own factory and
        named there rather than absorbed here.

        The model comes from the same surface ``plan`` was built from: reading
        it live could hand the harness a model belonging to another backend, or
        no surface at all on a desk that just cleared its choice.
        """
        workforce = self._surface(workforce)
        if plan.backend == CLAUDE_BACKEND:
            factory = self._session_factory
            if factory is None:
                from qlab.tui.claude import ClaudeSession
                factory = ClaudeSession
        else:
            from qlab.operator.ollama_role import session_factory

            factory = session_factory(
                backend=self._build_backend(plan.backend),
                model=workforce.model, role=plan.role,
                registry=self.registry)
        return factory(
            lambda event: self._on_event(workflow_id, event),
            cwd=self.cwd,
            runtime_url=self.runtime_url,
            offline=self.offline,
            fast=self.fast,
        )

    def _build_backend(self, name: str):
        if self._backend_factory is not None:
            return self._backend_factory(name)
        from qlab.operator.llm_backends import build_backend

        return build_backend(name)

    def _record_routes(self, plan: SessionPlan, roles: tuple[str, ...], *,
                       workforce=_LIVE) -> None:
        """One invocation row per role the CLAUDE coordinator was configured with.

        Deliberately not written for the harness path: the runner records its
        own row when it finishes, with the status and the latency it can vouch
        for. Here the driver can vouch for one thing only — which routes the
        coordinator was given — so the rows say ``dispatched`` and never claim
        a result. A row is written by whoever knows what happened.

        This is also where the operator's choice stops being invisible: on a
        non-claude workforce every one of these rows carries the reason its
        role did not move, which is why the multi-role case is a recorded pin
        rather than a silent downgrade.

        The surface is the dispatch's, not the current one: these rows and
        ``plan`` are two readings of one choice, and a row derived from a
        later reading would claim a provider this dispatch never ran on.
        """
        if self.registry is None or plan.backend != CLAUDE_BACKEND:
            return
        from qlab.tui.claude import fast_mode_enabled

        workforce = self._surface(workforce)
        # The session resolves `None` the same way; the row has to name the
        # model the CLI was actually configured with, not a default.
        fast = fast_mode_enabled() if self.fast is None else bool(self.fast)
        try:
            for role in roles:
                record_invocation(
                    self.registry,
                    resolve_route(role, workforce=workforce, fast=fast,
                                  pinned_reason=plan.pinned_reason),
                    status="dispatched")
        except Exception as exc:
            # An audit write must not turn a live dispatch into a failure, and
            # a lost row must not be silent either (ollama_role's rule).
            self._emit("atlas_coordinator_routes_lost",
                       {"reason": _head(f"{exc!r}")})

    def _on_event(self, workflow_id: str, event) -> None:
        """Republish one coordinator event onto the owner's audit bus.

        This is what makes autonomous work legible: the same stream the TUI
        renders when a human drives a run, recorded so any client — TUI, web,
        `qlab events` — sees an unattended run reason in real time.

        EVERY field goes through the gate, not the one that looked long. This
        is the sink for two producers, and it bounded nothing at all while both
        of them were argued about individually: a ten-thousand-character tool
        name is one hostile reply away on either side, and the row it lands in
        is durable. The only per-field decision left is the budget — prose is
        worth more of a row than a name is — and redaction is unconditional.
        """
        kind = str(getattr(event, "kind", "") or "")
        if kind not in _RECORDED_KINDS:
            return
        text = str(getattr(event, "text", "") or "")
        self._emit("atlas_coordinator_event", {
            "workflow_id": _head(str(workflow_id)),
            "event_kind": _head(kind),
            "agent": _head(str(getattr(event, "agent", "") or "")),
            "tool": _head(str(getattr(event, "tool", "") or "")),
            # A coordinator can emit long blocks and the event bus is a durable
            # table, not a scrollback buffer. `_head` also COLLAPSES: a
            # recorded block is one line where it used to keep its newlines.
            # That is the gate's doing and it is kept — a row is a record, the
            # renderer is where prose gets its shape back — but it is a real
            # change to what `atlas_coordinator_event.text` looks like.
            "text": _head(text, _TEXT_CHARS),
        })

    def _emit(self, kind: str, payload: dict) -> None:
        if self._record is None:
            return
        try:
            self._record(kind, payload)
        except Exception:
            # A recording failure must never take down the coordinator or the
            # owner tick that dispatched it.
            pass
