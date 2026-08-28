"""The qualitative matrix turned into bounded, unsigned risk views by rule."""

from __future__ import annotations

import pytest

from qlab.news.matrix import MatrixRow, QualitativeMatrix
from qlab.research.matrix_views import views_from_matrix


def row(t, coverage=0, publishers=0, corroborated=0, primary=0, keys=()):
    return MatrixRow(t, coverage, publishers, corroborated, primary, None, tuple(keys))


def matrix(rows):
    return QualitativeMatrix("2026-08-28", "w", {r.ticker: r for r in rows})


def test_excess_corroborated_primary_documents_fatten_the_tail_with_capped_confidence():
    now = matrix([row("TLT", 6, 3, 4, 4, keys=("a", "b", "c", "d"))])
    base = {"TLT": row("TLT", 2, 2, 1, 1)}
    views = views_from_matrix(now, base, sleeves={"rates": ["TLT", "IEF"]})
    tail = [v for v in views if v["type"] == "tail"]
    assert len(tail) == 1 and tail[0]["ticker"] == "TLT" and tail[0]["direction"] == "fatter"
    assert 0 < tail[0]["confidence"] <= 0.5
    assert tail[0]["source_claims"] == ["a", "b", "c", "d"]


def test_no_rule_emits_a_return_view_and_quiet_records_emit_nothing():
    quiet = matrix([row("GLD", 1, 1, 0, 0, keys=("k",))])
    assert views_from_matrix(quiet, None, sleeves={}) == []
    loud = matrix([row(t, 5, 4, 3, 3, keys=("k",)) for t in ["A", "B", "C", "D", "E"]])
    views = views_from_matrix(loud, None, sleeves={})
    assert len(views) <= 3
    assert {v["type"] for v in views} <= {"tail", "corr", "vol"}


def test_concentrated_sleeve_coverage_couples_the_loud_name_to_its_neighbours():
    now = matrix([
        row("SPY", 8, 4, 0, 0, keys=("s1", "s2")),
        row("QQQ", 1, 1, 0, 0, keys=("q1",)),
    ])
    views = views_from_matrix(now, None, sleeves={"equity": ["SPY", "QQQ"]})
    assert [v["type"] for v in views] == ["corr"]
    corr = views[0]
    assert corr["ticker_a"] == "SPY" and corr["ticker_b"] == "QQQ"
    assert corr["target_corr"] == 0.6
    assert 0 < corr["confidence"] <= 0.4
    assert corr["source_claims"] == ["s1", "s2"]
    # Unsigned by construction: no rule may emit a return or price field.
    assert not {"target_return", "expected_return", "price"} & set(corr)


def test_an_empty_matrix_refuses_rather_than_quietly_producing_nothing():
    with pytest.raises(ValueError, match="no rows"):
        views_from_matrix(matrix([]), None, sleeves={})
