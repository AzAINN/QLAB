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

A route now carries a *backend* as well as a model, because the provider is a
deployment detail for the same reason the model is (``llm_backends``' opening
paragraph). It is the same rule with one exception written into the code:
REQUIRED_CLAUDE_ROLES never leave the Claude backend, whatever the operator
configured.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace

from qlab.core.llm_config import SurfaceModel

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

# The backend a route runs on unless the operator configured another one, and
# the roles that stay on it regardless. REQUIRED_DEEP_ROLES' argument, applied
# one level up: the approval gate must never run on an experimental backend,
# because a PASS that means "passed on whatever provider was being tried this
# week" is not a gate. Configuring the workforce is a deployment choice; this
# is the one role where it is not the operator's to make.
CLAUDE_BACKEND = "claude"
REQUIRED_CLAUDE_ROLES = frozenset({"referee"})

# The process name recorded for a backend — what actually ran the role, which
# is not the same question as which model answered. "claude_cli" predates the
# backend dimension and is kept so old invocation rows and new ones stay one
# series; every other backend records under its registry name.
BACKEND_PROCESS: dict[str, str] = {CLAUDE_BACKEND: "claude_cli"}

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


# Why a decision carries a note — as a fact, not as prose. Three different
# things wrote one sentence into ``fallback_reason`` and the audit read all
# three as "a fallback was used", which is the opposite of true for two of
# them: an exemption fired is the mechanism PROTECTING a role, and an
# unregistered role has not fallen back from anything. Only the third is a
# genuinely cheaper answer. Selecting an event kind by sniffing the sentence
# would rebuild the same conflation one layer up, so the classification lives
# here and the sentence stays purely operator-facing.
NOTE_PINNED = "pinned"              # an exemption fired; the tier was kept
NOTE_UNREGISTERED = "unregistered"  # no configured tier for this role
NOTE_CHEAPENED = "cheapened"        # served below the tier that was asked for

# The event each note publishes under. Cheapened first: it is the only one that
# endangers a result, so when a route is both cheapened and noted for something
# else, the louder fact is the one the bus carries.
ROUTE_EVENT_KIND: dict[str, str] = {
    NOTE_CHEAPENED: "model.fallback_used",
    NOTE_PINNED: "model.route_pinned",
    NOTE_UNREGISTERED: "model.route_unregistered",
}
_KIND_ORDER = (NOTE_CHEAPENED, NOTE_PINNED, NOTE_UNREGISTERED)


@dataclass(frozen=True)
class RouteDecision:
    role: str
    requested_tier: str
    resolved_model: str
    # "agent_override" | "tier" | "unknown_role" | "workforce_config"
    source: str
    backend: str = CLAUDE_BACKEND
    fallback_reason: str | None = None
    # Zero or more of the NOTE_* facts above. ``fallback_reason`` is the same
    # information as a sentence for a human; this is the half code reads.
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "role": self.role, "requested_tier": self.requested_tier,
            "resolved_model": self.resolved_model, "source": self.source,
            "backend": self.backend, "fallback_reason": self.fallback_reason,
            "notes": list(self.notes),
        }


def route_event_kind(decision: RouteDecision) -> str:
    """The audit event ``decision`` publishes under.

    Selected from the notes, never from the reason, so "nothing fell back"
    cannot be published as ``model.fallback_used`` again.
    """
    for note in _KIND_ORDER:
        if note in decision.notes:
            return ROUTE_EVENT_KIND[note]
    return "model.route_resolved"


def _noted(decision: RouteDecision, note: str, reason: str) -> RouteDecision:
    """Add one note and its sentence, keeping any the decision already had."""
    return replace(
        decision, notes=decision.notes + (note,),
        fallback_reason=(f"{decision.fallback_reason}; {reason}"
                         if decision.fallback_reason else reason))


def tier_for(role: str) -> str:
    """The configured tier for ``role`` (``none`` when unregistered)."""
    return ROLE_TIER.get(role, NONE)


def _unknown_role_reason(role: str) -> str | None:
    """The fallback note for a role no tier map knows, or None.

    Read on both routes: whether an unregistered role is flagged is a fact
    about the role, and letting it depend on the configured backend would hide
    a caller's typo on exactly the desks that are experimenting.
    """
    return None if role in ROLE_TIER else f"role {role!r} has no configured tier"


def pinned_to_claude_reason(role: str, backend: str) -> str:
    """Why ``role`` did not follow the configured backend. Audit-facing."""
    return (f"{role} is pinned to claude; the configured {backend} backend "
            "does not serve the approval gate")


