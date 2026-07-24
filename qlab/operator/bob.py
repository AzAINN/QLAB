"""The deterministic BobSupervisor: lifecycle, triggers, dedupe, budgets.

Bob is a supervisor plus an interpreting agent. Everything here is the
*supervisor* — deterministic code that decides when something is worth a human's
or an agent's attention, deduplicates it, and persists it. Crucially, basic
health monitoring and "nothing changed" operation need no LLM call: the
observer evaluates triggers and assembles the desk brief from owner facts alone.

Authority is structural: this class exposes no execute or propose method in any
mode. In Observe mode it never launches a workforce — it records tasks and
alerts and produces briefs. Coordinator (Claude) unavailability degrades Bob to
``degraded`` while the owner, data, and book remain usable; it is never an owner
failure.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from qlab.state.registry import Registry

MANAGER_ID = "bob-the-quant"

# Modes (authority) and states (lifecycle).
MODES = ("observe", "research", "propose", "paused")
STARTING = "starting"
OBSERVING = "observing"
INVESTIGATING = "investigating"
COORDINATING = "coordinating"
SYNTHESIZING = "synthesizing"
AWAITING_APPROVAL = "awaiting_approval"
BLOCKED = "blocked"
DEGRADED = "degraded"
PAUSED = "paused"

# Trigger kinds that would launch an autonomous workflow (subject to the daily
# budget). Briefs and alerts are not workflow launches and do not count.
_WORKFLOW_TRIGGERS = frozenset({
    "drawdown_warning", "drawdown_control", "drift_breach", "regime_flip",
})


@dataclass(frozen=True)
class BobConfig:
    max_autonomous_workflows_per_day: int = 3
    max_bob_turns_per_task: int = 6
    regime_cooldown_sessions: int = 1
    drift_threshold: float = 0.05


@dataclass(frozen=True)
class Trigger:
    kind: str
    action: str            # brief | alert | block | workflow | pause_proposals
    template_id: str | None
    payload: dict
    state_hash: str


class BobSupervisor:
    """Deterministic desk supervisor. No execution or proposal authority."""

    def __init__(
        self,
        registry: Registry,
        *,
        coordinator_available: Callable[[], bool],
        config: BobConfig | None = None,
        id_gen: Callable[[], str] | None = None,
    ):
        self.registry = registry
        self._coordinator_available = coordinator_available
        self.config = config or BobConfig()
        self._id_gen = id_gen or (lambda: uuid.uuid4().hex[:16])
        if self.registry.get_bob_state() is None:
            self.registry.save_bob_state(
                {"mode": "observe", "state": STARTING}, MANAGER_ID)

    # -- mode / lifecycle controls ------------------------------------------
    def status(self) -> dict:
        state = self.registry.get_bob_state(MANAGER_ID) or {}
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
        self.registry.record_event("bob_mode", {"mode": mode})
        return self.status()

    def pause(self) -> dict:
        return self.set_mode("paused")

    def resume(self, mode: str = "observe") -> dict:
        if mode == "paused":
            raise ValueError("resume target cannot be 'paused'")
        return self.set_mode(mode)

    # -- the observe tick (deterministic; no LLM for unchanged health) ------
    def observe(self, facts: dict, *, trading_date: str) -> dict:
        """Evaluate triggers against owner facts and persist any new tasks.

        Returns a summary: the resolved lifecycle state, the deterministic desk
        brief, and the tasks created this tick (deduplicated). In Observe mode
        no workforce is launched; workflow-action triggers are recorded as
        queued tasks for a human or a higher mode to act on.
        """
        state_row = self.registry.get_bob_state(MANAGER_ID) or {}
        mode = state_row.get("mode", "observe")
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
                dedupe = self._dedupe_key(trig, trading_date, facts)
                task_id = self._id_gen()
                if self.registry.create_bob_task(
                        task_id, dedupe, trig.kind, trig.payload, trig.template_id):
                    self.registry.record_event(
                        "bob_task", {"task_id": task_id, "trigger": trig.kind,
                                     "action": trig.action})
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
                {"reason": "drift threshold exceeded", "drift": round(float(drift), 4)}))

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
           payload: dict) -> Trigger:
        return Trigger(kind=kind, action=action, template_id=template,
                       payload=payload, state_hash=_hash(payload))

    def _dedupe_key(self, trig: Trigger, trading_date: str, facts: dict) -> str:
        universe = ",".join(sorted(facts.get("universe", [])))
        return f"{trig.kind}|{trading_date}|{universe}|{trig.state_hash}"

    def _within_daily_budget(self, trading_date: str) -> bool:
        # Bounds autonomous WORKFLOW launches per day (briefs/alerts do not
        # count). Task volume per day is tiny, so a bounded scan is fine.
        day = trading_date[:10]
        used = sum(
            1 for task in self.registry.list_bob_tasks(200)
            if str(task.get("created_at", "")).startswith(day)
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
        return OBSERVING

    def _patch_state(self, **fields) -> None:
        current = self.registry.get_bob_state(MANAGER_ID) or {}
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
        self.registry.save_bob_state(merged, MANAGER_ID)


def _hash(payload: dict) -> str:
    import json

    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:12]
