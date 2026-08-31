"""The single current proposal: the one thing the desk wants answered.

A checked plan with an open approval request is the desk asking a question.
Nothing used to close the older question when a newer plan was checked, so an
operator could face two live requests and no statement of which one the desk
meant — and either could still be approved and booked.

So: the newest checked plan with a live request is *the* proposal, and every
other live request — pending, and approved-but-never-booked alike — is
invalidated with a reason that names its successor. The invalidation is the
registry's own ``{pending, approved} -> invalidated`` edge, the same one a
drifted book takes; there is no second state machine and no new terminal
status. Naming the reason is what keeps it a fail-loud act — a
superseded proposal is withdrawn on the record, never dropped.

Read-only apart from that one transition, and it takes no lock: both callers
(the owner's tick and ``GET /api/desk/proposal``) already run under the owner
dispatch lock, which is not reentrant.
"""

from __future__ import annotations

from datetime import datetime, timezone

from qlab.state.registry import targets_hash

# How many approval requests are read to find the desk's open questions. The
# search is driven from THIS side, never from a window over the plans table: a
# desk that checks plans faster than it books them would otherwise push a live
# request out of view, and a proposal nothing can see is a proposal nothing
# withdraws — it just stays bookable.
_APPROVAL_WINDOW = 200

# The statuses that still make a request the desk's open question: `pending` is
# what approve/reject bind to, `approved` (unspent) is what the execute gate
# consumes. Everything else — consumed, rejected, expired, invalidated — is
# terminal, and a plan behind one of those is not being asked about.
_LIVE = ("pending", "approved")


def supersession_reason(keep_plan_id: str) -> str:
    """The exact reason string a superseded approval carries."""
    return f"superseded by {keep_plan_id}"


def supersede(registry, keep_plan_id: str) -> tuple[list[str], list[dict]]:
    """Invalidate every *other* live approval; return ``(withdrawn, failures)``.

    Live means what it means everywhere else here: ``pending``, and
    ``approved`` that was never consumed. A plan the operator approved and
    never booked is exactly the stale allocation they must not be shown beside
    a newer one — leaving it alive would keep a second bookable allocation on
    the desk, which is the state this whole task exists to remove. Terminal
    rows — consumed, rejected, expired, already invalidated — are untouched: a
    booked plan is history, not a question.

    One try per row, and both outcomes are returned. Raising out of the loop
    would abandon the invalidations that already committed: the caller's
    ``except`` announced nothing, so an approval could be dead in the registry
    with the chat never saying so — a silent withdrawal, which is the one
    failure mode this module exists to rule out. A row that could not be moved
    is reported, not swallowed, so the desk can say that too.

    Idempotent by construction: the second call finds the rows already
    ``invalidated`` and so has nothing to name, which is what bounds the
    announcement to once per superseded plan.
    """
    keep = str(keep_plan_id or "")
    if not keep:
        raise ValueError("supersede needs the plan id to keep")
    withdrawn: list[str] = []
    failures: list[dict] = []
    for plan_id, row in live_requests(registry).items():
        if plan_id == keep:
            continue
        approval_id = str(row.get("approval_id") or "")
        try:
            registry.transition_approval(
                approval_id, "invalidated",
                invalidated_reason=supersession_reason(keep))
        except Exception as exc:  # one bad row must not hide the good ones
            failures.append({"plan_id": plan_id, "approval_id": approval_id,
                             "status": str(row.get("status") or ""),
                             "error": str(exc)[:200]})
            continue
        if plan_id not in withdrawn:
            withdrawn.append(plan_id)
    return withdrawn, failures


def live_requests(registry, now_iso: str | None = None) -> dict[str, dict]:
    """The desk's open questions: newest live request per plan id.

    Public because the announcement needs the state it is about to withdraw —
    "superseded (approved, unbooked)" and "superseded (pending)" are different
    facts for an operator — and because ``current_proposal`` is driven from
    here rather than from the plans table.

    Expiry is applied as a *read*: a request past ``expires_at`` is not an open
    question, whatever its stored status still says. Deliberately not by
    calling the sweeper — this runs on a GET, and a read that writes would make
    every client poll a registry mutation.

    Ordering is made total here rather than trusted from SQL. The registry
    orders by ``created_at`` alone, and two requests minted in the same tick
    carry the same timestamp, so "newest" was a coin flip between them; the
    ``approval_id`` tiebreak makes the desk's answer the same on every read.
    """
    now = now_iso or datetime.now(timezone.utc).isoformat()
    rows = sorted(
        registry.list_approval_requests(_APPROVAL_WINDOW),
        key=lambda row: (str(row.get("created_at") or ""),
                         str(row.get("approval_id") or "")),
        reverse=True)
    live: dict[str, dict] = {}
    for row in rows:
        plan_id = str(row.get("plan_id") or "")
        if not plan_id or plan_id in live:
            continue
        if str(row.get("status") or "") not in _LIVE:
            continue
        if row.get("consumed_at"):
            continue
        expires_at = row.get("expires_at")
        if expires_at and str(expires_at) <= now:
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


def current_proposal(registry, now_iso: str | None = None) -> dict | None:
    """The one proposal the desk wants answered, or ``None``.

    Driven from the live requests, and each is resolved with ``get_plan``: the
    newest *checked* plan behind one of them is the proposal. The first version
    walked the newest plans instead and asked which had a request, which made
    the answer depend on how many plans had been proposed since — sixty refused
    previews were enough to hide a live approved request from the route AND
    from the supersession that should have withdrawn it.

    A plan whose request was consumed, rejected, expired or invalidated is not
    the proposal; the desk withdrew that question and must not fall back to it.
    """
    live = live_requests(registry, now_iso)
    if not live:
        return None
    candidates = []
    for plan_id, request in live.items():
        plan = registry.get_plan(plan_id)
        if plan is None or plan.get("state") != "checked":
            continue
        candidates.append((str(plan.get("created_at") or ""), plan_id,
                           plan, request))
    if not candidates:
        return None
    # Newest wins; the plan_id breaks a same-timestamp tie so two plans checked
    # in one tick do not make the desk's own question a coin flip.
    _, plan_id, plan, request = max(candidates, key=lambda c: (c[0], c[1]))
    targets = plan.get("targets") or {}
    plan_hash = targets_hash(targets)
    reason = supersession_reason(plan_id)
    superseded = []
    for row in registry.list_approval_requests(_APPROVAL_WINDOW, "invalidated"):
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
