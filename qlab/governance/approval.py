"""Human approval as a persisted, exact-plan-bound, expiring record.

Invariant #14: no CLI argument or HTTP body may impersonate human approval with
``{human_confirmed: true}``. Real execution consumes a persisted
``approval_requests`` record that a human explicitly approved, bound to the
exact plan, targets, data permit, and book state it was approved against. If any
of those changed, the approval no longer covers the plan and is invalidated
rather than silently executed.

The digests are content addresses over the *material* facts, so a re-proposed or
mutated plan, or a book that moved since approval, produces a different digest
and fails the binding check.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from qlab.state.registry import targets_hash


def _sha(material) -> str:
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
        .encode()).hexdigest()[:16]


def plan_digest(plan: dict) -> str:
    """Content address of a checked plan's material execution facts."""
    legs = plan.get("legs") or []
    material = {
        "decision_id": plan.get("decision_id"),
        "targets_hash": targets_hash(plan.get("targets") or {}),
        "legs": sorted(
            [str(leg.get("client_order_id")), str(leg.get("ticker")),
             str(leg.get("side")), round(float(leg.get("notional", 0.0)), 6)]
            for leg in legs),
    }
    return _sha(material)


def book_revision(positions: dict) -> str:
    """Content address of the current book (non-zero positions only)."""
    material = sorted(
        [ticker, round(float(pos.get("qty", 0.0)), 8)]
        for ticker, pos in (positions or {}).items()
        if abs(float(pos.get("qty", 0.0))) > 1e-12)
    return _sha(material)


def build_approval_request(
    plan: dict,
    *,
    broker: str,
    data_permit_id: str | None,
    current_book_revision: str,
    summary: dict,
    ttl_seconds: int = 900,
    now: datetime | None = None,
    task_id: str | None = None,
    approval_id: str | None = None,
) -> dict:
    """Assemble a pending approval request bound to ``plan``.

    The plan must be a persisted *checked* plan (its legs are the exact orders
    the human is approving). ``now`` defaults to wall clock; tests pass it.
    """
    if plan.get("state") != "checked":
        raise ValueError(
            f"only a checked plan can be approved; plan is {plan.get('state')!r}")
    now = now or datetime.now(timezone.utc)
    expires_at = (now + timedelta(seconds=int(ttl_seconds))).isoformat()
    return {
        "approval_id": approval_id or uuid.uuid4().hex[:16],
        "task_id": task_id,
        "plan_id": plan["plan_id"],
        "plan_digest": plan_digest(plan),
        "decision_id": plan.get("decision_id"),
        "targets_hash": targets_hash(plan.get("targets") or {}),
        "data_permit_id": data_permit_id,
        "broker": broker,
        "book_revision": current_book_revision,
        "expected_cost": (plan.get("pre_trade") or {}).get("expected_cost"),
        "summary": summary,
        "expires_at": expires_at,
    }


def check_approval_for_execution(
    approval: dict,
    plan: dict,
    *,
    current_book_revision: str,
    now_iso: str,
    data_permit_id: str | None = None,
) -> list[str]:
    """Return reasons the approval does NOT cover this plan right now (empty = ok).

    Fail-closed: a missing/unapproved/expired approval, or any drift in the
    plan, targets, book, or data permit since approval, refuses execution.
    """
    reasons: list[str] = []
    if not approval:
        return ["no approval record"]
    if approval.get("status") != "approved":
        reasons.append(
            f"approval status is {approval.get('status')!r}, not 'approved'")
    expires_at = approval.get("expires_at")
    if expires_at and str(expires_at) <= now_iso:
        reasons.append("approval has expired")
    if approval.get("plan_id") != plan.get("plan_id"):
        reasons.append("approval is for a different plan")
    if approval.get("plan_digest") != plan_digest(plan):
        reasons.append("plan changed since approval (digest mismatch)")
    if approval.get("targets_hash") != targets_hash(plan.get("targets") or {}):
        reasons.append("targets changed since approval")
    if approval.get("book_revision") != current_book_revision:
        reasons.append("book moved since approval (revision mismatch)")
    if (data_permit_id is not None
            and approval.get("data_permit_id") not in (None, data_permit_id)):
        reasons.append("data permit changed since approval")
    return reasons
