"""The regime panel: all indicators read off ONE snapshot, with a fingerprint.

Individual indicators (:mod:`qlab.signals.indicators`) each answer one question.
The *panel* is the auditable object built from all of them at a single point in
time: it binds every reading to one ``snapshot_id``/``as_of``, records agreement
and disagreement, and produces a versioned fingerprint used for similar-regime
recall.

Three properties the individual indicators cannot provide on their own:

* **Same-snapshot binding.** Readings computed from different snapshots must
  never be mixed into one panel — that would compare different market states.
* **Visible failure.** An indicator that raises (too little history, bad data)
  appears as a ``failed`` reading with its reason. It never silently disappears
  and never counts as agreement.
* **Honest uncertainty.** Widespread disagreement, or too few usable readings,
  yields ``uncertain`` rather than a coin-flip regime label.

The panel is a *diagnostic*, not a trading signal: it describes market state, it
does not forecast returns or recommend weights.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from qlab.core.types import DataSnapshot
from qlab.signals.indicators import INDICATORS

# Bump when the fingerprint's features or their semantics change, so recall can
# refuse to compare fingerprints built under incompatible definitions.
FINGERPRINT_VERSION = 2

PANEL_VERSION = 1

CALM = "calm"
STRESS = "stress"
UNCERTAIN = "uncertain"

# Below this share of agreement among usable readings the panel refuses to call
# a side; and below this many usable readings there is not enough to judge.
_AGREEMENT_FLOOR = 0.6
_MIN_USABLE = 3


@dataclass(frozen=True)
class PanelReading:
    indicator_id: str
    version: int
    state: str                    # calm | stress | failed
    signal: float | None
    threshold: float | None
    percentile: float | None
    window: int | None
    reasoning: str
    quality_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "indicator_id": self.indicator_id, "version": self.version,
            "state": self.state, "signal": self.signal,
            "threshold": self.threshold, "percentile": self.percentile,
            "window": self.window, "reasoning": self.reasoning,
            "quality_flags": list(self.quality_flags),
        }


@dataclass(frozen=True)
class RegimePanel:
    snapshot_id: str
    as_of: str
    universe: list[str]
    readings: list[PanelReading]
    robust_state: str
    agreement_count: int
    disagreement_count: int
    failed_count: int
    uncertainty_reason: str | None
    fingerprint: dict

    def to_dict(self) -> dict:
        return {
            "panel_version": PANEL_VERSION,
            "snapshot_id": self.snapshot_id,
            "as_of": self.as_of,
            "universe": list(self.universe),
            "readings": [r.to_dict() for r in self.readings],
            "robust_state": self.robust_state,
            "agreement_count": self.agreement_count,
            "disagreement_count": self.disagreement_count,
            "failed_count": self.failed_count,
            "uncertainty_reason": self.uncertainty_reason,
            "fingerprint": dict(self.fingerprint),
        }


def build_panel(snapshot: DataSnapshot, *,
                indicators: dict | None = None) -> RegimePanel:
    """Run every indicator against ONE snapshot and summarize the result.

    Deterministic: the same snapshot and indicator set always produce the same
    panel, including its fingerprint.
    """
    indicators = indicators or INDICATORS
    snapshot_id = snapshot.content_hash()
    readings: list[PanelReading] = []

    for name in sorted(indicators):
        try:
            raw = indicators[name](snapshot)
        except Exception as exc:
            # Visible failure: a broken indicator is recorded, never dropped.
            readings.append(PanelReading(
                indicator_id=name, version=PANEL_VERSION, state="failed",
                signal=None, threshold=None, percentile=None, window=None,
                reasoning=f"indicator failed: {exc}",
                quality_flags=["failed"]))
            continue
        readings.append(PanelReading(
            indicator_id=name, version=PANEL_VERSION,
            state=str(raw.get("regime")), signal=raw.get("signal"),
            threshold=raw.get("threshold"), percentile=raw.get("percentile"),
            window=raw.get("window"), reasoning=str(raw.get("reasoning", "")),
        ))

    usable = [r for r in readings if r.state in (CALM, STRESS)]
    failed = [r for r in readings if r.state == "failed"]
    stress = sum(1 for r in usable if r.state == STRESS)
    calm = sum(1 for r in usable if r.state == CALM)

    robust_state, reason = _resolve_state(usable, stress, calm, len(failed))
    majority = max(stress, calm)
    return RegimePanel(
        snapshot_id=snapshot_id,
        as_of=str(snapshot.as_of),
        universe=list(snapshot.tickers),
        readings=readings,
        robust_state=robust_state,
        agreement_count=majority,
        disagreement_count=len(usable) - majority,
        failed_count=len(failed),
        uncertainty_reason=reason,
        fingerprint=_fingerprint(snapshot_id, snapshot, readings, robust_state),
    )


def _resolve_state(usable, stress: int, calm: int,
                   failed: int) -> tuple[str, str | None]:
    if len(usable) < _MIN_USABLE:
        return UNCERTAIN, (
            f"only {len(usable)} usable indicator reading(s); {failed} failed — "
            "too few to call a regime")
    majority = max(stress, calm)
    agreement = majority / len(usable)
    if agreement < _AGREEMENT_FLOOR:
        return UNCERTAIN, (
            f"indicators disagree ({stress} stress vs {calm} calm); agreement "
            f"{agreement:.0%} is below the {_AGREEMENT_FLOOR:.0%} floor")
    return (STRESS if stress > calm else CALM), None


def _fingerprint(snapshot_id: str, snapshot: DataSnapshot, readings,
                 robust_state: str) -> dict:
    """A versioned, comparable summary of this regime for similarity recall.

    Percentiles are already normalized to ``[0, 1]``, so they compare directly
    across indicators and dates. A failed indicator contributes ``None`` rather
    than a fabricated value, and recall must treat it as non-comparable.
    """
    features = {
        r.indicator_id: (float(r.percentile) if r.percentile is not None else None)
        for r in readings
    }
    material = {
        "version": FINGERPRINT_VERSION,
        "features": features,
        "regime_label": robust_state,
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":"),
                   default=str).encode()).hexdigest()[:16]
    return {
        "fingerprint_version": FINGERPRINT_VERSION,
        "snapshot_id": snapshot_id,
        "as_of": str(snapshot.as_of),
        "regime_label": robust_state,
        "features": features,
        # Back-compat aliases: the recall scorer reads these two percentile
        # fields directly, so a panel fingerprint is usable by it unchanged.
        "vol_percentile": features.get("volatility_term_structure"),
        "turbulence_percentile": features.get("turbulence"),
        "digest": digest,
    }


def assert_same_snapshot(readings_snapshot_ids: list[str]) -> None:
    """Refuse a panel assembled from more than one snapshot.

    Used when readings are collected separately (e.g. across tool calls) and
    then combined: mixing snapshots compares different market states.
    """
    unique = {s for s in readings_snapshot_ids if s}
    if len(unique) > 1:
        raise ValueError(
            f"regime panel mixes {len(unique)} snapshots ({sorted(unique)}); "
            "every reading must come from one point-in-time snapshot")
