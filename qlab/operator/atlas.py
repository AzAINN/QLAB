"""The deterministic AtlasSupervisor: lifecycle, triggers, dedupe, budgets.

Atlas is a supervisor plus an interpreting agent. Everything here is the
*supervisor* — deterministic code that decides when something is worth a human's
or an agent's attention, deduplicates it, and persists it. Crucially, basic
health monitoring and "nothing changed" operation need no LLM call: the
observer evaluates triggers and assembles the desk brief from owner facts alone.

Authority is structural: this class exposes no execute or propose method in any
mode. In Observe mode it never launches a workforce — it records tasks and
alerts and produces briefs. Coordinator (Claude) unavailability degrades Atlas to
``degraded`` while the owner, data, and book remain usable; it is never an owner
failure.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from qlab.state.registry import Registry

if TYPE_CHECKING:      # annotations only; a runtime import would be a cycle
    from qlab.operator.template_judge import ReasonerChoice

MANAGER_ID = "atlas"

# Modes (authority) and states (lifecycle).
MODES = ("observe", "research", "propose", "paused")
# What a desk with no persisted state starts as. Research is the highest mode
# that cannot create a paper plan: `check_startable` refuses every
# plan-creating template below Propose, so this widens what Atlas *researches*
# without moving the execution boundary an inch.
DEFAULT_MODE = "research"
STARTING = "starting"
OBSERVING = "observing"
INVESTIGATING = "investigating"
COORDINATING = "coordinating"
SYNTHESIZING = "synthesizing"
AWAITING_APPROVAL = "awaiting_approval"
BLOCKED = "blocked"
DEGRADED = "degraded"
PAUSED = "paused"

# The statuses a task may still be started from. `start_task` is the authority;
# every surface that offers an approve button reads this rather than writing its
# own copy, because an offer the gate answers 400 to is a wrong verdict.
STARTABLE_TASK_STATES = ("queued", "failed")

# Trigger kinds that would launch an autonomous workflow (subject to the daily
# budget). Briefs and alerts are not workflow launches and do not count.
_WORKFLOW_TRIGGERS = frozenset({
    "drawdown_warning", "drawdown_control", "drift_breach", "regime_flip",
})

# Trigger kinds whose mint has no bound per window, and which therefore must not
# start unattended. Everything else the beat may start fires once per condition
# — one startup, one recovery, one kill-switch trip, one research run — so the
# count is bounded even where `_WORKFLOW_TRIGGERS` does not charge for it.
# `held_record_change` mints one task per held name whose qualitative record
# moved, per window, and `portfolio_watch` runs a Claude coordinator with
# WebSearch/WebFetch, so a wide news day is one uncounted coordinator per
# ticker. These stay `queued` and announced until the mint site itself is
# bounded (coalesce per window, then join `_WORKFLOW_TRIGGERS`).
#
# NOT the complement of `_WORKFLOW_TRIGGERS`: membership there is about the
# daily budget, and briefs and alerts are deliberately outside it while still
# being work the beat has always started.
_UNBOUNDED_TRIGGERS = frozenset({"held_record_change"})

# How many of the newest task rows a bounded scan reads. One number, because
# two would disagree the first time either moved.
#
# The limit alone is not what keeps a scan honest — every scan below also names
# the status or origin it cares about, so its window holds that class of row
# only. Fifty was enough while triggers were the only thing queued; proposals
# are minted per template per day and no task row is ever deleted, so an
# unfiltered window fills with spent proposals in about a month and a queued
# trigger that is still inside `max_task_age_days` drops off the end of it.
TASK_SCAN_WINDOW = 200


@dataclass(frozen=True)
class AtlasConfig:
    max_autonomous_workflows_per_day: int = 3
    max_atlas_turns_per_task: int = 6
    regime_cooldown_sessions: int = 1
    drift_threshold: float = 0.05
    # One corrected retry per failed task; a second failure blocks rather than
    # looping (plan §8.6: "no automatic loop after a second failure").
    max_task_attempts: int = 2
    # A trigger is a claim about a specific trading day. After this many days
    # the portfolio it described has moved, so the queued task is reported
    # stale rather than startable; if the condition still holds, the observe
    # tick fires it again under today's date. Five covers a trading week, so a
    # Friday breach is still actionable on the following Wednesday.
    max_task_age_days: int = 5


@dataclass(frozen=True)
class Dispatched:
    """A runner started durable work; the task is *not* finished.

    Returning this instead of a conclusion is what keeps Atlas honest. A runner
    that creates a workflow row has not performed the research, so the task
    stays ``running`` and is bound to the workflow. Only
    :meth:`AtlasSupervisor.reconcile_tasks`, reading the workflow's own terminal
    state, may complete or fail it.

    A runner returning a plain dict is asserting the work is genuinely done
    inline -- which is true only for deterministic templates that need no
    coordinator.
    """

    workflow_id: str
    detail: dict | None = None


# Workflow states from which an Atlas task may be resolved. Anything else means
# the workflow is still in flight and the task must keep waiting.
_WORKFLOW_SUCCESS = "complete"
_WORKFLOW_UNSUCCESSFUL = frozenset({
    # `stale` is the owner's week-without-progress mark. It belongs here rather
    # than outside: a task bound to a workflow nobody has walked in seven days
    # would otherwise stay `running` forever, and the drive sweep would keep
    # re-spawning a coordinator for a graph that is not going anywhere.
    "failed", "blocked", "interrupted", "abandoned", "stale",
})
# The union, for readers that only need "would reconcile_tasks resolve this?".
# Derived rather than re-listed so a state added to either half above cannot
# leave a second copy behind: `drive_pending_tasks` spawns a coordinator for
# every workflow NOT in this set, so a stale copy here would re-walk a run that
# reconciliation is about to close.
WORKFLOW_RESOLVED_STATUSES = frozenset({_WORKFLOW_SUCCESS}) | _WORKFLOW_UNSUCCESSFUL


@dataclass(frozen=True)
class Trigger:
    kind: str
    action: str            # brief | alert | block | workflow | pause_proposals
    template_id: str | None
    payload: dict
    state_hash: str


class AtlasSupervisor:
    """Deterministic desk supervisor. No execution or proposal authority."""

    def __init__(
        self,
        registry: Registry,
        *,
        coordinator_available: Callable[[], bool],
        config: AtlasConfig | None = None,
        id_gen: Callable[[], str] | None = None,
    ):
        self.registry = registry
        self._coordinator_available = coordinator_available
        self.config = config or AtlasConfig()
        self._id_gen = id_gen or (lambda: uuid.uuid4().hex[:16])
        if self.registry.get_atlas_state() is None:
            # A fresh desk starts in Research, not Observe. Observe permits no
            # workflow at all, so the desk shipped inert: it observed, queued
            # nothing it could act on, and looked broken. Research lets Atlas do
            # the research work by itself while `check_startable` still refuses
            # every plan-creating template — reaching a paper trade needs Propose
            # mode AND a human approval, both untouched by this default.
            self.registry.save_atlas_state(
                {"mode": DEFAULT_MODE, "state": STARTING}, MANAGER_ID)

    # -- mode / lifecycle controls ------------------------------------------
    @property
    def mode(self) -> str:
        """The persisted authority mode. The one read, in one place.

        Four call sites read it with the same inline `or {}` dance and a fifth
        was about to: the owner needs it to compose the reasoner's menu, and a
        menu built from a differently-defaulted mode than the gate uses is the
        exact disagreement `startable_templates` exists to prevent.
        """
        return (self.registry.get_atlas_state(MANAGER_ID) or {}).get(
            "mode", "observe")

    def status(self) -> dict:
        state = self.registry.get_atlas_state(MANAGER_ID) or {}
        return {
            "manager_id": MANAGER_ID,
            "mode": state.get("mode", "observe"),
            "state": state.get("state", STARTING),
            "current_task_id": state.get("current_task_id"),
            "last_wake_reason": state.get("last_wake_reason"),
            "last_brief_at": state.get("last_brief_at"),
            "blocked_reason": state.get("blocked_reason"),
            "coordinator_available": bool(self._coordinator_available()),
        }

    def set_mode(self, mode: str) -> dict:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
        self._patch_state(mode=mode)
        self.registry.record_event("atlas_mode", {"mode": mode})
        return self.status()

    def pause(self) -> dict:
        return self.set_mode("paused")

    def resume(self, mode: str = "observe") -> dict:
        if mode == "paused":
            raise ValueError("resume target cannot be 'paused'")
        return self.set_mode(mode)

    # -- the observe tick (deterministic; no LLM for unchanged health) ------
    def pending_judgments(self, facts: dict, *,
                          trading_date: str) -> list[Trigger]:
        """Triggers that would open a NEW task and so need a template chosen.

        Deterministic, read-only, and with no model anywhere near it: the owner
        calls this while it holds its dispatch lock and asks the reasoner
        outside it, which is the only arrangement in which a model call can sit
        on the observe loop's path without freezing the owner.

        Deduplicated on purpose. A condition that persists — a drawdown tier, a
        drift breach — fires on every tick and creates exactly one task, so
        asking a model on each of those ticks would spend the desk's latency
        and the operator's tokens on an answer discarded before it was read.
        The daily budget is checked for the same reason: a workflow trigger
        past the ceiling starts nothing, so its template is not worth choosing.
        """
        if self.mode == "paused":
            return []
        # Trigger rows only, of every status: a trigger already handled today
        # must keep suppressing itself after it has run, so this one genuinely
        # needs history — but not the history of what the operator was offered.
        known = {task.get("dedupe_key")
                 for task in self.registry.list_atlas_tasks(
                     TASK_SCAN_WINDOW, origin="trigger")}
        pending: list[Trigger] = []
        for trig in self._evaluate_triggers(facts):
            if trig.template_id is None or trig.action == "block":
                continue
            if trig.action == "workflow" and not self._within_daily_budget(
                    trading_date):
                continue
            if self._dedupe_key(trig, trading_date, facts) in known:
                continue
            pending.append(trig)
        return pending

    def observe(self, facts: dict, *, trading_date: str,
                judgments: Mapping[str, "ReasonerChoice"] | None = None) -> dict:
        """Evaluate triggers against owner facts and persist any new tasks.

        Returns a summary: the resolved lifecycle state, the deterministic desk
        brief, and the tasks created this tick (deduplicated). In Observe mode
        no workforce is launched; workflow-action triggers are recorded as
        queued tasks for a human or a higher mode to act on.

        ``judgments`` maps a trigger kind to the reasoner's template choice for
        it, already made — out of band, by the owner, from a menu this gate
        composed. It arrives as *data* rather than as a callable so this method
        stays what its docstring has always said it is: deterministic, with no
        model call inside it and nothing that can block the loop. An empty or
        absent map is the desk exactly as it was before the reasoner existed.
        """
        mode = self.mode
        coordinator = bool(self._coordinator_available())

        triggers = self._evaluate_triggers(facts)
        created: list[dict] = []
        blocked_reason = None

        # Paused: monitoring and approval expiry continue, but no new autonomous
        # tasks are created; the brief is still available.
        if mode != "paused":
            for trig in triggers:
                if trig.action == "block":
                    blocked_reason = trig.payload.get("reason", trig.kind)
                    continue
                if trig.action == "workflow" and not self._within_daily_budget(
                        trading_date):
                    blocked_reason = (
                        f"autonomous workflow budget exhausted "
                        f"({self.config.max_autonomous_workflows_per_day}/day)")
                    continue
                template_id, source, judged = self._judged_template(
                    trig, mode, facts, judgments)
                dedupe = self._dedupe_key(trig, trading_date, facts)
                task_id = self._id_gen()
                if self.registry.create_atlas_task(
                        task_id, dedupe, trig.kind, trig.payload, template_id):
                    self.registry.record_event(
                        "atlas_task", {"task_id": task_id, "trigger": trig.kind,
                                     "action": trig.action,
                                     # Which path chose the template, on every
                                     # row. The doc's condition for ever
                                     # removing the table is that both are
                                     # recorded against identical facts and
                                     # compared; a row that did not say which
                                     # one it came from would make that
                                     # comparison unrunnable after the fact.
                                     "template_id": template_id,
                                     "source": source})
                    if judged is not None:
                        kind, payload = judged
                        self.registry.record_event(
                            kind, {"task_id": task_id, **payload})
                    created.append({"task_id": task_id, "trigger": trig.kind,
                                    "action": trig.action})

        resolved = self._resolve_state(mode, facts, coordinator, blocked_reason)
        wake = triggers[0].kind if triggers else None
        self._patch_state(state=resolved, last_wake_reason=wake,
                          blocked_reason=blocked_reason)
        return {
            "state": resolved,
            "mode": mode,
            "coordinator_available": coordinator,
            "created_tasks": created,
            "brief": self.desk_brief(facts),
        }

    # -- research mode: start registered templates only ---------------------
    def startable_tasks(self, facts: dict, *, today: str | None = None) -> list[dict]:
        """Queued tasks whose template Atlas may start right now, with reasons.

        Deterministic and side-effect free: it reports what *could* run under
        the current mode and data state, and why anything is refused.

        Queued tasks do not expire on their own, so this surface had grown into
        fifteen `drift_breach` rows — one per trading day back to 2026-07-19 —
        every one of them offered as work Atlas could start, all carrying the
        same permit refusal. Two things were wrong with that. Fifteen rows
        saying one sentence is not a list of options, it is noise that buries
        whatever else is queued. Worse, the refusal named the *permit*, so
        widening the permit would have started a fifteen-day-old drift breach
        against a portfolio whose weights had long since moved: a trigger is a
        claim about a specific day, and it does not keep.

        ``today`` is injected rather than read from the clock. A deterministic
        surface whose answer changes at midnight UTC cannot be reproduced or
        tested, and this one is read by the authority gate.
        """
        from qlab.operator.templates import (
            TemplateNotAllowed, check_startable, template_for_trigger)

        mode = self.mode
        today = (today or _utc_today())[:10]
        out: list[dict] = []
        # Queued only, in SQL. This is the gate's window: what it can see must
        # be bounded by what is waiting, not by how much has happened.
        for task in self.registry.list_atlas_tasks(
                TASK_SCAN_WINDOW, status="queued"):
            template_id = task.get("template_id") or template_for_trigger(
                str(task.get("trigger_kind")))
            if not template_id:
                continue
            # NULL is a pre-column row, and those are all trigger work. Only
            # NULL: `or` would fold `""` in with it, and an empty origin must
            # not read as the one value the heartbeat is allowed to start.
            # Everything else passes through verbatim, so anything that is not
            # exactly "trigger" is refused downstream rather than normalised
            # into a permit.
            origin = task.get("origin")
            # The kind travels with the entry because the unattended caller
            # gates on it: which trigger fired decides whether the beat may
            # start this without a human, and re-reading the row to find out
            # would be a second copy of the queue's own answer.
            entry = {"task_id": task["task_id"], "template_id": template_id,
                     "kind": str(task.get("trigger_kind") or ""),
                     "origin": "trigger" if origin is None else str(origin)}
            entry.update(self.task_age(task, today))
            if entry["stale"] is not False:
                # Stale, or of unknown age. Either way it is refused, and the
                # reason is the age rather than the permit, because the permit
                # is not what is wrong with it.
                entry["startable"] = False
                entry["reason"] = entry.pop("age_reason")
                out.append(entry)
                continue
            entry.pop("age_reason", None)
            try:
                template = check_startable(template_id, mode, facts)
            except TemplateNotAllowed as exc:
                entry.update({"startable": False, "reason": str(exc)})
            else:
                entry.update({"startable": True,
                              "needs_coordinator": template.needs_coordinator,
                              "creates_plan": template.creates_plan})
            out.append(entry)
        return out

    def task_age(self, task: dict, today: str) -> dict:
        """How old this task's triggering condition is, in trading days.

        Read from the dedupe key, which carries the trading date the trigger
        fired on; `created_at` is wall-clock and would call a task recorded
        just after midnight UTC a day older than it is.

        Public because a second surface needs exactly this answer: the owner's
        two-second snapshot cannot call ``atlas_facts`` (it latches the regime)
        and so cannot ask the full gate, but age needs no facts. One reader of
        the dedupe key's age, not two that drift apart — and ``start_task``
        checks no age at all, so a surface that shows a stale proposal as
        startable is showing work that will run.
        """
        day = _dedupe_trading_date(task.get("dedupe_key"))
        if not _looks_like_a_date(day):
            # Unknown age must never read the same as known-fresh, so this is
            # None rather than False and the task is not offered.
            return {"stale": None, "trading_date": day or None,
                    "age_days": None,
                    "age_reason": ("age unknown: this task carries no readable "
                                   "trading date, so whether its trigger still "
                                   "describes today cannot be established")}
        age = _days_between(day, today)
        limit = self.config.max_task_age_days
        if age > limit:
            return {"stale": True, "trading_date": day, "age_days": age,
                    "age_reason": (
                        f"stale: this trigger fired on {day}, {age} days before "
                        f"{today}, and describes a portfolio that has since "
                        f"moved. Tasks older than {limit} days are not started; "
                        f"if the condition still holds it will fire again.")}
        return {"stale": False, "trading_date": day, "age_days": age}

    def start_task(self, task_id: str, facts: dict, *,
                   runner: Callable[[dict, str], dict] | None = None) -> dict:
        """Start one queued task's registered template.

        ``runner(task, template_id) -> conclusion`` performs the actual work
        (the coordinator dispatch); it is injected so the authority checks and
        task lifecycle are testable without a live model. Authority is checked
        here, before any runner is called.
        """
        from qlab.operator.templates import TemplateNotAllowed, check_startable

        task = self.registry.get_atlas_task(task_id)
        if task is None:
            raise KeyError(f"unknown task {task_id!r}")
        if task.get("status") not in STARTABLE_TASK_STATES:
            raise PermissionError(
                f"task {task_id!r} is {task.get('status')!r}; only a queued or "
                "failed task may start")
        attempts = int(task.get("attempt_count") or 0)
        if attempts >= self.config.max_task_attempts:
            self.registry.update_atlas_task(
                task_id, status="blocked",
                error=f"exhausted {attempts} attempt(s); no automatic retry")
            self._patch_state(state=BLOCKED,
                              blocked_reason=f"task {task_id} exhausted retries")
            return {"started": False, "blocked_by": "retry_budget"}

        mode = self.mode
        template_id = str(task.get("template_id") or "")
        try:
            template = check_startable(template_id, mode, facts)
        except TemplateNotAllowed as exc:
            # An authority refusal is a fact about the CURRENT mode, not about
            # the task: the same task is legitimately startable once the
            # operator moves to Research or Propose. Writing "blocked" — the
            # status an exhausted retry budget earns — made it terminal, since
            # nothing moves a task back to queued and `startable_tasks` lists
            # only queued ones. The day's trigger was then gone for good,
            # because its dedupe key was unchanged and no new task could be
            # created for the same condition.
            self.registry.update_atlas_task(task_id, error=str(exc))
            return {"started": False, "blocked_by": "authority",
                    "reason": str(exc)}

        self.registry.update_atlas_task(task_id, status="running", bump_attempt=True)
        self._patch_state(state=COORDINATING, current_task_id=task_id)
        self.registry.record_event(
            "atlas_task_started", {"task_id": task_id, "template_id": template_id})

        if runner is None:
            # Nothing to run the work: the task stays running for a supervisor
            # with a coordinator; report honestly rather than faking a result.
            return {"started": True, "template_id": template_id,
                    "needs_coordinator": template.needs_coordinator,
                    "conclusion": None}
        try:
            conclusion = runner(task, template_id)
        except Exception as exc:
            self.registry.update_atlas_task(task_id, status="failed", error=str(exc))
            self._patch_state(state=OBSERVING, current_task_id=None)
            self.registry.record_event(
                "atlas_task_failed", {"task_id": task_id, "error": str(exc)[:300]})
            return {"started": True, "completed": False, "error": str(exc)}

        if isinstance(conclusion, Dispatched):
            # Durable work is now in flight elsewhere. The task stays running
            # and Atlas stays coordinating; reconcile_tasks resolves it from the
            # workflow's own terminal state, never from the fact of dispatch.
            self.registry.update_atlas_task(
                task_id, workflow_id=conclusion.workflow_id)
            self.registry.record_event(
                "atlas_task_dispatched",
                {"task_id": task_id, "workflow_id": conclusion.workflow_id,
                 "template_id": template_id})
            return {"started": True, "completed": False, "dispatched": True,
                    "workflow_id": conclusion.workflow_id,
                    "template_id": template_id}

        self.registry.update_atlas_task(task_id, status="completed",
                                      conclusion=conclusion)
        self._patch_state(state=OBSERVING, current_task_id=None)
        self.registry.record_event("atlas_task_completed", {"task_id": task_id})
        return {"started": True, "completed": True, "conclusion": conclusion}

    def reconcile_tasks(self) -> list[dict]:
        """Resolve running tasks from the terminal state of their workflow.

        This is the only path that may complete a dispatched task, and it runs
        on the observe cycle and at owner startup -- so a workflow that finished
        while the process was down is still picked up.

        A task with no workflow binding is left alone: it belongs to a
        deterministic template that completes inline. A task bound to a workflow
        that does not exist is failed rather than left running forever.
        """
        moved: list[dict] = []
        for task in self.registry.list_atlas_tasks(
                limit=TASK_SCAN_WINDOW, status="running"):
            workflow_id = str(task.get("workflow_id") or "")
            if not workflow_id:
                continue
            task_id = str(task["task_id"])
            workflow = self.registry.get_workflow(workflow_id)
            if workflow is None:
                error = (f"bound workflow {workflow_id} does not exist; "
                         "the task cannot be resolved")
                self.registry.update_atlas_task(
                    task_id, status="failed", error=error)
                self.registry.record_event(
                    "atlas_task_failed",
                    {"task_id": task_id, "error": error})
                moved.append({"task_id": task_id, "status": "failed",
                              "workflow_status": None})
                continue

            workflow_status = str(workflow.get("status") or "")
            if workflow_status == _WORKFLOW_SUCCESS:
                conclusion = {
                    "workflow_id": workflow_id,
                    "workflow_status": workflow_status,
                    "result": workflow.get("result") or {},
                }
                self.registry.update_atlas_task(
                    task_id, status="completed", conclusion=conclusion)
                self.registry.record_event(
                    "atlas_task_completed",
                    {"task_id": task_id, "workflow_id": workflow_id})
                moved.append({"task_id": task_id, "status": "completed",
                              "workflow_status": workflow_status})
            elif workflow_status in _WORKFLOW_UNSUCCESSFUL:
                error = f"workflow {workflow_id} {workflow_status}"
                self.registry.update_atlas_task(
                    task_id, status="failed", error=error)
                self.registry.record_event(
                    "atlas_task_failed",
                    {"task_id": task_id, "error": error})
                moved.append({"task_id": task_id, "status": "failed",
                              "workflow_status": workflow_status})
            else:
                continue

        if moved:
            self._patch_state(state=OBSERVING, current_task_id=None)
        return moved

    # -- deterministic desk brief (no LLM) ----------------------------------
    def desk_brief(self, facts: dict) -> dict:
        data = facts.get("data", {})
        pf = facts.get("portfolio", {})
        regime = facts.get("regime", {})
        return {
            "data": {
                "provider": data.get("provider"),
                "blocked": bool(data.get("blocked")),
                "eligible_for_paper_proposal": bool(
                    data.get("eligible_for_paper_proposal")),
            },
            "book": {
                "equity": pf.get("equity"),
                "drawdown": pf.get("drawdown"),
                "drawdown_tier": pf.get("drawdown_tier"),
                "halted": bool(pf.get("halted")),
                "gross_exposure": pf.get("gross_exposure"),
                "max_drift": pf.get("drift"),
            },
            "regime": regime.get("robust_state"),
            "open_workflows": int(facts.get("open_workflows", 0)),
            "pending_approvals": int(facts.get("pending_approvals", 0)),
        }

    # -- internals -----------------------------------------------------------
    def _judged_template(
        self, trig: Trigger, mode: str, facts: dict,
        judgments: Mapping[str, "ReasonerChoice"] | None,
    ) -> tuple[str | None, str, tuple[str, dict] | None]:
        """Which template this trigger starts, and which path chose it.

        The order is the whole design and it is ``start_task``'s precedent:
        authority is checked in code, after judgment, never by it.
        The reasoner has already chosen — out of band, from a menu
        ``check_startable`` composed — and here the SAME gate runs again on
        what it returned. That second run is not belt-and-braces: the menu was
        composed one lock phase earlier, so a mode the operator changed in
        between is caught here and nowhere else.

        Returns ``(template_id, source, event)`` where ``source`` is
        ``reasoner`` or ``lookup`` and ``event`` is the extra row to record
        once the task actually exists — a divergence to compare later, or the
        reason a choice was dropped. Nothing is recorded here, because a
        deduplicated trigger creates no task and must leave no trace either.
        """
        from qlab.operator.templates import TemplateNotAllowed, check_startable

        lookup = trig.template_id
        choice = (judgments or {}).get(trig.kind)
        if choice is None:
            return lookup, "lookup", None
        try:
            check_startable(choice.template_id, mode, facts)
        except TemplateNotAllowed as exc:
            # Discarded loudly. The reasoner may want anything; what it gets is
            # what the gate already permits, and the table's answer stands.
            return lookup, "lookup", ("reasoner.fallback", {
                "trigger": trig.kind, "lookup": lookup,
                "reasoner": choice.template_id,
                "reason": f"the gate refused the reasoner's choice: {exc}"})
        if choice.template_id == lookup:
            return choice.template_id, "reasoner", None
        return choice.template_id, "reasoner", ("reasoner.divergence", {
            "trigger": trig.kind, "reasoner": choice.template_id,
            "lookup": lookup, "rationale": choice.rationale[:300]})

    def _evaluate_triggers(self, facts: dict) -> list[Trigger]:
        triggers: list[Trigger] = []
        data = facts.get("data", {})
        pf = facts.get("portfolio", {})

        if facts.get("startup"):
            triggers.append(self._t("owner_startup", "brief", "desk_brief",
                                    {"reason": "owner startup"}))
        if data.get("blocked"):
            triggers.append(self._t(
                "data_degraded", "block", None,
                {"reason": "required data source unavailable",
                 "detail": data.get("reason") or data.get("reasons")}))
        elif facts.get("data_recovered"):
            triggers.append(self._t("data_recovered", "brief", "desk_brief",
                                    {"reason": "data recovered"}))

        tier = pf.get("drawdown_tier")
        if pf.get("halted") or tier == "breaker":
            triggers.append(self._t(
                "kill_switch", "alert", "risk_event",
                {"reason": "kill switch / breaker", "tier": tier}))
        elif tier in ("warning", "control"):
            triggers.append(self._t(
                "drawdown_" + str(tier), "workflow", "risk_event",
                {"reason": f"drawdown {tier} tier", "tier": tier}))

        drift = pf.get("drift")
        if isinstance(drift, (int, float)) and drift > self.config.drift_threshold:
            triggers.append(self._t(
                "drift_breach", "workflow", "regime_review",
                {"reason": "drift threshold exceeded",
                 "drift": round(float(drift), 4)},
                # The breach is the condition; the exact drift is detail for
                # the operator and must not re-key the trigger as it moves.
                identity={"reason": "drift threshold exceeded"}))

        if facts.get("regime", {}).get("flip"):
            triggers.append(self._t(
                "regime_flip", "workflow", "regime_review",
                {"reason": "regime flip confirmed"}))

        if facts.get("order_anomaly"):
            triggers.append(self._t(
                "order_anomaly", "pause_proposals", None,
                {"reason": "order reject/partial/stale"}))
        return triggers

    def _t(self, kind: str, action: str, template: str | None,
           payload: dict, identity: dict | None = None) -> Trigger:
        # The trigger->template map is the single source of truth; a trigger
        # that maps to no template starts no workflow.
        #
        # `identity` is what makes two firings the SAME condition, and defaults
        # to the whole payload. A payload carrying a live measurement needs it:
        # the drift breach hashed its own drift value, so a 1bp move minted a
        # fresh dedupe key, and three ticks of ordinary price movement created
        # three tasks and exhausted the daily workflow budget — after which a
        # genuine drawdown trigger was refused for the rest of the day.
        from qlab.operator.templates import template_for_trigger

        return Trigger(kind=kind, action=action,
                       template_id=template_for_trigger(kind) or template,
                       payload=payload,
                       state_hash=_hash(payload if identity is None else identity))

    def _dedupe_key(self, trig: Trigger, trading_date: str, facts: dict) -> str:
        universe = ",".join(sorted(facts.get("universe", [])))
        return f"{trig.kind}|{trading_date}|{universe}|{trig.state_hash}"

    def _within_daily_budget(self, trading_date: str) -> bool:
        # Bounds autonomous WORKFLOW launches per TRADING day (briefs/alerts do
        # not count). The trading date is read from the dedupe key, never from
        # created_at: wall-clock UTC rolls over at 00:00 while a trading date
        # does not, so counting by created_at would silently drop the budget
        # for any task recorded after midnight UTC. Task volume per day is
        # tiny, so a bounded scan is fine.
        #
        # This bounds UNATTENDED launches, so it counts trigger rows only, and
        # it says so in SQL. Two locks, deliberately: proposals are minted as
        # `proposal:<template_id>`, which is outside _WORKFLOW_TRIGGERS and so
        # would not be counted anyway. The failure both prevent is a proposal
        # that spends the desk's own autonomy while sitting unapproved — seven
        # of them in the morning would exhaust the day's budget and the
        # afternoon's genuine drawdown trigger would be refused. The kind alone
        # was one rename away from that; the origin filter does not depend on
        # anyone remembering which kinds are in the set.
        day = trading_date[:10]
        used = sum(
            1 for task in self.registry.list_atlas_tasks(
                TASK_SCAN_WINDOW, origin="trigger")
            if _dedupe_trading_date(task.get("dedupe_key")) == day
            and task.get("trigger_kind") in _WORKFLOW_TRIGGERS)
        return used < self.config.max_autonomous_workflows_per_day

    def _resolve_state(self, mode: str, facts: dict, coordinator: bool,
                       blocked_reason: str | None) -> str:
        if mode == "paused":
            return PAUSED
        if blocked_reason is not None:
            return BLOCKED
        if not coordinator:
            # Owner/data/book remain usable; only the interpreting agent is out.
            return DEGRADED
        if self._has_running_task():
            # A dispatched task is still in flight. Reporting OBSERVING here
            # told the operator the desk was idle while a workforce run it had
            # launched was executing — the observe tick overwrote the
            # COORDINATING that start_task had just written.
            return COORDINATING
        return OBSERVING

    def _has_running_task(self) -> bool:
        return bool(self.registry.list_atlas_tasks(
            limit=1, status="running"))

    def _patch_state(self, **fields) -> None:
        current = self.registry.get_atlas_state(MANAGER_ID) or {}
        merged = {
            "mode": fields.get("mode", current.get("mode", "observe")),
            "state": fields.get("state", current.get("state", STARTING)),
            "current_task_id": fields.get(
                "current_task_id", current.get("current_task_id")),
            "last_wake_reason": fields.get(
                "last_wake_reason", current.get("last_wake_reason")),
            "last_brief_at": fields.get(
                "last_brief_at", current.get("last_brief_at")),
            "blocked_reason": fields.get("blocked_reason")
            if "blocked_reason" in fields else current.get("blocked_reason"),
            "coordinator_session_id": current.get("coordinator_session_id"),
        }
        self.registry.save_atlas_state(merged, MANAGER_ID)


def _dedupe_trading_date(dedupe_key) -> str:
    """The trading date segment of ``kind|trading_date|universe|state_hash``."""
    parts = str(dedupe_key or "").split("|")
    return parts[1][:10] if len(parts) > 1 else ""


def _looks_like_a_date(value: str) -> bool:
    try:
        date.fromisoformat(str(value)[:10])
    except ValueError:
        return False
    return True


def _days_between(earlier: str, later: str) -> int:
    return (date.fromisoformat(later[:10]) - date.fromisoformat(earlier[:10])).days


def _utc_today() -> str:
    return datetime.now(UTC).date().isoformat()


def _hash(payload: dict) -> str:
    import json

    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:12]
