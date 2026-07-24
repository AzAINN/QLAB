"""Bounded debate, enforced by the registry rather than by prompt text.

The analyst and challenger may argue about *how the estimate was formed* — the
estimation window, shrinkage, the regime read. They may not argue about target
weights: that is the optimizer's arithmetic under a reviewed objective, and a
debate over it would relitigate the numbers an algorithm owns.

Two limits that must hold in code, not in instructions a model can talk past:

* **Two rounds, never a third.** The round ceiling is checked when a turn is
  recorded, so an eager coordinator cannot extend the argument.
* **Claims are typed and allowlisted.** A turn about a forbidden subject is
  refused outright.

An open material disagreement must be adjudicated before the desk reports: the
reporter's dependency on adjudication is what makes the debate consequential
rather than decorative.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

MAX_ROUNDS = 2

OPEN = "open"
ADJUDICATED = "adjudicated"
WITHDRAWN = "withdrawn"

# Subjects a debate may address — all about how an estimate was formed.
ALLOWED_CLAIMS = frozenset({
    "estimation_window",
    "shrinkage",
    "regime_read",
    "data_quality",
    "estimator_choice",
})

# Explicitly forbidden: the optimizer's arithmetic and the execution decision.
FORBIDDEN_CLAIMS = frozenset({
    "target_weights",
    "targets",
    "order_size",
    "execution",
    "referee_verdict",
})

DEFEND = "defend"
AMEND = "amend"
REBUT = "rebut"
POSITIONS = (DEFEND, AMEND, REBUT)


class DebateViolation(ValueError):
    """A debate rule was broken; the turn is refused."""


@dataclass(frozen=True)
class DebateTurn:
    turn_id: str
    debate_id: str
    round: int
    role: str
    claim_id: str
    position: str
    argument: str
    evidence_refs: list[str]


def validate_claim(claim_id: str) -> str:
    """Return the normalized claim, refusing forbidden or unknown subjects."""
    claim = str(claim_id or "").strip().lower()
    if claim in FORBIDDEN_CLAIMS:
        raise DebateViolation(
            f"claim {claim!r} is not debatable: target weights and execution "
            "are the optimizer's arithmetic under a reviewed objective, not a "
            "matter of opinion")
    if claim not in ALLOWED_CLAIMS:
        raise DebateViolation(
            f"claim {claim!r} is not an allowlisted debate subject; expected "
            f"one of {sorted(ALLOWED_CLAIMS)}")
    return claim


def open_debate(registry, *, workflow_id: str, original_decision_id: str,
                material_claims: list[str], panel_snapshot_id: str | None = None,
                debate_id: str | None = None) -> str:
    """Open a debate over one or more allowlisted material claims."""
    if not material_claims:
        raise DebateViolation("a debate needs at least one material claim")
    claims = [validate_claim(c) for c in material_claims]
    did = debate_id or uuid.uuid4().hex[:16]
    registry.create_debate({
        "debate_id": did, "workflow_id": workflow_id,
        "original_decision_id": original_decision_id, "status": OPEN,
        "max_rounds": MAX_ROUNDS, "panel_snapshot_id": panel_snapshot_id,
        "material_claims": claims,
    })
    registry.record_event("debate.started",
                          {"debate_id": did, "workflow_id": workflow_id,
                           "claims": claims})
    return did


def record_turn(registry, debate_id: str, *, role: str, claim_id: str,
                position: str, argument: str,
                evidence_refs: list[str] | None = None,
                turn_id: str | None = None) -> DebateTurn:
    """Record one debate turn, enforcing the round ceiling and claim allowlist."""
    debate = registry.get_debate(debate_id)
    if debate is None:
        raise DebateViolation(f"unknown debate {debate_id!r}")
    if debate.get("status") != OPEN:
        raise DebateViolation(
            f"debate {debate_id!r} is {debate.get('status')!r}; no further turns")
    if position not in POSITIONS:
        raise DebateViolation(
            f"position must be one of {POSITIONS}, got {position!r}")
    claim = validate_claim(claim_id)
    if claim not in (debate.get("material_claims") or []):
        raise DebateViolation(
            f"claim {claim!r} was not opened with this debate")
    if not str(argument or "").strip():
        raise DebateViolation("a debate turn needs an argument")

    turns = registry.list_debate_turns(debate_id)
    # A round is one exchange: each role gets at most one turn per round on a
    # claim, and the ceiling is checked here so no prompt can talk past it.
    used = [t for t in turns if t.get("claim_id") == claim
            and t.get("role") == role]
    round_number = len(used) + 1
    if round_number > int(debate.get("max_rounds") or MAX_ROUNDS):
        raise DebateViolation(
            f"debate {debate_id!r} claim {claim!r} has used its "
            f"{debate.get('max_rounds')} rounds for role {role!r}; "
            "adjudicate instead of arguing further")

    turn = DebateTurn(
        turn_id=turn_id or uuid.uuid4().hex[:16], debate_id=debate_id,
        round=round_number, role=role, claim_id=claim, position=position,
        argument=argument.strip(), evidence_refs=list(evidence_refs or []))
    registry.add_debate_turn(vars(turn))
    registry.record_event("debate.turn_recorded",
                          {"debate_id": debate_id, "round": round_number,
                           "role": role, "claim_id": claim})
    return turn


def adjudicate(registry, debate_id: str, *, decided_by: str, resolution: str,
               winning_claim_positions: dict, evidence_refs: list[str] | None = None,
               amended_decision_id: str | None = None) -> dict:
    """Close a debate with a reasoned adjudication referencing exact ids."""
    debate = registry.get_debate(debate_id)
    if debate is None:
        raise DebateViolation(f"unknown debate {debate_id!r}")
    if debate.get("status") != OPEN:
        raise DebateViolation(f"debate {debate_id!r} is already closed")
    if not str(resolution or "").strip():
        raise DebateViolation("adjudication needs a resolution")
    claims = set(debate.get("material_claims") or [])
    undecided = claims - set(winning_claim_positions)
    if undecided:
        raise DebateViolation(
            f"adjudication leaves claims undecided: {sorted(undecided)}")
    adjudication = {
        "decided_by": decided_by,
        "resolution": resolution.strip(),
        "winning_claim_positions": dict(winning_claim_positions),
        "amended_decision_id": amended_decision_id,
        "original_decision_id": debate.get("original_decision_id"),
        "panel_snapshot_id": debate.get("panel_snapshot_id"),
        "evidence_refs": list(evidence_refs or []),
    }
    registry.close_debate(debate_id, ADJUDICATED, adjudication)
    registry.record_event("debate.adjudicated",
                          {"debate_id": debate_id, "decided_by": decided_by})
    registry.record_event("debate.closed", {"debate_id": debate_id})
    return adjudication


def open_debates_for(registry, workflow_id: str) -> list[dict]:
    """Debates on this workflow still awaiting adjudication."""
    return [d for d in registry.list_debates(workflow_id)
            if d.get("status") == OPEN]
