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
    assert out.provenance["mean_pinning_max_abs"] > 0.0

    run = reg.get_run(out.provenance["views_run_id"])
    assert run["kind"] == "views"
    spec = run["spec"]
    assert spec["kl_total"] <= spec["kl_budget"]
    assert spec["provenance_verified"] is True
    assert spec["views"][0]["source_claims"] == ["a", "b", "c", "d"]
    # And the matrix it was counted from is on the record beside it.
    assert reg.newest_run_of_kind("qualitative_matrix") is not None


def test_a_tilt_never_moves_a_mean_the_parent_actually_carried(
    reg, snapshot, monkeypatch
):
    """The pinning claim is only testable on a parent that HAS a mean."""
    import numpy as np

    from qlab.core.moments import estimate_moments
    from qlab.news.matrix import QualitativeMatrix
    from qlab.research import views_arm

    parent = estimate_moments(snapshot, lookback_days=504,
                              higher_moments=False, include_mu=True)
    assert parent.mu is not None
    loud = QualitativeMatrix("2015-09-30", "w1", {
        "ACWI": row("ACWI", 6, 3, 4, 4, keys=("a", "b", "c", "d")),
    })
    monkeypatch.setattr(views_arm, "build_matrix", lambda *a, **k: loud)

    out = _conditioner(reg).condition(parent, snapshot)
    assert not np.allclose(out.cov, parent.cov)
    assert np.array_equal(out.mu, parent.mu)
    # The tilted mean the pooling code returned did differ; it was measured
    # against the parent's and discarded, which is what makes this a check.
    assert out.provenance["mean_pinning_max_abs"] > 0.0


def test_a_view_citing_a_claim_absent_from_the_matrix_is_not_conditioned_on(
    reg, snapshot, moment_set_7, monkeypatch
):
    """Provenance is derived from the archive, never asserted by the caller."""
    from qlab.news.matrix import QualitativeMatrix
    from qlab.research import views_arm

    loud = QualitativeMatrix("2015-09-30", "w1", {
        "ACWI": row("ACWI", 6, 3, 4, 4, keys=("a", "b", "c", "d")),
    })
    monkeypatch.setattr(views_arm, "build_matrix", lambda *a, **k: loud)
    monkeypatch.setattr(views_arm, "views_from_matrix", lambda *a, **k: [
        {"type": "tail", "ticker": "ACWI", "direction": "fatter",
         "confidence": 0.3, "source_claims": ["not-in-the-archive"]}])

    cond = _conditioner(reg)
    out = cond.condition(moment_set_7, snapshot)
    assert out is moment_set_7
    assert cond.stats["windows_conditioned"] == 0
    assert cond.stats["unverified_windows"] == 1
    # A window whose views were refused applied no views. Counting them before
    # the gate makes the summary say the arm acted on evidence it rejected.
    assert cond.stats["windows_with_views"] == 0
    assert cond.stats["views_applied"] == 0
    spec = reg.newest_run_of_kind("views")["spec"]
    assert spec["provenance_verified"] is False
    assert spec["provenance_source"] == "matrix_rule"


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


def test_the_baseline_is_never_a_later_window_or_another_universes_matrix(reg):
    """A shared registry holds the owner's matrices too — and later ones.

    Taking "the second newest qualitative_matrix run" as the baseline reads a
    matrix the owner logged today, for a different universe, as though it were
    this window's past: look-ahead and cross-universe in one line.
    """
    from qlab.news.matrix import QualitativeMatrix
    from qlab.research.views_arm import ARM_MATRIX_SOURCE

    def logged(source, as_of, window, rows):
        spec = {"matrix": QualitativeMatrix(as_of, window, rows).to_dict()}
        if source:
            spec["source"] = source
        reg.log_run("qualitative_matrix", spec)

    # A later window, this universe — the look-ahead the old baseline read.
    logged(ARM_MATRIX_SOURCE, "2016-06-30", "later",
           {"ACWI": row("ACWI", 9, 3, 9, 9, keys=("z",))})
    # An earlier window, a different universe.
    logged(ARM_MATRIX_SOURCE, "2015-03-31", "other",
           {"SPY": row("SPY", 9, 3, 9, 9, keys=("s",))})
    # An earlier window the OWNER logged from live news, not this arm.
    logged(None, "2015-03-31", "owner",
           {"ACWI": row("ACWI", 7, 3, 7, 7, keys=("o",))})

    cond = _conditioner(reg)
    now = QualitativeMatrix("2015-09-30", "w2",
                            {"ACWI": row("ACWI", 8, 3, 6, 6, keys=("b",))})
    assert cond._log_and_previous(now) is None
    earlier = QualitativeMatrix("2015-06-30", "w1",
                                {"ACWI": row("ACWI", 6, 3, 4, 4, keys=("a",))})
    assert cond._log_and_previous(earlier) is None
    baseline = cond._log_and_previous(now)
    assert set(baseline) == {"ACWI"}
    assert baseline["ACWI"].primary_docs == 4  # the arm's own earlier window


