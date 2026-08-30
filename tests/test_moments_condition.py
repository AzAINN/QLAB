"""Views-conditioned moment sets: the tilt moves risk, never a mean.

The whole safety of the qualitative lane rests on one arithmetic fact — an
entropy-pooling tilt reweights *scenarios*, so it moves second and higher
moments while every first moment is pinned. ``condition`` re-establishes that
by construction (it copies the parent's ``mu`` and discards the tilted one)
rather than by trusting the pooling code, and records the size of what it
discarded so a silent regression is visible in the provenance.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import numpy as np
import pytest

from qlab.core.moments import condition
from qlab.core.types import MomentSet

TICKERS = ["ACWI", "BNDW", "GLD"]


def _panel_and_set(seed: int = 7) -> tuple[np.ndarray, MomentSet]:
    rng = np.random.default_rng(seed)
    panel = rng.normal(size=(120, 3)) * 0.01
    ms = MomentSet(
        tickers=list(TICKERS),
        as_of=date(2021, 6, 30),
        cov=np.cov(panel, rowvar=False, bias=True),
        mu=panel.mean(axis=0),
    )
    return panel, ms


def _tilt(n: int = 120) -> np.ndarray:
    p = np.full(n, 1.0 / n)
    p[:20] *= 3.0
    return p / p.sum()


def test_conditioning_moves_the_covariance_and_pins_every_mean():
    panel, ms = _panel_and_set()
    out = condition(ms, _tilt(), panel=panel, views_run_id="v1")

    assert np.array_equal(out.mu, ms.mu), "a view can never move a mean"
    assert not np.allclose(out.cov, ms.cov)
    assert out.provenance["parent"] == ms.content_hash()
    assert out.provenance["views_run_id"] == "v1"
    # A different covariance is a different moment set, and must not collide.
    assert out.content_hash() != ms.content_hash()


def test_the_discarded_tilted_mean_is_recorded_not_silently_dropped():
    """The pooled mean is thrown away on purpose; how far it moved is evidence."""
    from qlab.core.views import conditioned_moments

    panel, ms = _panel_and_set()
    p = _tilt()
    tilted, _ = conditioned_moments(panel, p)
    out = condition(ms, p, panel=panel, views_run_id="v1")

    drift = float(np.max(np.abs(tilted - ms.mu)))
    assert drift > 0.0, "this fixture must actually tilt the mean"
    assert out.provenance["mean_pinning_max_abs"] == pytest.approx(drift)


def test_provenance_never_enters_the_content_hash():
    """Adding lineage must not renumber every moment set already logged."""
    _, ms = _panel_and_set()
    tagged = replace(ms, provenance={"parent": "x", "views_run_id": "v1"})
    assert tagged.content_hash() == ms.content_hash()


def test_a_conditioned_set_carries_its_lineage_into_the_summary():
    panel, ms = _panel_and_set()
    out = condition(ms, _tilt(), panel=panel, views_run_id="v1")
    assert out.summary()["provenance"]["views_run_id"] == "v1"
    assert ms.summary()["provenance"] == {}


def test_conditioning_a_panel_that_is_not_this_moment_sets_panel_is_refused():
    panel, ms = _panel_and_set()
    with pytest.raises(ValueError, match="panel"):
        condition(ms, _tilt(), panel=panel[:, :2], views_run_id="v1")


def test_a_parents_own_lineage_is_carried_forward():
    panel, ms = _panel_and_set()
    first = condition(ms, _tilt(), panel=panel, views_run_id="v1")
    heavier = np.full(120, 1.0 / 120)
    heavier[:40] *= 4.0
    heavier /= heavier.sum()
    second = condition(first, heavier, panel=panel, views_run_id="v2")
    assert second.provenance["parent"] == first.content_hash()
    assert not np.allclose(second.cov, first.cov)
    assert second.provenance["views_run_id"] == "v2"


# ---------------------------------------------------------------------------
# The tool: research-stage, and gated three ways before it computes anything.
# ---------------------------------------------------------------------------
_EXCERPT = "Options markets imply unusually wide outcomes for global equities."
_VIEW = {
    "type": "tail", "ticker": "ACWI", "direction": "fatter",
    "confidence": 0.25,
    "source_quote": "options markets imply unusually wide outcomes",
}


def _session(reg):
    from qlab.ui.server import UISession

    return UISession(offline_default=True, seed=7, registry=reg)


def _views_run(session, **overrides) -> str:
    body = {"as_of": "2021-06-30", "universe": "core", "views": [_VIEW],
            "excerpt": _EXCERPT, "persist": True}
    body.update(overrides)
    return session.call_lab_tool("research.apply_views", body,
                                 offline=True)["run_id"]


def _moment_set(session) -> str:
    return session.call_lab_tool(
        "moments.estimate",
        {"as_of": "2021-06-30", "universe": "core", "lookback_days": 504},
        offline=True)["moment_set_id"]


def test_a_persisted_views_run_carries_what_conditioning_needs_to_check(reg):
    session = _session(reg)
    run_id = _views_run(session)
    spec = reg.get_run(run_id)["spec"]
    assert spec["kl_total"] > 0.0
    assert spec["kl_budget"] > 0.0
    assert spec["provenance_verified"] is True
    assert len(spec["probabilities"]) == spec["n_scenarios"]


def test_moments_condition_refuses_because_the_catalog_entry_is_research(reg):
    session = _session(reg)
    with pytest.raises(PermissionError, match="staged agent surface"):
        session.call_lab_tool(
            "moments.condition",
            {"moment_set_id": _moment_set(session),
             "views_run_id": _views_run(session)},
            offline=True)


def test_moments_condition_refuses_a_views_run_that_is_not_one(reg):
    session = _session(reg)
    other = reg.log_run("backtest", {"arm": "A1"})
    with pytest.raises(ValueError, match="not a persisted views run"):
        session.call_lab_tool(
            "moments.condition",
            {"moment_set_id": _moment_set(session), "views_run_id": other},
            offline=True)


def test_moments_condition_refuses_a_views_run_over_its_own_kl_budget(reg):
    session = _session(reg)
    run_id = reg.log_run("views", {"kl_total": 0.9, "kl_budget": 0.25,
                                   "provenance_verified": True,
                                   "probabilities": [1.0]})
    with pytest.raises(ValueError, match="KL budget"):
        session.call_lab_tool(
            "moments.condition",
            {"moment_set_id": _moment_set(session), "views_run_id": run_id},
            offline=True)


def test_moments_condition_refuses_views_whose_provenance_was_never_verified(reg):
    """Conditioning on an unsourced view is exactly what the gate exists to stop."""
    session = _session(reg)
    run_id = reg.log_run("views", {"kl_total": 0.01, "kl_budget": 0.25,
                                   "provenance_verified": False,
                                   "probabilities": [1.0]})
    with pytest.raises(ValueError, match="provenance"):
        session.call_lab_tool(
            "moments.condition",
            {"moment_set_id": _moment_set(session), "views_run_id": run_id},
            offline=True)


def test_once_promoted_the_tool_conditions_and_logs_the_lineage(reg, monkeypatch):
    """The body behind the stage gate, exercised on the path a promotion opens."""
    from dataclasses import replace as dc_replace

    from qlab.algorithms import catalog

    spec = catalog.get_algorithm("views_conditioned_min_variance")
    monkeypatch.setitem(catalog._BY_ID, spec.id,
                        dc_replace(spec, stage="operational"))

    session = _session(reg)
    parent = _moment_set(session)
    run_id = _views_run(session)
    out = session.call_lab_tool(
        "moments.condition",
        {"moment_set_id": parent, "views_run_id": run_id}, offline=True)

    assert out["parent"] == parent
    assert out["moment_set_id"] != parent
    assert out["kl_total"] > 0.0
    child = session.lab_state.get_moment_set(out["moment_set_id"])
    assert child.provenance["views_run_id"] == run_id
    row = reg.moment_set(out["moment_set_id"])
    assert row["provenance"]["views_run_id"] == run_id


def test_research_qualitative_matrix_reads_the_newest_logged_matrix(reg):
    session = _session(reg)
    empty = session.call_lab_tool(
        "research.qualitative_matrix", {"as_of": "2021-06-30"}, offline=True)
    assert empty["status"] == "never_built" and empty["rows"] == {}

    reg.log_run("qualitative_matrix", {"source": "desk", "matrix": {
        "as_of": "2021-06-30", "window_hash": "w",
        "rows": {"ACWI": {"ticker": "ACWI", "coverage": 3, "publishers": 2,
                          "corroborated": 2, "primary_docs": 2,
                          "days_to_next_release": None,
                          "claim_keys": ["k1", "k2"]}}}})
    out = session.call_lab_tool(
        "research.qualitative_matrix", {"as_of": "2021-06-30"}, offline=True)
    assert out["status"] == "ok"
    assert out["rows"]["ACWI"]["primary_docs"] == 2
    assert out["window_hash"] == "w"


def test_moments_condition_refuses_verified_views_with_no_matrix_lineage(reg):
    """Verified provenance and no lineage field at all is an inconsistent run.

    Every run the tool persists writes ``matrix_run_id`` — the matrix it
    verified against, or None for a quoted excerpt. A run that claims verified
    provenance and carries no such field was written by something that never
    established lineage, and conditioning on it would restore exactly the
    unsourced tilt the gate exists to refuse.
    """
    session = _session(reg)
    run_id = reg.log_run("views", {"kl_total": 0.01, "kl_budget": 0.25,
                                   "provenance_verified": True,
                                   "probabilities": [1.0]})
    with pytest.raises(ValueError, match="matrix_run_id"):
        session.call_lab_tool(
            "moments.condition",
            {"moment_set_id": _moment_set(session), "views_run_id": run_id},
            offline=True)
