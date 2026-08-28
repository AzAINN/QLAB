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
    assert tail[0]["confidence"] == 0.45  # 0.15 per excess document, under the cap
    assert tail[0]["source_claims"] == ["a", "b", "c", "d"]


def test_no_rule_emits_a_return_view_and_quiet_records_emit_nothing():
    quiet = matrix([row("GLD", 1, 1, 0, 0, keys=("k",))])
    assert views_from_matrix(quiet, None, sleeves={}) == []
    loud = matrix([row(t, 5, 4, 3, 3, keys=("k",)) for t in ["A", "B", "C", "D", "E"]])
    views = views_from_matrix(loud, None, sleeves={})
    assert len(views) == 3
    assert {v["type"] for v in views} <= {"tail", "corr"}
    # Equal confidences keep the matrix's own row order, so the tie-break is fixed.
    assert [v["ticker"] for v in views] == ["A", "B", "C"]


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


def test_a_stronger_correlation_view_outranks_a_weaker_tail_view():
    """Both rules are ranked on one scale — confidence — or corr never lands."""
    now = matrix([
        row("A", 0, 0, 2, 2, keys=("a",)),
        row("B", 0, 0, 2, 2, keys=("b",)),
        row("C", 0, 0, 2, 2, keys=("c",)),
        row("X", 8, 3, 0, 0, keys=("x",)),
        row("Y", 1, 1, 0, 0, keys=("y",)),
    ])
    views = views_from_matrix(now, None, sleeves={"s": ["X", "Y"]})
    assert len(views) == 3
    corr = [v for v in views if v["type"] == "corr"]
    assert len(corr) == 1 and corr[0]["confidence"] == 0.389
    assert [v["confidence"] for v in views] == [0.389, 0.3, 0.3]
    # Three equal tails at 0.3; the weakest by stable order is the one dropped.
    assert [v["ticker"] for v in views if v["type"] == "tail"] == ["A", "B"]


def test_one_sleeve_spends_at_most_one_view_on_its_widest_pair():
    now = matrix([
        row("X", 9, 3, 0, 0, keys=("x",)),
        row("Y", 1, 1, 0, 0, keys=("y",)),
        row("Z", 2, 1, 0, 0, keys=("z",)),
        row("W", 0, 0, 0, 0, keys=("w",)),
    ])
    views = views_from_matrix(now, None, sleeves={"s": ["X", "Y", "Z", "W"]})
    assert len(views) == 1
    assert views[0]["type"] == "corr" and views[0]["ticker_a"] == "X"
    assert views[0]["ticker_b"] == "W"  # the widest coverage gap in the sleeve


# ---------------------------------------------------------------------------
# The production caller: the ablation's A5 arm.
# ---------------------------------------------------------------------------
CORE7 = ["ACWI", "BNDW", "GSG", "IGF", "GLD", "VNQ", "EMB"]


@pytest.fixture
def snapshot():
    from qlab.core import data as market

    return market.snapshot(CORE7, "2015-09-30", offline=True, seed=7)


@pytest.fixture
def moment_set_7(snapshot):
    from qlab.core.moments import estimate_moments

    return estimate_moments(snapshot, lookback_days=504, higher_moments=False)


def _conditioner(reg, **kw):
    from qlab.research.views_arm import MatrixViewsConditioner

    return MatrixViewsConditioner(reg, panel_lookback_days=504, **kw)


def test_sleeves_come_from_the_universes_own_asset_classes():
    from qlab.research.views_arm import sleeves_for

    sleeves = sleeves_for(CORE7)
    # Non-overlapping by construction: a ticker appears in exactly one sleeve,
    # or the correlation rule would state one concentration under two names.
    seen = [t for members in sleeves.values() for t in members]
    assert sorted(seen) == sorted(CORE7)
    assert sleeves_for(["NOT_IN_UNIVERSE"]) == {"unclassified": ["NOT_IN_UNIVERSE"]}


def test_a_silent_record_leaves_the_covariance_exactly_as_it_was(
    reg, snapshot, moment_set_7
):
    """No view is a real answer; the arm must then reproduce its baseline."""
    cond = _conditioner(reg)
    out = cond.condition(moment_set_7, snapshot)
    assert out is moment_set_7
    assert cond.stats["windows"] == 1
    assert cond.stats["windows_with_views"] == 0
    assert cond.stats["windows_conditioned"] == 0


