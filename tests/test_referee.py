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


def test_the_autopilot_passes_its_registry_so_the_check_actually_runs():
    """Invariant 10: a gate nothing calls with a registry is a gate that never runs."""
    import inspect

    from qlab.autopilot import loop

    src = inspect.getsource(loop)
    assert src.count("registry=reg") >= 1


@pytest.mark.parametrize("kind", ["backtest", "solve"])
def test_a_run_of_the_wrong_kind_is_not_a_views_run(reg, kind):
    run_id = reg.log_run(kind, {"kl_total": 0.0, "kl_budget": 1.0})
    status, reasons = _referee(reg, _summary(views_run_id=run_id))
    assert status == "FAIL"
    assert any("not in the registry" in r for r in reasons)
