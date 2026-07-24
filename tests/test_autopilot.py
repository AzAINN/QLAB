"""Autopilot loop: deployment, heartbeat, and cross-run reproducibility."""

from __future__ import annotations

from qlab.autopilot.loop import daily_ops, run_once
from qlab.experiment import recommend
from qlab.state.registry import Registry


def test_defensive_targets_are_validated_when_mandate_loads(tmp_path):
    import pytest
    import yaml

    from qlab.paths import data_path
    from qlab.trader.mandate import MandateViolation, load_mandate

    mandate = load_mandate()
    mandate.check_targets(mandate.defensive_targets)
    assert mandate.defensive_targets["BNDW"] == 0.40
    assert mandate.defensive_targets["GLD"] == 0.30

    raw = yaml.safe_load(data_path("mandate.yaml").read_text(encoding="utf-8"))
    raw["defensive_targets"]["GLD"] = 0.90
    invalid = tmp_path / "mandate.yaml"
    invalid.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(MandateViolation, match="invalid defensive_targets"):
        load_mandate(invalid)


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
    assert result["robust_regime_history"][-1] == (
        result["robust_regime_observation"]
    )
    # daily_ops must not create any order plans
    assert reg._rows("SELECT * FROM orders", []) == []
    reg.close()


def test_daily_ops_drift_trigger_builds_proposal_without_execution(monkeypatch):
    from datetime import date

    import qlab.autopilot.loop as loop
    from qlab.core.types import Decision
    from qlab.signals.robust import RobustRegime
    from qlab.trader.mandate import load_mandate

    reg = Registry(":memory:")
    mandate = load_mandate()
    targets = {
        ticker: 1.0 / len(mandate.universe_whitelist)
        for ticker in mandate.universe_whitelist
    }
    reg.log_decision(Decision(
        as_of=date.today(),
        kind="regime",
        choice={"targets": targets, "regime": "calm"},
        rationale="drift trigger fixture",
    ))
    # Calendar cadence is current; only the all-cash-versus-target drift fires.
    reg.record_event("plan_executed", {"plan_id": "prior-fixture"})
    monkeypatch.setattr(
        loop,
        "detect_regime_robust",
        lambda _snapshot, history=(): RobustRegime("calm", 1.0, 1.0),
    )

    orders_before = reg.list_orders()
    result = loop.daily_ops(registry=reg, mandate=mandate, offline=True)

    assert [trigger["kind"] for trigger in result["triggers"]] == ["drift"]
    assert result["proposal_plan_ids"]
    plans = reg.list_plans()
    assert {plan["plan_id"] for plan in plans} == set(result["proposal_plan_ids"])
    assert all(plan["state"] in {"checked", "refused"} for plan in plans)
    assert reg.list_orders() == orders_before
    trigger_events = [
        event for event in reg.read_events(100)
        if event["kind"] == "autopilot_trigger"
    ]
    assert len(trigger_events) == 1
    assert trigger_events[0]["payload"]["kind"] == "drift"
    reg.close()


def test_autopilot_cli_once_prints_fired_triggers(monkeypatch, capsys):
    import qlab.autopilot.cli as cli

    monkeypatch.setattr(cli, "is_trading_day", lambda _day: True)
    monkeypatch.setattr(
        cli,
        "daily_ops",
        lambda **_kwargs: {
            "triggers": [{"kind": "calendar", "detail": {"due_date": "2026-07-24"}}],
            "proposal_plan_ids": ["plan-1"],
        },
    )

    assert cli.main(["autopilot", "--offline", "--once"]) == 0
    output = capsys.readouterr().out
    assert "autopilot triggers fired: 1" in output
    assert "calendar" in output
    assert "plan-1" in output


def test_recommendation_is_reproducible():
    a = recommend(as_of="2026-07-13", offline=True)
    b = recommend(as_of="2026-07-13", offline=True)
    assert a["recommended_weights"] == b["recommended_weights"]


