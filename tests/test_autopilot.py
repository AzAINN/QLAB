"""Autopilot loop: deployment, heartbeat, and cross-run reproducibility."""

from __future__ import annotations

from qlab.autopilot.loop import daily_ops, run_once
from qlab.experiment import recommend
from qlab.state.registry import Registry


def test_run_once_deploys_and_conserves_equity():
    reg = Registry(":memory:")
    summary = run_once(registry=reg, offline=True, as_of="2026-07-13")
    assert summary["trade"]["executed"] is True
    assert abs(summary["equity_after"] - 10000.0) < 5.0
    assert summary["classical_vs_quantum"]["classical"]["objective_value"] >= 0
    reg.close()


def test_daily_ops_never_trades_and_reports_triggers():
    reg = Registry(":memory:")
    result = daily_ops(registry=reg, offline=True)
    assert result["kind"] == "daily_ops"
    assert "triggers" in result
    # daily_ops must not create any order plans
    assert reg._rows("SELECT * FROM orders", []) == []
    reg.close()


def test_recommendation_is_reproducible():
    a = recommend(as_of="2026-07-13", offline=True, run_qaoa=False)
    b = recommend(as_of="2026-07-13", offline=True, run_qaoa=False)
    assert a["recommended_weights"] == b["recommended_weights"]
