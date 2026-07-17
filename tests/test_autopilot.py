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


def test_run_once_logs_verdict_and_reconciles(tmp_registry):
    from qlab.autopilot.loop import run_once
    summary = run_once(registry=tmp_registry, offline=True, execute=True, as_of="2021-06-30")
    assert summary["referee"]["verdict"] == "PASS"
    assert summary["reconcile"]["clean"] is True


def test_run_once_refuses_dirty_ledger(tmp_registry, monkeypatch):
    # SimulatedPaperBroker reads positions from the same registry as the ledger,
    # so a genuine mismatch is unreachable in-process - stub the reconcile result.
    import qlab.autopilot.loop as loop
    monkeypatch.setattr(loop, "reconcile",
                        lambda *a, **k: {"clean": False, "diffs": {"ACWI": {}}})
    summary = loop.run_once(registry=tmp_registry, offline=True, execute=True,
                            as_of="2021-06-30")
    assert summary["trade"]["executed"] is False
    assert summary["trade"]["blocked_by"] == "reconcile"


def test_reconcile_detects_stub_broker_mismatch():
    from qlab.state.registry import Registry
    from qlab.trader.reconcile import reconcile

    class StubBroker:
        def portfolio_state(self, tickers):
            return {"positions": {"ACWI": {"qty": 3.0}}}

    reg = Registry(":memory:")
    out = reconcile(reg, StubBroker(), ["ACWI"])
    assert out["clean"] is False and "ACWI" in out["diffs"]