def test_a_speaking_record_tilts_the_covariance_and_logs_a_checkable_views_run(
    reg, snapshot, moment_set_7, monkeypatch
):
    from qlab.news.matrix import QualitativeMatrix
    from qlab.research import views_arm

    loud = QualitativeMatrix("2015-09-30", "w1", {
        "ACWI": row("ACWI", 6, 3, 4, 4, keys=("a", "b", "c", "d")),
    })
    monkeypatch.setattr(views_arm, "build_matrix",
                        lambda *a, **k: loud)

    cond = _conditioner(reg)
    out = cond.condition(moment_set_7, snapshot)

    assert cond.stats["windows_with_views"] == 1
    assert cond.stats["windows_conditioned"] == 1
    import numpy as np

    assert not np.allclose(out.cov, moment_set_7.cov)
    # The arm estimates covariance-only (mu is None by policy), so the pinning
    # evidence is the drift that was measured and discarded, not a copied mu.
    assert out.mu is moment_set_7.mu
    assert out.provenance["mean_pinning_max_abs"] > 0.0

    run = reg.get_run(out.provenance["views_run_id"])
    assert run["kind"] == "views"
    spec = run["spec"]
    assert spec["kl_total"] <= spec["kl_budget"]
    assert spec["provenance_verified"] is True
    assert spec["views"][0]["source_claims"] == ["a", "b", "c", "d"]
    # And the matrix it was counted from is on the record beside it.
    assert reg.newest_run_of_kind("qualitative_matrix") is not None


def test_the_window_is_read_once_however_many_arms_walk_the_same_date(
    reg, snapshot, moment_set_7
):
    cond = _conditioner(reg)
    cond.condition(moment_set_7, snapshot)
    cond.condition(moment_set_7, snapshot)
    assert cond.stats["windows"] == 1
    assert len(reg.runs_of_kind("qualitative_matrix", 10)) == 1


def test_the_previous_window_is_the_baseline_not_the_current_one(reg):
    """A rule that compares a window to itself finds every document 'new'."""
    from qlab.news.matrix import QualitativeMatrix

    cond = _conditioner(reg)
    first = QualitativeMatrix("2015-06-30", "w1",
                              {"ACWI": row("ACWI", 6, 3, 4, 4, keys=("a",))})
    assert cond._log_and_previous(first) is None
    second = QualitativeMatrix("2015-09-30", "w2",
                               {"ACWI": row("ACWI", 8, 3, 6, 6, keys=("b",))})
    baseline = cond._log_and_previous(second)
    assert baseline["ACWI"].primary_docs == 4


def test_an_arm_asking_for_views_with_no_conditioner_refuses_loudly(snapshot):
    from qlab.arms import Arm, solve_arm

    arm = Arm("A5", "min_variance", "classical",
              {"views_source": "qualitative_matrix"})
    with pytest.raises(ValueError, match="needs a conditioner"):
        solve_arm(arm, snapshot)


def test_views_conditioning_refuses_the_higher_moment_objectives(snapshot, reg):
    from qlab.arms import Arm, MomentsConfig, solve_arm

    arm = Arm("A5m", "mvsk", "classical_multistart",
              {"views_source": "qualitative_matrix"})
    cfg = MomentsConfig(lookback_days=504, views_conditioner=_conditioner(reg))
    with pytest.raises(ValueError, match="covariance-only"):
        solve_arm(arm, snapshot, moments=cfg)


def test_an_unknown_views_source_is_refused_rather_than_ignored(snapshot, reg):
    from qlab.arms import Arm, MomentsConfig, solve_arm

    arm = Arm("A5x", "min_variance", "classical", {"views_source": "vibes"})
    cfg = MomentsConfig(lookback_days=504, views_conditioner=_conditioner(reg))
    with pytest.raises(ValueError, match="unknown views_source"):
        solve_arm(arm, snapshot, moments=cfg)


def test_the_ablation_spec_carries_the_arm_and_the_runner_honours_it():
    """Invariant 10: the arm in the yaml must be the arm the runner builds."""
    import inspect

    import yaml

    from qlab import experiment
    from qlab.paths import workspace_root

    spec = yaml.safe_load(
        (workspace_root() / "configs/specs/ablation_v1.yaml").read_text())
    a5 = next(a for a in spec["arms"] if a["id"] == "A5")
    assert a5["params"]["views_source"] == "qualitative_matrix"
    assert "views_source" in inspect.getsource(experiment.run_ablation)
