"""Deterministic regime-misclassification controls.

This module contains no model dependency.  It turns detector votes and an
optional HMM posterior into a guarded state with three controls: an immediate
uncertain state, confirmation delay, and a gradual post-switch risk ramp.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from typing import Literal

_BASE_REGIMES = ("calm", "normal", "stress")
_ALL_REGIMES = (*_BASE_REGIMES, "uncertain")
_RISK_RAMP = (0.5, 0.7, 0.9)


@dataclass(frozen=True)
class RobustRegime:
    regime: Literal["calm", "normal", "stress", "uncertain"]
    confidence: float
    effective_risk_fraction: float
    observation: Literal["calm", "normal", "stress", "uncertain"] | None = None


def _label(value: object, *, allow_uncertain: bool = False) -> str:
    label = str(value).strip().lower()
    permitted = _ALL_REGIMES if allow_uncertain else _BASE_REGIMES
    if label not in permitted:
        expected = ", ".join(permitted)
        raise ValueError(f"unknown regime {value!r}; expected one of {expected}")
    return label


def detector_agreement(detector_votes: Sequence[str]) -> tuple[str, float]:
    """Return the deterministic plurality label and agreeing vote fraction.

    Ties resolve toward the more conservative (higher-volatility) state.
    """
    votes = [_label(vote) for vote in detector_votes]
    if not votes:
        raise ValueError("at least one detector vote is required")
    counts = Counter(votes)
    winner = max(
        _BASE_REGIMES,
        key=lambda regime: (counts[regime], _BASE_REGIMES.index(regime)),
    )
    return winner, counts[winner] / len(votes)


def _posterior(
    values: Mapping[str, float],
) -> tuple[str, float]:
    if not values:
        raise ValueError("HMM posterior must not be empty")
    unknown = set(values) - set(_BASE_REGIMES)
    if unknown:
        raise ValueError(
            f"HMM posterior contains unknown states: {sorted(unknown)}"
        )
    posterior = {
        regime: float(values.get(regime, 0.0))
        for regime in _BASE_REGIMES
    }
    if any(not isfinite(value) or value < 0.0 for value in posterior.values()):
        raise ValueError("HMM posterior probabilities must be finite and non-negative")
    total = sum(posterior.values())
    if abs(total - 1.0) > 1e-6:
        raise ValueError("HMM posterior probabilities must sum to 1")
    winner = max(
        _BASE_REGIMES,
        key=lambda regime: (posterior[regime], _BASE_REGIMES.index(regime)),
    )
    return winner, posterior[winner]


def _confirmed_state(
    observations: Sequence[str],
    confirmation_days: int,
) -> tuple[str, int | None]:
    effective = observations[0]
    pending: str | None = None
    pending_count = 0
    last_switch: int | None = None

    for index, observation in enumerate(observations[1:], start=1):
        if observation == "uncertain":
            if effective != "uncertain":
                effective = "uncertain"
                last_switch = index
            pending = None
            pending_count = 0
            continue
        if observation == effective:
            pending = None
            pending_count = 0
            continue
        if observation == pending:
            pending_count += 1
        else:
            pending = observation
            pending_count = 1
        if pending_count >= confirmation_days:
            effective = observation
            last_switch = index
            pending = None
            pending_count = 0
    return effective, last_switch


def robust_regime(
    detector_votes: Sequence[str],
    *,
    hmm_posterior: Mapping[str, float] | None = None,
    history: Sequence[str] = (),
    confirmation_days: int = 3,
    posterior_threshold: float = 0.5,
    agreement_threshold: float = 0.70,
) -> RobustRegime:
    """Guard a daily regime observation against likely misclassification.

    ``history`` contains prior raw daily observations, oldest first.  Persist
    the returned ``observation`` and thread it into the next call; ``regime``
    is the delayed effective state and must not be used as raw history.  The
    current observation is derived from the HMM's highest-probability state
    when supplied, otherwise from the deterministic detector plurality.
    Uncertainty takes effect immediately; a confident state change takes
    ``confirmation_days`` consecutive observations.
    """
    if (
        isinstance(confirmation_days, bool)
        or not isinstance(confirmation_days, int)
        or confirmation_days < 1
    ):
        raise ValueError("confirmation_days must be a positive integer")
    if not 0.0 <= posterior_threshold <= 1.0:
        raise ValueError("posterior_threshold must be in [0, 1]")
    if not 0.0 <= agreement_threshold <= 1.0:
        raise ValueError("agreement_threshold must be in [0, 1]")

    vote_regime, agreement = detector_agreement(detector_votes)
    candidate = vote_regime
    posterior_confidence = 1.0
    if hmm_posterior is not None:
        candidate, posterior_confidence = _posterior(hmm_posterior)

    confidence = min(agreement, posterior_confidence)
    uncertain = (
        posterior_confidence < posterior_threshold
        or agreement < agreement_threshold
    )
    current = "uncertain" if uncertain else candidate
    if current == "uncertain":
        return RobustRegime("uncertain", confidence, 0.5, current)

    prior = [_label(regime, allow_uncertain=True) for regime in history]
    observations = [*prior, current]
    effective, switch_index = _confirmed_state(
        observations,
        confirmation_days,
    )
    if effective == "uncertain":
        risk_fraction = 0.5
    elif switch_index is None:
        risk_fraction = 1.0
    else:
        days_after_switch = len(observations) - 1 - switch_index
        risk_fraction = _RISK_RAMP[min(days_after_switch, len(_RISK_RAMP) - 1)]
    return RobustRegime(effective, confidence, risk_fraction, current)
