"""Standing paper authority: an expiring, revocable, code-checked grant.

**This is inert by default and must stay that way until reviewed.** No grant
exists unless a human creates one, no code path creates one on its own, and Atlas
can neither create, edit, nor read-around one. The plan requires a separate
design review of the grant schema, revocation, anomaly pauses, and the operator
experience before standing authority is actually used; this module implements
the schema and the checks so that review has something concrete to examine, not
so the desk can start trading unattended.

Design rules, all enforced here in deterministic code:

* **Expiring, never open-ended.** A grant without a future expiry is invalid.
* **Revocable immediately.** Revocation is checked before anything else and is
  never blocked by any other condition.
* **Scoped.** Universe, notional, turnover, and order count are ceilings the
  plan must fit inside; a plan touching anything outside the allowed universe
  is refused whole, not trimmed to fit. Books per day bounds how *many* plans
  a grant covers, which the per-plan ceilings alone never did.
* **Anomaly-paused.** A halted book, a dirty reconcile, a stale data permit, or
  a recent order anomaly suspends a grant without revoking it.
* **Never sufficient alone.** A grant replaces the *per-plan human approval*,
  not the mandate, the referee gate, the cost gate, or execution-time
  revalidation. Those all still run.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

# Grants are scoped to paper only. There is no live mode and adding one would
# be a different review.
PAPER_AUTO = "paper_auto"
VALID_MODES = (PAPER_AUTO,)

# A grant longer than this is refused: standing authority should be renewed
# deliberately, not left running.
MAX_GRANT_DAYS = 30


class AuthorityError(ValueError):
    """A grant is malformed or a request falls outside it."""


def build_grant(
    *,
    allowed_universe: list[str],
    max_notional: float,
    max_turnover: float,
    max_orders: int,
    max_books_per_day: int,
    allowed_policy: str,
    granted_by: str,
    ttl_days: int = 7,
    now: datetime | None = None,
    mode: str = PAPER_AUTO,
    grant_id: str | None = None,
) -> dict:
    """Assemble a standing grant. Every field is a ceiling, none is a default."""
    if mode not in VALID_MODES:
        raise AuthorityError(
            f"mode must be one of {VALID_MODES}; there is no live authority")
    if not allowed_universe:
        raise AuthorityError("a grant must name its allowed universe")
    if not str(granted_by or "").strip():
        raise AuthorityError("a grant must record who granted it")
    for name, value in (("max_notional", max_notional),
                        ("max_turnover", max_turnover),
                        ("max_orders", max_orders),
                        ("max_books_per_day", max_books_per_day)):
        if value is None or float(value) <= 0:
            raise AuthorityError(f"{name} must be a positive ceiling")
    if not 1 <= int(ttl_days) <= MAX_GRANT_DAYS:
        raise AuthorityError(
            f"ttl_days must be within 1..{MAX_GRANT_DAYS}; standing authority "
            "is renewed deliberately, never open-ended")
    now = now or datetime.now(timezone.utc)
    return {
        "grant_id": grant_id or uuid.uuid4().hex[:16],
        "mode": mode,
        "allowed_universe": sorted(allowed_universe),
        "max_notional": float(max_notional),
        "max_turnover": float(max_turnover),
        "max_orders": int(max_orders),
        "max_books_per_day": int(max_books_per_day),
        "allowed_policy": allowed_policy,
        "valid_from": now.isoformat(),
        "expires_at": (now + timedelta(days=int(ttl_days))).isoformat(),
        "granted_by": granted_by,
    }


def check_grant_covers(
    grant: dict | None,
    plan: dict,
    *,
    now_iso: str,
    policy_id: str,
    anomalies: list[str] | None = None,
    books_today: int | None = None,
) -> list[str]:
    """Reasons this grant does NOT cover this plan right now (empty = covered).

    Fail-closed at every branch: a missing, revoked, expired, out-of-scope,
    anomaly-suspended, or daily-spent grant refuses.

    `books_today` is how many books this grant has already made on today's
    trading date, counted by the caller — this module never touches the
    registry. It has no default that means "unlimited": an unsupplied count is
    a ceiling that cannot be evaluated, and that refuses.
    """
    if not grant:
        return ["no standing authority grant"]
    reasons: list[str] = []

    # Revocation first and unconditionally — it must never be outrankable.
    if grant.get("revoked_at"):
        return [f"grant revoked at {grant['revoked_at']}: "
                f"{grant.get('revoked_reason') or 'no reason recorded'}"]

    valid_from = str(grant.get("valid_from") or "")
    expires_at = str(grant.get("expires_at") or "")
    if not expires_at:
        return ["grant has no expiry; open-ended authority is invalid"]
    if valid_from and now_iso < valid_from:
        reasons.append("grant is not yet in effect")
    if now_iso >= expires_at:
        reasons.append(f"grant expired at {expires_at}")
    if grant.get("mode") not in VALID_MODES:
        reasons.append(f"grant mode {grant.get('mode')!r} is not a paper mode")

    allowed_policy = grant.get("allowed_policy")
    if allowed_policy and policy_id != allowed_policy:
        reasons.append(
            f"grant covers policy {allowed_policy!r}, plan used {policy_id!r}")

    allowed = set(grant.get("allowed_universe") or [])
    targets = plan.get("targets") or {}
    outside = sorted(set(targets) - allowed)
    if outside:
        # Refuse the plan whole; never trim it to fit the grant.
        reasons.append(f"plan touches symbols outside the grant: {outside}")

    pre_trade = plan.get("pre_trade") or {}
    legs = plan.get("legs") or []
    notional = sum(abs(float(leg.get("notional", 0.0))) for leg in legs)
    if notional > float(grant.get("max_notional") or 0.0):
        reasons.append(
            f"plan notional {notional:.2f} exceeds the grant ceiling "
            f"{float(grant.get('max_notional') or 0.0):.2f}")
    turnover = float(pre_trade.get("turnover") or 0.0)
    if turnover > float(grant.get("max_turnover") or 0.0):
        reasons.append(
            f"plan turnover {turnover:.4f} exceeds the grant ceiling "
            f"{float(grant.get('max_turnover') or 0.0):.4f}")
    if len(legs) > int(grant.get("max_orders") or 0):
        reasons.append(
            f"plan has {len(legs)} legs, above the grant ceiling "
            f"{int(grant.get('max_orders') or 0)}")

    # The per-plan ceilings bound one book; this one bounds how many. A grant
    # that names no daily ceiling was written before the ceiling existed, and a
    # missing ceiling refuses — it never reads as unlimited, or the oldest row
    # in a desk's registry would outrank every grant made since.
    books_ceiling = grant.get("max_books_per_day")
    if books_ceiling is None or int(books_ceiling) <= 0:
        reasons.append(
            "grant names no max_books_per_day ceiling; a missing daily "
            "ceiling refuses rather than reading as unlimited")
    elif books_today is None:
        reasons.append(
            "the day's book count is unknown; a daily ceiling that cannot be "
            "evaluated refuses")
    elif int(books_today) >= int(books_ceiling):
        reasons.append(
            f"grant has booked {int(books_today)} today, at its ceiling of "
            f"{int(books_ceiling)}")

    # An anomaly suspends a grant without revoking it: the authority survives,
    # but it does not apply while the desk is in a state a human should see.
    for anomaly in (anomalies or []):
        reasons.append(f"grant suspended by anomaly: {anomaly}")
    return reasons


def detect_anomalies(*, halted: bool, reconcile_clean: bool,
                     data_execution_eligible: bool,
                     recent_order_anomaly: bool) -> list[str]:
    """Deterministic conditions that suspend standing authority."""
    anomalies = []
    if halted:
        anomalies.append("account is halted")
    if not reconcile_clean:
        anomalies.append("ledger and broker do not reconcile")
    if not data_execution_eligible:
        anomalies.append("data is not execution-eligible")
    if recent_order_anomaly:
        anomalies.append("a recent order was rejected or expired")
    return anomalies
