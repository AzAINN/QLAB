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
    assert summary["algorithm_id"] == "hrp"
    assert summary["operational_policy"]["id"] == "hrp"
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
    a = recommend(as_of="2026-07-13", offline=True)
    b = recommend(as_of="2026-07-13", offline=True)
    assert a["recommended_weights"] == b["recommended_weights"]


def test_run_once_logs_verdict_and_reconciles(tmp_registry):
    from qlab.autopilot.loop import run_once
    summary = run_once(registry=tmp_registry, offline=True, execute=True, as_of="2021-06-30")
    assert summary["referee"]["verdict"] == "PASS"
    assert summary["reconcile"]["clean"] is True


def test_run_once_records_challenger_view(tmp_registry):
    summary = run_once(
        registry=tmp_registry,
        offline=True,
        execute=False,
        as_of="2021-06-30",
    )
    decision = tmp_registry.recent_decisions(limit=1)[0]
    assert decision["challenger_view"]
    assert "window" in decision["challenger_view"]
    assert decision["choice"]["est_vol"] > 0
    assert summary["challenger_view"] == decision["challenger_view"]


def test_reflection_loop_resolves_pending_decisions(tmp_registry):
    from datetime import date

    from qlab.core import data as market
    from qlab.core.types import Decision
    from qlab.governance.reflection import resolve_pending
    from qlab.trader.mandate import load_mandate

    tickers = load_mandate().universe_whitelist
    prices = market.get_prices(
        tickers,
        "2019-01-01",
        "2021-12-31",
        offline=True,
        seed=7,
    )
    count = len(tickers)
    tmp_registry.log_decision(Decision(
        as_of=date(2020, 6, 30),
        kind="regime",
        choice={
            "targets": {ticker: 1.0 / count for ticker in tickers},
            "regime": "calm",
            "est_vol": 0.10,
        },
        rationale="reflection regression fixture",
    ))

    assert resolve_pending(tmp_registry, prices, horizon_days=63) == 1
    decision = tmp_registry.recent_decisions(limit=1)[0]
    assert decision["realized_outcome"]["realized_vol"] > 0
    assert "vol" in decision["reflection"].lower()
    assert resolve_pending(tmp_registry, prices, horizon_days=63) == 0


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
