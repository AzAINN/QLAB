"""The single current proposal: the one thing the desk wants answered.

A checked plan with an open approval request is the desk asking a question.
Nothing used to close the older question when a newer plan was checked, so an
operator could face two live requests and no statement of which one the desk
meant — and either could still be approved and booked.

So: the newest checked plan with a live request is *the* proposal, and every
other pending request is invalidated with a reason that names its successor.
The invalidation is the registry's own ``pending -> invalidated`` edge, the
same one a drifted book takes; there is no second state machine and no new
terminal status. Naming the reason is what keeps it a fail-loud act — a
superseded proposal is withdrawn on the record, never dropped.

Read-only apart from that one transition, and it takes no lock: both callers
(the owner's tick and ``GET /api/desk/proposal``) already run under the owner
dispatch lock, which is not reentrant.
"""

from __future__ import annotations

from qlab.state.registry import targets_hash

# How far back a proposal may be found. The desk asks about the newest checked
# plan; a window this wide is generous for a surface that holds one question.
_PLAN_WINDOW = 50
_APPROVAL_WINDOW = 200

# The statuses that still make a request the desk's open question: `pending` is
# what approve/reject bind to, `approved` (unspent) is what the execute gate
# consumes. Everything else — consumed, rejected, expired, invalidated — is
# terminal, and a plan behind one of those is not being asked about.
_LIVE = ("pending", "approved")


def supersession_reason(keep_plan_id: str) -> str:
    """The exact reason string a superseded approval carries."""
    return f"superseded by {keep_plan_id}"


def supersede(registry, keep_plan_id: str) -> list[str]:
    """Invalidate every *other* pending approval; return their plan ids.

    Only pending requests. An approval a human already granted is their
    decision, not the desk's question, and revoking it here would let a fresh
    plan silently cancel a fill the operator had already authorised — the
    execute gate's own binding checks (plan digest, targets, book revision)
    are what refuse a stale approved one.

    Idempotent by construction: the second call finds the rows already
    ``invalidated`` and so has nothing to name, which is what bounds the
    announcement to once per superseded plan.
    """
    keep = str(keep_plan_id or "")
    if not keep:
        raise ValueError("supersede needs the plan id to keep")
    superseded: list[str] = []
    for row in registry.list_approval_requests(_APPROVAL_WINDOW, "pending"):
        plan_id = str(row.get("plan_id") or "")
        if not plan_id or plan_id == keep:
            continue
        registry.transition_approval(
            str(row["approval_id"]), "invalidated",
            invalidated_reason=supersession_reason(keep))
        if plan_id not in superseded:
            superseded.append(plan_id)
    return superseded


def _live_requests(registry) -> dict[str, dict]:
    """Newest live request per plan id."""
    live: dict[str, dict] = {}
    rows = registry.list_approval_requests(_APPROVAL_WINDOW)
    # list_approval_requests is newest first, so the first row for a plan wins.
    for row in rows:
        plan_id = str(row.get("plan_id") or "")
        if not plan_id or plan_id in live:
            continue
        if str(row.get("status") or "") not in _LIVE:
            continue
        if row.get("consumed_at"):
            continue
        live[plan_id] = row
    return live


def _referee(registry, plan: dict, plan_hash: str) -> dict | None:
    """The PASS that covers these exact targets, if one is on the record.

    Looked up by decision and then held to the hash, exactly as
    ``rebalance_preview`` does: a verdict for the decision that does not cover
    these targets covers nothing here either.
    """
    decision_id = str(plan.get("decision_id") or "")
    if not decision_id:
        return None
    verdict = registry.get_verdict(decision_id)
    if not verdict or str(verdict.get("targets_hash") or "") != plan_hash:
        return None
    return {
        "verdict_id": verdict.get("verdict_id"),
        "verdict": verdict.get("verdict"),
        "reasons": verdict.get("reasons") or [],
        "source": verdict.get("source"),
        "targets_hash": verdict.get("targets_hash"),
        "created_at": verdict.get("created_at"),
    }


def current_proposal(registry) -> dict | None:
    """The one proposal the desk wants answered, or ``None``.

    The newest checked plan that still has a live (pending or approved,
    unspent) request. A plan whose request was consumed, rejected, expired or
    invalidated is not the proposal — the desk withdrew that question and must
    not fall back to it.
    """
    live = _live_requests(registry)
    if not live:
        return None
    for plan in registry.list_plans(_PLAN_WINDOW):
        plan_id = str(plan.get("plan_id") or "")
        if not plan_id or plan.get("state") != "checked":
            continue
        request = live.get(plan_id)
        if request is None:
            continue
        targets = plan.get("targets") or {}
        plan_hash = targets_hash(targets)
        reason = supersession_reason(plan_id)
        superseded = []
        for row in registry.list_approval_requests(_APPROVAL_WINDOW,
                                                   "invalidated"):
            other = str(row.get("plan_id") or "")
            if str(row.get("invalidated_reason") or "") != reason:
                continue
            if other and other not in superseded:
                superseded.append(other)
        return {
            "plan_id": plan_id,
            "approval_id": request.get("approval_id"),
            "approval_state": request.get("status"),
            "expires_at": request.get("expires_at"),
            "decision_id": plan.get("decision_id"),
            "targets": targets,
            "targets_hash": plan_hash,
            "pre_trade": plan.get("pre_trade") or {},
            "referee": _referee(registry, plan, plan_hash),
            "created_at": plan.get("created_at"),
            "superseded": superseded,
        }
    return None
