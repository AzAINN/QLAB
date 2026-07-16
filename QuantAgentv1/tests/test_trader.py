"""Execution gateway: mandate enforcement, two-phase plans, kill-switch."""

from __future__ import annotations

import pytest

from qlab.trader.broker import SimulatedPaperBroker, default_price_provider
from qlab.trader.mandate import Mandate, MandateViolation, load_mandate
from qlab.trader.plan import build_plan, execute_plan

CORE = ["ACWI", "BNDW", "GSG", "IGF", "GLD", "VNQ", "EMB"]


def _broker(reg):
    return SimulatedPaperBroker(
        reg, default_price_provider(offline=True, seed=7),
        starting_cash=10000.0, universe=CORE)


def test_mandate_rejects_offuniverse_ticker():
    m = load_mandate()
    with pytest.raises(MandateViolation):
        m.check_targets({"TSLA": 1.0})


def test_mandate_rejects_over_cap():
    m = load_mandate()
    with pytest.raises(MandateViolation):
        m.check_targets({"GLD": 0.9, "ACWI": 0.1})   # 0.9 > 0.40 cap


def test_initial_deployment_conserves_equity(reg):
    m = load_mandate()
    broker = _broker(reg)
    targets = {t: 1.0 / len(CORE) for t in CORE}
    plan = build_plan(reg, broker, m, targets, "d1")
    assert plan.state == "checked"
    assert plan.pre_trade["initial_deployment"] is True
    execute_plan(reg, broker, plan)
    state = broker.portfolio_state(CORE)
    assert abs(state["equity"] - 10000.0) < 1.0     # only tiny cost drag


def test_execution_is_idempotent(reg):
    m = load_mandate()
    broker = _broker(reg)
    targets = {t: 1.0 / len(CORE) for t in CORE}
    plan = build_plan(reg, broker, m, targets, "d1")
    coids = {leg.client_order_id for leg in plan.legs}
    # same decision + targets → same plan_id → same client_order_ids (idempotent)
    plan2 = build_plan(reg, broker, m, targets, "d1")
    assert plan2.plan_id == plan.plan_id
    assert {leg.client_order_id for leg in plan2.legs} == coids


def test_kill_switch_blocks_trading(reg):
    m = Mandate(paper_capital=10000.0, universe_whitelist=CORE,
                max_weight_per_asset=1.0, trailing_drawdown_pct=0.10)
    broker = _broker(reg)
    # simulate a large drawdown: high-water 20000, equity 10000 → 50% dd
    reg.init_account(10000.0)
    reg.update_high_water_mark(20000.0)
    with pytest.raises(MandateViolation):
        build_plan(reg, broker, m, {t: 1.0 / len(CORE) for t in CORE}, "d1")
    assert reg.get_account()["halted"] is True
