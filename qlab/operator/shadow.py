"""Shadow-rollout scorecard: is Atlas worth trusting with more authority?

Phase 9 is an operator-run evaluation, not an automatic promotion. Atlas runs in
Observe/Research, produces alerts and proposals, the human decides, and nothing
executes automatically. This module computes the evidence that comparison needs
from what is already persisted — task history, approvals, and model invocations
— so the decision rests on recorded behavior rather than impression.

Deliberately NOT here: any standing paper-authority grant. Promotion requires a
separate design review of the grant schema, revocation, and anomaly pauses, so
nothing in this module can widen Atlas's authority.
"""

from __future__ import annotations

from collections import Counter


def _day(row: dict, field: str = "created_at") -> str:
    return str(row.get(field) or "")[:10]


def shadow_scorecard(registry, *, since: str | None = None,
                     limit: int = 500) -> dict:
    """Summarize Atlas's shadow behavior over recent history.

    ``since`` is an ISO date; rows created before it are ignored. Every figure
    is a count of persisted facts — nothing is inferred or modeled.

    **Trigger work only.** These are the statistics that say whether Atlas's
    unattended judgment is worth more authority, and a proposal is attended by
    construction: a human asked for it and a human approved it. Counted here it
    would fill ``by_trigger`` with ``proposal:<template_id>`` keys, and an
    approved proposal that concluded ``action_taken: False`` would be scored as
    a FALSE TRIGGER — when no trigger fired and the desk did exactly what it
    was asked. Whether proposals deserve their own scorecard (approved against
    refused, which is a different question) is an open one; mixing them into
    this one biases the measurement that governs promotion.
    """
    tasks = [t for t in registry.list_atlas_tasks(limit, origin="trigger")
             if not since or _day(t) >= since[:10]]
    approvals = [a for a in registry.list_approval_requests(limit)
                 if not since or _day(a) >= since[:10]]
    invocations = [m for m in registry.list_model_invocations(limit)
                   if not since or _day(m) >= since[:10]]

    by_status = Counter(str(t.get("status")) for t in tasks)
    by_trigger = Counter(str(t.get("trigger_kind")) for t in tasks)
    # A task whose conclusion recorded no action is a trigger that fired without
    # earning its wake — the "false trigger" rate the rollout is watching.
    no_action = sum(
        1 for t in tasks
        if (t.get("conclusion") or {}).get("action_taken") is False)
    completed = by_status.get("completed", 0)

    approval_status = Counter(str(a.get("status")) for a in approvals)
    decided = (approval_status.get("approved", 0)
               + approval_status.get("rejected", 0)
               + approval_status.get("consumed", 0))
    # Proposal validity: of the proposals a human actually ruled on, how many
    # were accepted. Expired/invalidated ones were never ruled on.
    accepted = approval_status.get("approved", 0) + approval_status.get("consumed", 0)

    tokens = sum(int(m.get("tokens") or 0) for m in invocations)
    by_tier = Counter(str(m.get("requested_tier")) for m in invocations)
    fallbacks = sum(1 for m in invocations if m.get("fallback_reason"))

    return {
        "since": since,
        "tasks": {
            "total": len(tasks),
            "by_status": dict(by_status),
            "by_trigger": dict(by_trigger),
            "completed": completed,
            "failed": by_status.get("failed", 0),
            "blocked": by_status.get("blocked", 0),
            "no_action_conclusions": no_action,
            # Of the work that finished, how much concluded nothing was needed.
            "false_trigger_rate": (no_action / completed) if completed else None,
        },
        "approvals": {
            "total": len(approvals),
            "by_status": dict(approval_status),
            "decided": decided,
            "accepted": accepted,
            "acceptance_rate": (accepted / decided) if decided else None,
            "expired": approval_status.get("expired", 0),
            "invalidated": approval_status.get("invalidated", 0),
        },
        "model_cost": {
            "invocations": len(invocations),
            "tokens": tokens,
            "by_tier": dict(by_tier),
            "fallbacks": fallbacks,
        },
        "readiness": _readiness(tasks, approvals, by_status, approval_status),
    }


def _readiness(tasks, approvals, by_status, approval_status) -> dict:
    """A blunt, conservative read on whether more authority is even discussable.

    This is decision *support*, never a promotion: the answer is always for a
    human, and a separate design review gates any standing grant regardless.
    """
    blockers: list[str] = []
    if len(tasks) < 10:
        blockers.append(
            f"only {len(tasks)} autonomous task(s) observed; too little history")
    if by_status.get("failed", 0) or by_status.get("blocked", 0):
        blockers.append(
            f"{by_status.get('failed', 0)} failed and "
            f"{by_status.get('blocked', 0)} blocked task(s) in the window")
    if approval_status.get("invalidated", 0):
        blockers.append(
            f"{approval_status['invalidated']} proposal(s) went stale before a "
            "decision — proposals are not tracking the book")
    if len(approvals) < 3:
        blockers.append(
            f"only {len(approvals)} proposal(s) reviewed; not enough to judge "
            "proposal validity")
    return {
        "sufficient_evidence": not blockers,
        "blockers": blockers,
        "note": ("Evidence summary only. Standing paper authority requires a "
                 "separate design review of the grant schema, revocation, and "
                 "anomaly pauses; nothing here grants authority."),
    }
