"""Optional LLM-phrased lessons written over an immutable outcome.

The deterministic outcome (:mod:`qlab.governance.reflection`) is the fact. A
lesson is *interpretation on top of it* — better prose for a human, never a new
number. Three rules make that safe:

* **Bound to an outcome hash.** A lesson references the exact outcome it was
  written against. Correct the outcome and its hash changes, so the lesson is
  stale by construction rather than quietly wrong.
* **Numerically grounded.** Every numeric token in the lesson must appear in
  the outcome (to a display tolerance). An unsupported figure is rejected — the
  model may phrase the evidence, not invent it.
* **Never authoritative.** A lesson carries no verdict and no weights. Callers
  and surfaces label it as interpretation.

Generation itself is model work and lives behind the caller; this module owns
validation, binding, and staleness so those cannot be skipped.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

# Numbers as a human writes them: 12, 1.5, -0.42, 12.5%, +3bps.
_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")

# Fields a lesson may quote. Anything else in the outcome is bookkeeping.
_QUOTABLE = (
    "realized_vol", "est_vol", "vol_ratio", "horizon_days",
    "realized_portfolio_return", "realized_6040_return",
    "realized_alpha_vs_6040", "regime_threshold",
)


class UngroundedLesson(ValueError):
    """A lesson cited a number the outcome does not support."""


@dataclass(frozen=True)
class Lesson:
    lesson_id: str
    decision_id: str
    outcome_hash: str
    summary: str
    what_worked: str
    what_failed: str
    next_time: str
    uncertainty: str
    prompt_version: str
    model_record_id: str | None = None
    evidence_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "lesson_id": self.lesson_id, "decision_id": self.decision_id,
            "outcome_hash": self.outcome_hash, "summary": self.summary,
            "what_worked": self.what_worked, "what_failed": self.what_failed,
            "next_time": self.next_time, "uncertainty": self.uncertainty,
            "prompt_version": self.prompt_version,
            "model_record_id": self.model_record_id,
            "evidence_refs": list(self.evidence_refs),
            # A lesson is interpretation, never an authoritative verdict.
            "advisory": True,
        }


def _supported_values(outcome: dict) -> set[str]:
    """Numeric tokens the outcome supports, in the forms a writer would use."""
    supported: set[str] = set()
    for key in _QUOTABLE:
        raw = outcome.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        for candidate in (value, value * 100.0, value * 1e4):
            for places in (0, 1, 2, 3, 4):
                supported.add(f"{abs(candidate):.{places}f}")
                supported.add(f"{abs(candidate):.{places}f}".rstrip("0").rstrip("."))
    return supported


def validate_grounding(text: str, outcome: dict) -> list[str]:
    """Return numeric tokens in ``text`` the outcome does not support."""
    supported = _supported_values(outcome)
    ungrounded = []
    for token in _NUMBER.findall(text or ""):
        bare = token.lstrip("+-")
        normalized = bare.rstrip("0").rstrip(".") if "." in bare else bare
        if bare not in supported and normalized not in supported:
            ungrounded.append(token)
    return ungrounded


def build_lesson(
    decision_id: str,
    outcome: dict,
    draft: dict,
    *,
    prompt_version: str = "lesson_v1",
    model_record_id: str | None = None,
    lesson_id: str | None = None,
) -> Lesson:
    """Validate a model-written draft and bind it to the outcome.

    Raises :class:`UngroundedLesson` if any field cites an unsupported number,
    or ``ValueError`` if the outcome carries no hash to bind to.
    """
    o_hash = outcome.get("outcome_hash")
    if not o_hash:
        raise ValueError(
            "outcome has no outcome_hash; a lesson cannot bind to it")
    fields = {
        "summary": str(draft.get("summary", "")).strip(),
        "what_worked": str(draft.get("what_worked", "")).strip(),
        "what_failed": str(draft.get("what_failed", "")).strip(),
        "next_time": str(draft.get("next_time", "")).strip(),
        "uncertainty": str(draft.get("uncertainty", "")).strip(),
    }
    if not fields["summary"]:
        raise ValueError("lesson summary must not be empty")
    offending: list[str] = []
    for name, text in fields.items():
        offending.extend(f"{name}:{tok}" for tok in validate_grounding(text, outcome))
    if offending:
        raise UngroundedLesson(
            "lesson cites numbers the outcome does not support: "
            + ", ".join(offending))
    return Lesson(
        lesson_id=lesson_id or uuid.uuid4().hex[:16],
        decision_id=decision_id,
        outcome_hash=str(o_hash),
        prompt_version=prompt_version,
        model_record_id=model_record_id,
        evidence_refs=[f"decision:{decision_id}", f"outcome:{o_hash}"],
        **fields,
    )


def is_stale(lesson: dict, outcome: dict) -> bool:
    """True when the outcome has changed since the lesson was written."""
    return str(lesson.get("outcome_hash") or "") != str(
        outcome.get("outcome_hash") or "")