def test_run_once_logs_verdict_and_reconciles(tmp_registry):
    from qlab.autopilot.loop import run_once
    summary = run_once(registry=tmp_registry, offline=True, execute=True, as_of="2021-06-30")
    assert summary["referee"]["verdict"] == "PASS"
    assert summary["reconcile"]["clean"] is True


def test_run_once_referee_receives_portfolio_state_and_annualized_asset_vols(
    monkeypatch,
):
    import dataclasses

    import numpy as np
    import pytest

    import qlab.autopilot.loop as loop
    from qlab.trader.mandate import load_mandate

    reg = Registry(":memory:")
    reg.init_account(10_000.0)
    reg.con.execute(
        "UPDATE account SET high_water_mark=? WHERE id=1",
        [11_000.0],
    )
    mandate = dataclasses.replace(load_mandate(), stress_vol_limit=1e-9)
    captured = {}
    referee = loop.deterministic_referee

    def capture_referee(*args, **kwargs):
        captured.update(kwargs)
        return referee(*args, **kwargs)

    monkeypatch.setattr(loop, "deterministic_referee", capture_referee)
    summary = loop.run_once(
        registry=reg,
        mandate=mandate,
        offline=True,
        execute=False,
        as_of="2021-06-30",
    )

    assert captured["portfolio_state"]["high_water_mark"] == 11_000.0
    daily_vols = summary["diagnostics"]["portfolio_moments"]["asset_daily_vols"]
    assert set(captured["vols"]) == set(summary["targets"])
    for ticker, daily_vol in daily_vols.items():
        assert captured["vols"][ticker] == pytest.approx(
            daily_vol * np.sqrt(252.0)
        )
    reasons = summary["referee"]["reasons"]
    assert any(reason.startswith("drawdown tier: warning") for reason in reasons)
    assert any(reason.startswith("stress: WARNING") for reason in reasons)
    assert reg.get_verdict(summary["decision_id"])["reasons"] == reasons
    reg.close()


def test_referee_breaker_liquidation_is_the_only_budget_exception():
    from datetime import date

    from qlab.governance.referee import deterministic_referee
    from qlab.trader.mandate import load_mandate

    mandate = load_mandate()
    invested = {
        ticker: 1.0 / len(mandate.universe_whitelist)
        for ticker in mandate.universe_whitelist
    }
    zero = {ticker: 0.0 for ticker in mandate.universe_whitelist}
    breaker_state = {
        "drawdown": mandate.drawdown_tiers.breaker,
        "weights": invested,
    }

    verdict, reasons = deterministic_referee(
        zero,
        mandate,
        date(2020, 1, 1),
        portfolio_state=breaker_state,
    )
    assert verdict == "PASS"
    assert any("liquidation mode" in reason for reason in reasons)

    verdict, reasons = deterministic_referee(
        invested,
        mandate,
        date(2020, 1, 1),
        portfolio_state=breaker_state,
    )
    assert verdict == "FAIL"
    assert any("permits liquidation only" in reason for reason in reasons)

    verdict, reasons = deterministic_referee(
        zero,
        mandate,
        date(2020, 1, 1),
        portfolio_state={"drawdown": 0.0, "weights": invested},
    )
    assert verdict == "FAIL"
    assert any("budget violated" in reason for reason in reasons)


def test_drawdown_boundaries_tolerate_float_noise_on_both_sides():
    from qlab.trader.mandate import load_mandate, tier

    epsilon_noise = 5e-10
    for threshold, expected in (
        (0.05, "warning"),
        (0.10, "control"),
        (0.15, "breaker"),
    ):
        assert tier(threshold - epsilon_noise) == expected
        assert tier(threshold + epsilon_noise) == expected
    assert tier(0.05 - 2e-9) == "none"

    mandate = load_mandate()
    limit = mandate.trailing_drawdown_pct
    high_water_mark = 10_000.0
    for drawdown in (
        limit - epsilon_noise,
        limit,
        limit + epsilon_noise,
    ):
        equity = high_water_mark * (1.0 - drawdown)
        assert mandate.drawdown_breached(equity, high_water_mark)
    equity = high_water_mark * (1.0 - (limit - 2e-9))
    assert not mandate.drawdown_breached(equity, high_water_mark)


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