def test_the_baseline_survives_a_registry_busier_than_any_python_scan(reg):
    """The predicates belong in SQL: a bounded scan loses the baseline silently.

    A lost baseline is not a lost view — ``views_from_matrix`` reads a missing
    previous window as ``prior = 0``, so every primary document counts as
    excess and the tail rule becomes MORE likely to fire, mid-walk, with no
    error anywhere.
    """
    from qlab.news.matrix import QualitativeMatrix

    cond = _conditioner(reg)
    earlier = QualitativeMatrix("2015-06-30", "w1",
                                {"ACWI": row("ACWI", 6, 3, 4, 4, keys=("a",))})
    assert cond._log_and_previous(earlier) is None

    # 600 matrices the owner logged after it, from live news, for its own
    # universe: enough to push the arm's own window past any fixed window.
    for i in range(600):
        reg.log_run("qualitative_matrix", {"matrix": QualitativeMatrix(
            f"2015-07-{i % 28 + 1:02d}", f"noise{i}",
            {"SPY": row("SPY", 1, 1, 0, 0, keys=(f"n{i}",))}).to_dict()})

    now = QualitativeMatrix("2015-09-30", "w2",
                            {"ACWI": row("ACWI", 8, 3, 6, 6, keys=("b",))})
    baseline = cond._log_and_previous(now)
    assert baseline is not None, "the arm's own earlier window is still there"
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
    import yaml

    from qlab.paths import workspace_root

    spec = yaml.safe_load(
        (workspace_root() / "configs/specs/ablation_v1.yaml").read_text())
    a5 = next(a for a in spec["arms"] if a["id"] == "A5")
    assert a5["params"]["views_source"] == "qualitative_matrix"


def test_the_runner_walks_the_arm_and_records_a_queryable_views_summary(reg):
    """A null result must be a queryable null, not a dict that dies with the run."""
    from qlab.experiment import run_ablation

    report = run_ablation({
        "name": "a5_smoke",
        "seed": 7,
        "data": {"tickers": CORE7, "start": "2013-01-01", "end": "2015-12-31"},
        "backtest": {"rebalance": "quarterly", "lookback_days": 252,
                     "cost_bps": 5},
        "arms": [
            {"id": "A1", "objective": "min_variance", "solver": "classical"},
            {"id": "A5", "objective": "min_variance", "solver": "classical",
             "params": {"views_source": "qualitative_matrix"}},
        ],
    }, registry=reg, offline=True)

    summary = reg.newest_run_of_kind("views_summary")
    assert summary is not None, "the walk's counts must survive the run"
    spec = summary["spec"]
    assert spec["ablation_run_id"] == report["run_id"]
    assert spec["arm"] == "A5"
    assert spec["windows"] > 0
    assert spec["windows"] == report["views_conditioning"]["windows"]
    assert spec["windows_with_views"] == \
        report["views_conditioning"]["windows_with_views"]
    # The counts describe the walk; they must never enter the metrics that
    # feed ranking and the DSR trial accounting.
    assert not {"windows", "views_applied"} & set(report["arms"]["A5"]["metrics"])