def resolve_route(role: str, *, source_model: str | None = None,
                  tier_model: dict[str, str] | None = None,
                  fast: bool = False,
                  workforce: SurfaceModel | None = None,
                  pinned_reason: str = "") -> RouteDecision:
    """Resolve which backend and model serve ``role``.

    ``workforce`` is the operator's configured surface (``llm_config``). A role
    outside REQUIRED_CLAUDE_ROLES follows it wholesale — backend *and* model,
    because an agent file's ``model:`` is a Claude tier alias that no other
    provider can serve, so a route that mixed the two would name a model the
    backend does not have.

    A pinned role does not follow it at all: it resolves exactly as it does on
    an unconfigured desk, and the decision says the pin fired. That is the same
    mechanism fast mode uses for REQUIRED_DEEP_ROLES — the exemption is
    recorded, never silent, so an audit shows what the config asked for.

    ``pinned_reason`` is a caller's own pin: a session that cannot serve this
    role on the configured backend says why, and the role then resolves exactly
    as a claude-pinned one does. The driver uses it for a multi-phase graph,
    which only the Claude coordinator can walk — the same recorded-not-silent
    mechanism REQUIRED_CLAUDE_ROLES uses, extended to a capability the harness
    does not have rather than a policy the desk sets.

    With no configured backend (or a Claude one), this is today's routing
    unchanged: the model still comes from the tier, because ``inherit`` already
    follows the operator's own session.
    """
    backend = workforce.backend if workforce is not None else CLAUDE_BACKEND
    if (backend != CLAUDE_BACKEND and not pinned_reason
            and role not in REQUIRED_CLAUDE_ROLES):
        unknown = _unknown_role_reason(role)
        return RouteDecision(role=role, requested_tier=tier_for(role),
                             resolved_model=workforce.model, backend=backend,
                             source="workforce_config",
                             fallback_reason=unknown,
                             notes=(NOTE_UNREGISTERED,) if unknown else ())
    decision = _claude_route(role, source_model=source_model,
                             tier_model=tier_model, fast=fast)
    if backend == CLAUDE_BACKEND:
        return decision
    # The role's own pin outranks the caller's: "the approval gate does not
    # move" is true on every desk, while a graph-shaped reason is true of one
    # dispatch.
    return _noted(decision, NOTE_PINNED,
                  pinned_to_claude_reason(role, backend)
                  if role in REQUIRED_CLAUDE_ROLES else pinned_reason)


def _claude_route(role: str, *, source_model: str | None,
                  tier_model: dict[str, str] | None,
                  fast: bool) -> RouteDecision:
    """Tier resolution on the Claude backend — the desk's default provider.

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
        # and shows which role refused it. NOT a fallback: the role kept its
        # tier, which is the opposite of being served below it.
        return RouteDecision(
            role=role, requested_tier=tier,
            resolved_model=TIER_MODEL[tier], source="tier",
            fallback_reason="fast mode does not apply to a required-deep role",
            notes=(NOTE_PINNED,))
    if role not in ROLE_TIER:
        return RouteDecision(role=role, requested_tier=NONE,
                             resolved_model=models.get(NONE, "inherit"),
                             source="unknown_role",
                             fallback_reason=_unknown_role_reason(role),
                             notes=(NOTE_UNREGISTERED,))
    decision = RouteDecision(role=role, requested_tier=tier,
                             resolved_model=models[tier], source="tier")
    if tier == DEEP and models[tier] != TIER_MODEL[DEEP]:
        # The module's one genuinely cheapened route, and it was the silent
        # one: a judgment role served by the quick model published
        # `model.route_resolved` with no note while the exempt referee beside
        # it published `model.fallback_used`. Keyed on the resolved model
        # rather than on the `fast` flag so a custom `tier_model` that swaps
        # the deep tier for a cheaper model is read the same way.
        return _noted(decision, NOTE_CHEAPENED,
                      f"the deep tier was served by {models[tier]!r} rather "
                      f"than {TIER_MODEL[DEEP]!r}")
    return decision


def record_invocation(registry, decision: RouteDecision, *,
                      status: str = "ok", backend: str | None = None,
                      latency_ms: float | None = None,
                      tokens: int | None = None,
                      invocation_id: str | None = None) -> str:
    """Persist one model invocation and emit its route event.

    The registry is the single writer; this only assembles the record.

    ``backend`` names the process that served the role, not the model. It
    defaults to the one the route resolved, so a row cannot claim a provider
    the decision never chose; a caller with a different process to name (a
    second coordinator, ``bob_shell``) passes its own value.
    """
    backend = backend or BACKEND_PROCESS.get(decision.backend, decision.backend)
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
    registry.record_event(route_event_kind(decision), decision.to_dict())
    return invocation_id


def degrades_result(role: str, decision: RouteDecision) -> bool:
    """True when ``role`` required its deep tier but did not get it.

    A required deep role served below its tier cannot be reported as a clean
    PASS — the caller must mark the phase degraded.

    Reading *any* note as degradation inverted the meaning of the two that
    exist to protect the gate: a referee pinned to claude, or exempted from
    fast mode, got exactly the tier it required, and calling that degraded
    would make the safeguard indistinguishable from the damage.
    """
    if role not in REQUIRED_DEEP_ROLES:
        return False
    return decision.requested_tier != DEEP or NOTE_CHEAPENED in decision.notes