def test_run_once_cites_recalled_analogous_reflections(
    tmp_registry,
    monkeypatch,
):
    captured = {}

    def recall(fingerprint, kind=None, limit=10):
        captured.update({
            "fingerprint": fingerprint,
            "kind": kind,
            "limit": limit,
        })
        return [
            {
                "decision_id": "prior-regime-a",
                "reflection": "The shorter window lagged 60/40.",
                "similarity_score": 0.97,
            },
            {
                "decision_id": "prior-regime-b",
                "reflection": "The longer window kept volatility calibrated.",
                "similarity_score": 0.91,
            },
        ]

    monkeypatch.setattr(tmp_registry, "recall_similar_decisions", recall)
    run_once(
        registry=tmp_registry,
        offline=True,
        execute=False,
        as_of="2021-06-30",
    )

    decision = tmp_registry.recent_decisions(limit=1)[0]
    assert captured["kind"] == "regime"
    assert captured["limit"] == 2
    assert set(captured["fingerprint"]) == {
        "vol_percentile",
        "turbulence_percentile",
        "regime_label",
    }
    assert decision["choice"]["regime_label"] == decision["choice"]["regime"]
    for decision_id in ("prior-regime-a", "prior-regime-b"):
        assert decision_id in decision["rationale"]
        assert decision_id in decision["challenger_view"]


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


def test_reflection_loop_scores_realized_alpha_vs_configured_6040(tmp_registry):
    from datetime import date

    import pandas as pd
    import pytest

    from qlab.core.types import Decision
    from qlab.governance.reflection import resolve_pending

    index = pd.bdate_range("2024-01-01", periods=4)
    prices = pd.DataFrame(
        {
            "ACWI": [100.0, 100.0, 110.0, 110.0],
            "BNDW": [100.0, 100.0, 100.0, 100.0],
        },
        index=index,
    )
    tmp_registry.log_decision(Decision(
        as_of=date.fromisoformat(str(index[1].date())),
        kind="regime",
        choice={
            "targets": {"ACWI": 1.0, "BNDW": 0.0},
            "regime": "calm",
            "est_vol": 0.10,
        },
        rationale="hand-built realized-alpha fixture",
    ))

    assert resolve_pending(tmp_registry, prices, horizon_days=2) == 1
    decision = tmp_registry.recent_decisions(limit=1)[0]
    outcome = decision["realized_outcome"]
    assert outcome["realized_portfolio_return"] == pytest.approx(0.10)
    assert outcome["realized_6040_return"] == pytest.approx(0.06)
    assert outcome["realized_alpha_vs_6040"] == pytest.approx(0.04)
    assert "60/40" in decision["reflection"]
    assert "not a forecast" in decision["reflection"]


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


