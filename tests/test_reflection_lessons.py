"""Grounded lessons bound to an immutable outcome hash (P5 gap 5)."""

from __future__ import annotations

import pytest

from qlab.governance.lessons import (
    UngroundedLesson,
    build_lesson,
    is_stale,
    validate_grounding,
)
from qlab.governance.reflection import outcome_hash
from qlab.state.registry import Registry


@pytest.fixture
def reg():
    r = Registry(":memory:")
    yield r
    r.close()


def _outcome(**over):
    out = {
        "schema_version": 2,
        "realized_vol": 0.184,
        "est_vol": 0.150,
        "vol_ratio": 1.23,
        "horizon_days": 63,
        "realized_alpha_vs_6040": -0.021,
        "regime_call": "calm",
        "regime_realized": "stress",
        "regime_consistent": False,
    }
    out.update(over)
    out["outcome_hash"] = outcome_hash(out)
    return out


def _draft(**over):
    d = {
        "summary": "The volatility estimate ran low against the realized path.",
        "what_worked": "The universe stayed inside its mandate.",
        "what_failed": "The regime call of calm was contradicted.",
        "next_time": "Widen the estimation window when term structure steepens.",
        "uncertainty": "One horizon is a small sample; do not over-generalize.",
    }
    d.update(over)
    return d


def test_outcome_hash_is_stable_and_excludes_itself():
    o = _outcome()
    assert outcome_hash(o) == o["outcome_hash"]
    # A changed number changes the identity.
    assert outcome_hash(_outcome(realized_vol=0.99)) != o["outcome_hash"]


def test_lesson_binds_to_the_outcome_hash():
    o = _outcome()
    lesson = build_lesson("dec-1", o, _draft())
    assert lesson.outcome_hash == o["outcome_hash"]
    assert lesson.to_dict()["advisory"] is True
    assert f"outcome:{o['outcome_hash']}" in lesson.evidence_refs


def test_unsupported_number_is_rejected():
    o = _outcome()
    with pytest.raises(UngroundedLesson, match="does not support"):
        build_lesson("dec-1", o,
                     _draft(summary="Realized vol was 47.3% over the horizon."))


def test_supported_numbers_in_display_forms_are_accepted():
    o = _outcome()
    # 0.184 -> "18.4%" and horizon_days -> "63" are both grounded.
    lesson = build_lesson("dec-1", o, _draft(
        summary="Realized vol was 18.4% over 63 days."))
    assert lesson.summary.startswith("Realized vol")
    assert validate_grounding("18.4% over 63 days", o) == []


def test_lesson_cannot_bind_to_an_outcome_without_a_hash():
    with pytest.raises(ValueError, match="no outcome_hash"):
        build_lesson("dec-1", {"realized_vol": 0.1}, _draft())


def test_lesson_roundtrips_and_goes_stale_when_the_outcome_changes(reg):
    o = _outcome()
    lesson = build_lesson("dec-1", o, _draft())
    reg.record_lesson(lesson.to_dict())
    stored = reg.get_lesson("dec-1")
    assert stored["outcome_hash"] == o["outcome_hash"]
    assert stored["stale"] is False
    assert stored["lesson"]["advisory"] is True

    corrected = _outcome(realized_vol=0.205)
    assert is_stale(stored, corrected)
    assert reg.mark_lessons_stale("dec-1", corrected["outcome_hash"]) == 1
    assert reg.get_lesson("dec-1")["stale"] is True
    kinds = [e["kind"] for e in reg.read_events(20)]
    assert "reflection.lesson_generated" in kinds
    assert "reflection.lesson_stale" in kinds


def test_lesson_generation_never_mutates_the_outcome():
    o = _outcome()
    before = dict(o)
    build_lesson("dec-1", o, _draft())
    assert o == before
