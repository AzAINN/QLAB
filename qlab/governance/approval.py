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

from qlab.state.registry import (
    APPROVAL_KIND_PLAN,
    APPROVAL_KIND_UNIVERSE_CHANGE,
    targets_hash,
)


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
        "kind": APPROVAL_KIND_PLAN,
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


def build_universe_change_request(
    ticker: str,
    *,
    memo_decision_id: str,
    task_id: str | None = None,
    approval_id: str | None = None,
) -> dict:
    """A pending request to admit one contender into the mandate's universe.

    It binds no plan, no targets, no book and no data permit, because it
    authorises none of those: approving it widens what the desk may research
    and later propose, and every proposal that follows still needs its own
    plan-bound approval. The two facts it carries are the ticker and the scout
    memo the operator can read before answering, and they ride in ``summary``
    rather than in new columns — the plan-shaped columns stay NULL, which is
    what makes an execution check's refusal unambiguous.

    No expiry. A plan approval expires because the book moves under it; a
    question about the universe stays true until it is answered, and an
    operator who comes back tomorrow should find it waiting rather than
    silently expired.
    """
    name = str(ticker or "").strip().upper()
    if not name:
        raise ValueError("a universe_change request needs a ticker")
    memo = str(memo_decision_id or "").strip()
    if not memo:
        raise ValueError(
            "a universe_change request needs the scout memo's decision_id; a "
            "contender with no memo behind it is a name from nowhere")
    return {
        "approval_id": approval_id or uuid.uuid4().hex[:16],
        "kind": APPROVAL_KIND_UNIVERSE_CHANGE,
        "task_id": task_id,
        "plan_id": None,
        "plan_digest": None,
        "decision_id": None,
        "targets_hash": None,
        "data_permit_id": None,
        "broker": None,
        "book_revision": None,
        "expected_cost": None,
        "summary": {"ticker": name, "memo_decision_id": memo},
        "expires_at": None,
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
    # By kind, first and unconditionally. A universe_change carries none of the
    # bindings below, so every one of them would be a mismatch anyway — but a
    # refusal assembled out of six "changed since approval" lines reads as a
    # drifted plan rather than as an approval that can never book anything.
    kind = str(approval.get("kind") or APPROVAL_KIND_PLAN)
    if kind != APPROVAL_KIND_PLAN:
        return [f"approval is a {kind!r} request, which binds no plan and can "
                f"never authorise execution"]
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
