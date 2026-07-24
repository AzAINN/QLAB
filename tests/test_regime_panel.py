"""The regime panel: same-snapshot binding, visible failures, honest uncertainty."""

from __future__ import annotations

import pytest

from qlab.core import data as market
from qlab.signals.panel import (
    CALM,
    FINGERPRINT_VERSION,
    STRESS,
    UNCERTAIN,
    assert_same_snapshot,
    build_panel,
)


@pytest.fixture
def snap():
    return market.snapshot(
        ["ACWI", "BNDW", "GSG", "IGF", "GLD", "VNQ", "EMB"],
        "2022-06-30", offline=True, seed=7)


def test_panel_binds_every_reading_to_one_snapshot(snap):
    panel = build_panel(snap)
    assert panel.snapshot_id == snap.content_hash()
    assert panel.as_of == str(snap.as_of)
    assert len(panel.readings) == 5
    assert {r.indicator_id for r in panel.readings} == {
        "turbulence", "absorption", "volatility_term_structure",
        "drawdown", "tail_risk"}


def test_panel_is_deterministic_for_the_same_snapshot(snap):
    a = build_panel(snap).to_dict()
    b = build_panel(snap).to_dict()
    assert a == b
    assert a["fingerprint"]["digest"] == b["fingerprint"]["digest"]


def test_robust_state_is_one_of_the_three_labels(snap):
    panel = build_panel(snap)
    assert panel.robust_state in (CALM, STRESS, UNCERTAIN)
    assert panel.agreement_count + panel.disagreement_count == sum(
        1 for r in panel.readings if r.state in (CALM, STRESS))


def test_a_failed_indicator_is_visible_and_never_counts_as_agreement(snap):
    def boom(_snapshot):
        raise ValueError("insufficient history")

    indicators = {"turbulence": boom,
                  "absorption": lambda s: {"regime": CALM, "signal": 0.1,
                                           "threshold": 0.5, "percentile": 0.2,
                                           "window": 252, "reasoning": "calm"}}
    panel = build_panel(snap, indicators=indicators)
    failed = [r for r in panel.readings if r.state == "failed"]
    assert len(failed) == 1
    assert "insufficient history" in failed[0].reasoning
    assert panel.failed_count == 1
    # One usable reading is below the floor -> uncertain, not a coin flip.
    assert panel.robust_state == UNCERTAIN
    assert "too few" in panel.uncertainty_reason


def test_widespread_disagreement_yields_uncertain(snap):
    def reading(regime):
        return lambda s: {"regime": regime, "signal": 1.0, "threshold": 1.0,
                          "percentile": 0.5, "window": 21, "reasoning": regime}

    # 2 stress vs 2 calm -> 50% agreement, below the 60% floor.
    indicators = {"a": reading(STRESS), "b": reading(STRESS),
                  "c": reading(CALM), "d": reading(CALM)}
    panel = build_panel(snap, indicators=indicators)
    assert panel.robust_state == UNCERTAIN
    assert "disagree" in panel.uncertainty_reason


def test_clear_majority_resolves_to_that_state(snap):
    def reading(regime):
        return lambda s: {"regime": regime, "signal": 1.0, "threshold": 1.0,
                          "percentile": 0.9, "window": 21, "reasoning": regime}

    indicators = {"a": reading(STRESS), "b": reading(STRESS),
                  "c": reading(STRESS), "d": reading(CALM)}
    panel = build_panel(snap, indicators=indicators)
    assert panel.robust_state == STRESS
    assert panel.agreement_count == 3 and panel.disagreement_count == 1
    assert panel.uncertainty_reason is None


def test_fingerprint_is_versioned_and_recall_compatible(snap):
    fp = build_panel(snap).fingerprint
    assert fp["fingerprint_version"] == FINGERPRINT_VERSION
    assert fp["snapshot_id"] == snap.content_hash()
    # The two fields the similarity scorer reads are present and normalized.
    for key in ("vol_percentile", "turbulence_percentile"):
        value = fp[key]
        assert value is None or 0.0 <= value <= 1.0
    assert fp["regime_label"] == build_panel(snap).robust_state


def test_mixed_snapshots_are_refused():
    assert_same_snapshot(["abc", "abc"])          # one snapshot is fine
    with pytest.raises(ValueError, match="mixes 2 snapshots"):
        assert_same_snapshot(["abc", "def"])
