"""Role → model-tier routing, with an auditable invocation record.

Two rules the plan is explicit about (§9.6):

* **Tiers, not brand names, are the architecture.** A role is configured as
  ``deep`` (judgment), ``quick`` (mechanical), or ``none``; the concrete model
  that serves a tier is a deployment detail resolved here in one place, not
  scattered through TUI code. Swapping the model behind a tier must never
  change a role's authority or its tools.
* **Every resolution is auditable.** Which tier was requested, which model
  served it, whether a fallback was used and why — recorded per invocation, so
  a phase that ran on a degraded model cannot quietly be read as a clean PASS.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

DEEP = "deep"
QUICK = "quick"
NONE = "none"
TIERS = (DEEP, QUICK, NONE)

# Judgment roles reason about estimation, adversarial critique, and approval;
# mechanical roles execute a cataloged step or format an existing result.
ROLE_TIER: dict[str, str] = {
    "moments-analyst": DEEP,
    "challenger": DEEP,
    "referee": DEEP,
    "optimization-runner": QUICK,
    "reporter": QUICK,
    "data-qa": QUICK,
    "signal-qa": QUICK,
}

# The one place a tier becomes a concrete model. ``inherit`` means "whatever
# model runs the coordinator" — the deep tier deliberately follows the session
# so the desk's judgment always runs on the operator's chosen frontier model.
TIER_MODEL: dict[str, str] = {
    DEEP: "inherit",
    QUICK: "sonnet",
    NONE: "inherit",
}

# Roles whose failure must not be reported as a clean result: if a deep-tier
# role could not run on its tier, the phase is degraded, not PASS.
REQUIRED_DEEP_ROLES = frozenset({"referee"})

# Fast mode: run the judgment roles on the quick model too, so a whole review
# finishes in the time the deep tier alone would take. It is a speed/quality
# trade the operator makes explicitly, and it is bounded — REQUIRED_DEEP_ROLES
# keeps its tier, because the approval gate is the one place where a cheaper
# answer is worth nothing. A PASS must never mean "passed on the fast model".
FAST_TIER_MODEL: dict[str, str] = {
    DEEP: "sonnet",
    QUICK: "sonnet",
    NONE: "inherit",
}


def tier_model_for(*, fast: bool = False) -> dict[str, str]:
    """The tier→model map in force. `fast` trades depth for latency."""
    return dict(FAST_TIER_MODEL if fast else TIER_MODEL)


@dataclass(frozen=True)
class RouteDecision:
    role: str
    requested_tier: str
    resolved_model: str
    source: str                 # "agent_override" | "tier" | "unknown_role"
    fallback_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "role": self.role, "requested_tier": self.requested_tier,
            "resolved_model": self.resolved_model, "source": self.source,
            "fallback_reason": self.fallback_reason,
        }


def tier_for(role: str) -> str:
    """The configured tier for ``role`` (``none`` when unregistered)."""
    return ROLE_TIER.get(role, NONE)


def resolve_route(role: str, *, source_model: str | None = None,
                  tier_model: dict[str, str] | None = None,
                  fast: bool = False) -> RouteDecision:
    """Resolve which model serves ``role``.

    A concrete ``model:`` in the agent source always wins (an explicit operator
    override); otherwise the role's tier is resolved through ``tier_model``.

    ``fast`` drops the judgment roles onto the quick model — except the roles in
    REQUIRED_DEEP_ROLES, which keep their tier no matter what. Speed is the
    operator's call everywhere except the gate that decides whether a trade may
    be proposed at all.
    """
    models = tier_model or tier_model_for(fast=fast)
    if source_model and source_model != "inherit":
        return RouteDecision(role=role, requested_tier=tier_for(role),
                             resolved_model=source_model, source="agent_override")
    tier = tier_for(role)
    if fast and role in REQUIRED_DEEP_ROLES:
        # Recorded as a deliberate exemption, so an audit shows fast mode was on
        # and shows which role refused it.
        return RouteDecision(
            role=role, requested_tier=tier,
            resolved_model=TIER_MODEL[tier], source="tier",
            fallback_reason="fast mode does not apply to a required-deep role")
    if role not in ROLE_TIER:
        return RouteDecision(role=role, requested_tier=NONE,
                             resolved_model=models.get(NONE, "inherit"),
                             source="unknown_role",
                             fallback_reason=f"role {role!r} has no configured tier")
    return RouteDecision(role=role, requested_tier=tier,
                         resolved_model=models[tier], source="tier")


def record_invocation(registry, decision: RouteDecision, *,
                      status: str = "ok", backend: str = "claude_cli",
                      latency_ms: float | None = None,
                      tokens: int | None = None,
                      invocation_id: str | None = None) -> str:
    """Persist one model invocation and emit its route event.

    The registry is the single writer; this only assembles the record.

    ``backend`` names the process that served the role, not the model — a
    second coordinator (``bob_shell``) would pass its own value here so a phase
    cannot be read as clean without knowing what actually ran it.
    """
    invocation_id = invocation_id or uuid.uuid4().hex[:16]
    registry.record_model_invocation({
        "invocation_id": invocation_id,
        "role": decision.role,
        "requested_tier": decision.requested_tier,
        "resolved_model": decision.resolved_model,
        "backend": backend,
        "status": status,
        "latency_ms": latency_ms,
        "tokens": tokens,
        "fallback_reason": decision.fallback_reason,
    })
    registry.record_event(
        "model.fallback_used" if decision.fallback_reason else "model.route_resolved",
        decision.to_dict())
    return invocation_id


def degrades_result(role: str, decision: RouteDecision) -> bool:
    """True when ``role`` required its deep tier but did not get it.

    A required deep role served by a fallback cannot be reported as a clean
    PASS — the caller must mark the phase degraded.
    """
    if role not in REQUIRED_DEEP_ROLES:
        return False
    return decision.requested_tier != DEEP or decision.fallback_reason is not None