def test_cost_gate_pure_cases():
    import math

    from qlab.governance.referee import cost_gate
    from qlab.trader.mandate import CostConfig, load_mandate
    import dataclasses

    mandate = dataclasses.replace(load_mandate(), costs=CostConfig())

    def pre(total, notionals):
        return {"n_legs": len(notionals),
                "expected_cost": {"total": total,
                                  "legs": [{"notional": n} for n in notionals]}}

    # True all-cash initial deployment (no positions, no exposure) is exempt.
    assert cost_gate(pre(500.0, [50_000.0]), 100_000.0, 0.0, 0, mandate) == []

    # Positions with ~zero gross exposure is malformed state, not an exemption.
    assert cost_gate(pre(1.0, [1000.0]), 100_000.0, 0.0, 3, mandate) != []

    # Big traded notional, modest cost: 50k*20bps*0.5 = 50 > 1.5*10 → pass.
    assert cost_gate(pre(10.0, [25_000.0, 25_000.0]),
                     100_000.0, 1.0, 7, mandate) == []

    # Tiny trade, same cost: benefit 1 <= 15 → net-alpha refusal.
    reasons = cost_gate(pre(10.0, [1_000.0]), 100_000.0, 1.0, 7, mandate)
    assert any("net-alpha gate" in reason for reason in reasons)

    # Cost above the absolute equity cap refuses even with huge notional.
    reasons = cost_gate(pre(300.0, [50_000.0]), 100_000.0, 1.0, 7, mandate)
    assert any("absolute cap" in reason for reason in reasons)

    # Fail-closed edges: bad equity, missing decomposition, NaN cost,
    # leg-count mismatch. A zero-leg plan is a no-op and passes.
    assert cost_gate(pre(1.0, [1000.0]), 0.0, 1.0, 7, mandate) != []
    assert cost_gate({"n_legs": 2}, 100_000.0, 1.0, 7, mandate) != []
    assert cost_gate(pre(math.nan, [1000.0]), 100_000.0, 1.0, 7, mandate) != []
    bad = pre(1.0, [1000.0]); bad["n_legs"] = 3
    assert cost_gate(bad, 100_000.0, 1.0, 7, mandate) != []
    assert cost_gate({"n_legs": 0, "expected_cost": {"total": 0.0, "legs": []}},
                     100_000.0, 1.0, 7, mandate) == []


def test_cost_config_rejects_inverted_gate_assumptions():
    import pytest

    from qlab.trader.mandate import CostConfig

    with pytest.raises(ValueError):
        CostConfig(live_haircut=1.5)
    with pytest.raises(ValueError):
        CostConfig(safety_multiplier=0.5)
    with pytest.raises(ValueError):
        CostConfig(rebalance_benefit_bps=0.0)


def test_run_once_cost_gate_refuses_terminally():
    import dataclasses

    from qlab.trader.mandate import CostConfig, load_mandate
    from qlab.trader.plan import MandateViolation, OrderLeg, OrderPlan, execute_plan
    from qlab.trader.broker import get_broker

    reg = Registry(":memory:")
    absurd = dataclasses.replace(
        load_mandate(),
        costs=CostConfig(impact_k=1e9, rebalance_benefit_bps=1e-6))

    first = run_once(registry=reg, mandate=absurd, offline=True,
                     execute=True, as_of="2021-06-30")
    assert first["trade"]["executed"] is True  # initial deployment is exempt

    second = run_once(registry=reg, mandate=absurd, offline=True,
                      execute=True, as_of="2022-09-30")
    trade = second["trade"]
    assert trade.get("blocked_by") == "cost_gate", trade
    assert trade["executed"] is False
    # The summary's governance state matches the registry: gate FAIL wins.
    assert second["referee"]["verdict"] == "FAIL"
    verdict = reg.get_verdict(second["decision_id"])
    assert verdict["verdict"] == "FAIL"

    # Revival attempt: a later PASS bound to the exact targets must NOT
    # resurrect the refused plan — refusal is a terminal plan state.
    refused = [p for p in reg.list_plans(10) if p["state"] == "refused"]
    assert refused, "gate refusal must persist a refused plan state"
    plan_row = refused[0]
    reg.log_verdict(plan_row["decision_id"], "PASS", ["revival attempt"],
                    source="agent", targets=plan_row["targets"])
    legs = [OrderLeg(**leg) for leg in (plan_row.get("legs") or [])]
    plan = OrderPlan(plan_id=plan_row["plan_id"],
                     decision_id=plan_row["decision_id"],
                     targets=plan_row["targets"], legs=legs,
                     pre_trade=plan_row["pre_trade"], state="checked")
    broker = get_broker(reg, offline=True, starting_cash=absurd.paper_capital,
                        seed=7, universe=absurd.universe_whitelist)
    import pytest
    with pytest.raises(MandateViolation, match="terminal"):
        execute_plan(reg, broker, plan)
