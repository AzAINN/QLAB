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
from pathlib import Path
from typing import Callable

from qlab.core.llm_config import SurfaceModel
from qlab.operator.model_routing import CLAUDE_BACKEND

# The event kinds worth putting on the audit bus. A coordinator emits far more
# than a reader needs; recording every token would bury the desk's own events.
_RECORDED_KINDS = ("text", "tool", "agent", "error", "session", "result")


def drive_enabled() -> bool:
    """Whether the owner may spawn its own coordinator.

    Default on: an Atlas that dispatches work nothing runs is the bug this
    module exists to fix, so opting *out* is the deliberate act.
    """
    return os.environ.get("QLAB_ATLAS_DRIVE", "1") != "0"


def no_role_harness_reason(backend: str) -> str:
    """Why a configured workforce backend cannot drive a governed run yet.

    The picker accepts any backend the desk can reach, and running the five
    governed roles on one is a separate build: the coordinator this driver
    spawns is the Claude CLI, and nothing else speaks its role protocol. Chosen
    but unrunnable is therefore an ordinary state, and it is refused by name
    rather than silently downgraded to claude — a run that quietly ignored the
    operator's choice is exactly the inference this whole feature refuses.
    """
    return (f"the {backend.capitalize()} role harness is not built — "
            "workforce runs on claude")


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
        # Injected in tests so the whole driver is exercisable without a real
        # Claude on PATH. Production passes nothing and gets the real session.
        self._session_factory = session_factory
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

    def available(self) -> tuple[bool, str]:
        """Can a coordinator be spawned right now, and if not, why not.

        Returns the reason alongside the verdict because every caller needs to
        show it: a silent False is what made the old behaviour unreadable.

        Ordered by how durable each refusal is, so the reason shown is the one
        still true after the operator has fixed the others: shutdown is
        terminal, a workforce backend with no harness survives every toggle,
        install and wait below it.
        """
        if self._closed:
            return False, "the owner is shutting down"
        backend = self.workforce.backend if self.workforce else CLAUDE_BACKEND
        if backend != CLAUDE_BACKEND:
            return False, no_role_harness_reason(backend)
        if not drive_enabled():
            return False, "owner-driven coordination is off (QLAB_ATLAS_DRIVE=0)"
        if self.busy:
            return False, (f"a coordinator is already driving workflow "
                           f"{self._workflow_id}")
        try:
            from qlab.tui.claude import resolve_claude_executable
        except Exception as exc:      # pragma: no cover - import-time only
            return False, f"coordinator support unavailable: {exc}"
        if not resolve_claude_executable():
            return False, "the `claude` CLI is not on PATH"
        return True, ""

    # -- driving --------------------------------------------------------------

    def drive(self, workflow_id: str, goal: str) -> dict:
        """Spawn a coordinator for an already-registered workflow.

        Returns ``{"driving": bool, "reason": str}``. Never raises for an
        ordinary refusal — a dispatch that cannot be driven is still a valid
        dispatch a human can resume, so it must not fail the Atlas task.
        """
        with self._lock:
            ok, reason = self.available()
            if not ok:
                self.last_reason = reason
                self._emit("atlas_coordinator_skipped",
                           {"workflow_id": workflow_id, "reason": reason})
                return {"driving": False, "reason": reason}
            try:
                session = self._build_session(workflow_id)
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
            self._emit("atlas_coordinator_started",
                       {"workflow_id": workflow_id, "goal": goal[:300]})
            return {"driving": True, "reason": ""}

    def stop(self, reason: str = "owner shutting down") -> None:
        """Terminate any running coordinator. Safe to call when idle.

        Holds the lock across the whole teardown, and clears the slot only once
        the process is actually gone. Releasing it earlier made `busy` report
        False while the tree was still dying, so a dispatch landing in that
        window spawned a second coordinator — and on owner shutdown, that second
        one outlives the runtime it was talking to.
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

    def _build_session(self, workflow_id: str):
        factory = self._session_factory
        if factory is None:
            from qlab.tui.claude import ClaudeSession
            factory = ClaudeSession
        return factory(
            lambda event: self._on_event(workflow_id, event),
            cwd=self.cwd,
            runtime_url=self.runtime_url,
            offline=self.offline,
            fast=self.fast,
        )

    def _on_event(self, workflow_id: str, event) -> None:
        """Republish one coordinator event onto the owner's audit bus.

        This is what makes autonomous work legible: the same stream the TUI
        renders when a human drives a run, recorded so any client — TUI, web,
        `qlab events` — sees an unattended run reason in real time.
        """
        kind = str(getattr(event, "kind", "") or "")
        if kind not in _RECORDED_KINDS:
            return
        text = str(getattr(event, "text", "") or "")
        self._emit("atlas_coordinator_event", {
            "workflow_id": workflow_id,
            "event_kind": kind,
            "agent": str(getattr(event, "agent", "") or ""),
            "tool": str(getattr(event, "tool", "") or ""),
            # Bounded: a coordinator can emit long blocks and the event bus is
            # a durable table, not a scrollback buffer.
            "text": text[:1000],
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
