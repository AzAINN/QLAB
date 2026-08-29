"""The referee's lineage check on a views-conditioned moment set.

The deterministic referee already re-verifies the mandate facts. A conditioned
covariance adds one more thing it must not take on trust: the views run that
tilted it. A solve whose moment set names a views run FAILS unless that run is
in the registry, stayed inside its own KL budget, and had its provenance
verified — otherwise the qualitative quarantine ends at the moment the
optimizer reads the tensor.
"""

from __future__ import annotations

from datetime import date

import pytest

from qlab.governance.referee import deterministic_referee
from qlab.trader.mandate import load_mandate

# Mandate-legal on its own (per-asset cap 0.4), so the only thing under
# test here is the lineage check.
TARGETS = {"ACWI": 0.3, "BNDW": 0.3, "GLD": 0.2, "EMB": 0.2}


def _summary(**provenance) -> dict:
    return {"n": 2, "condition_number": 10.0, "provenance": dict(provenance)}


def _referee(reg, summary):
    return deterministic_referee(
        TARGETS, load_mandate(), date(2021, 6, 30),
        moments_summary=summary, registry=reg)


def test_an_unconditioned_moment_set_still_passes(reg):
    status, reasons = _referee(reg, _summary())
    assert status == "PASS"
    assert not [r for r in reasons if "views run" in r]


def test_a_conditioned_moment_set_without_its_views_run_fails_the_referee(reg):
    status, reasons = _referee(reg, _summary(views_run_id="ghost"))
    assert status == "FAIL"
    assert any("views run 'ghost'" in r and "not in the registry" in r
               for r in reasons)


def test_a_conditioned_moment_set_over_the_kl_budget_fails(reg):
    run_id = reg.log_run("views", {"kl_total": 0.9, "kl_budget": 0.25,
                                   "provenance_verified": True})
    status, reasons = _referee(reg, _summary(views_run_id=run_id))
    assert status == "FAIL"
    assert any("KL budget" in r for r in reasons)


def test_a_conditioned_moment_set_on_unverified_views_fails(reg):
    run_id = reg.log_run("views", {"kl_total": 0.01, "kl_budget": 0.25,
                                   "provenance_verified": False})
    status, reasons = _referee(reg, _summary(views_run_id=run_id))
    assert status == "FAIL"
    assert any("provenance" in r for r in reasons)


def test_a_well_formed_views_run_passes_and_is_named_in_the_audit(reg):
    run_id = reg.log_run("views", {"kl_total": 0.01, "kl_budget": 0.25,
                                   "provenance_verified": True})
    status, reasons = _referee(reg, _summary(views_run_id=run_id))
    assert status == "PASS"
    assert any(run_id in r for r in reasons)


def test_lineage_that_cannot_be_checked_at_all_fails_rather_than_passes(reg):
    """No registry to check against is a refusal, never a quiet exemption."""
    status, reasons = deterministic_referee(
        TARGETS, load_mandate(), date(2021, 6, 30),
        moments_summary=_summary(views_run_id="v1"))
    assert status == "FAIL"
    assert any("cannot be verified" in r for r in reasons)


def test_the_trigger_proposal_passes_its_registry_so_the_check_actually_runs(
    reg, monkeypatch
):
    """Invariant 10: a gate nothing calls with a registry is a gate that never runs.

    The autopilot's other call site (``run_once``) is asserted the same way in
    ``tests/test_autopilot.py``; both check the object the referee actually
    received rather than the source text that passes it.
    """
    from qlab.autopilot import loop

    captured: dict = {}
    referee = loop.deterministic_referee

    def capture_referee(*args, **kwargs):
        captured.update(kwargs)
        return referee(*args, **kwargs)

    monkeypatch.setattr(loop, "deterministic_referee", capture_referee)
    proposal = loop._build_trigger_proposal(
        reg, None, load_mandate(),
        {"kind": "drift", "detail": {}}, TARGETS, date(2021, 6, 30),
        {"equity": 10_000.0, "high_water_mark": 10_000.0,
         "weights": {}, "positions": {}},
        {"clean": True}, "calm",
        moments_summary=_summary(views_run_id="ghost"))

    assert captured["registry"] is reg
    # And the lineage check that registry enabled is what refused the proposal.
    assert proposal["blocked_by"] == "referee"
    assert any("not in the registry" in r for r in proposal["reasons"])


@pytest.mark.parametrize("kind", ["backtest", "solve"])
def test_a_run_of_the_wrong_kind_is_not_a_views_run(reg, kind):
    run_id = reg.log_run(kind, {"kl_total": 0.0, "kl_budget": 1.0})
    status, reasons = _referee(reg, _summary(views_run_id=run_id))
    assert status == "FAIL"
    assert any("not in the registry" in r for r in reasons)


def _matrix_run(reg, as_of: str) -> str:
    return reg.log_run("qualitative_matrix", {
        "source": "desk",
        "matrix": {"as_of": as_of, "window_hash": as_of, "rows": {}}})


def test_a_views_run_sourced_from_a_later_matrix_fails(reg):
    """Provenance verified against a window the solve could not have seen.

    The lineage check re-verified that provenance was checked, never against
    WHAT. A matrix dated after the rebalance is look-ahead that has already
    passed every upstream gate, so the referee is the last place it can be
    caught — and it is the one gate holding the solve's own date.
    """
    matrix = _matrix_run(reg, "2021-07-31")
    run_id = reg.log_run("views", {"kl_total": 0.01, "kl_budget": 0.25,
                                   "provenance_verified": True,
                                   "matrix_run_id": matrix})
    status, reasons = _referee(reg, _summary(views_run_id=run_id))
    assert status == "FAIL"
    assert any("2021-07-31" in r and "2021-06-30" in r for r in reasons)


def test_a_views_run_sourced_from_a_matrix_at_or_before_the_solve_passes(reg):
    matrix = _matrix_run(reg, "2021-06-30")
    run_id = reg.log_run("views", {"kl_total": 0.01, "kl_budget": 0.25,
                                   "provenance_verified": True,
                                   "matrix_run_id": matrix})
    status, reasons = _referee(reg, _summary(views_run_id=run_id))
    assert status == "PASS"
    assert any(run_id in r for r in reasons)


def test_a_cited_matrix_that_is_not_in_the_registry_fails(reg):
    run_id = reg.log_run("views", {"kl_total": 0.01, "kl_budget": 0.25,
                                   "provenance_verified": True,
                                   "matrix_run_id": "ghost"})
    status, reasons = _referee(reg, _summary(views_run_id=run_id))
    assert status == "FAIL"
    assert any("'ghost'" in r for r in reasons)
