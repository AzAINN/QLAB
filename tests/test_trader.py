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
    reg.log_verdict("d1", "PASS", [], "deterministic", targets=targets)
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


def test_execute_requires_referee_pass(reg_and_broker):     # use existing fixtures' style
    import pytest
    from qlab.trader.mandate import MandateViolation, load_mandate
    from qlab.trader.plan import build_plan, execute_plan
    reg, broker = reg_and_broker
    mandate = load_mandate()
    targets = {t: 1.0 / len(mandate.universe_whitelist) for t in mandate.universe_whitelist}
    plan = build_plan(reg, broker, mandate, targets, "dec1")
    with pytest.raises(MandateViolation, match="referee"):
        execute_plan(reg, broker, plan)
    reg.log_verdict("dec1", "FAIL", ["planted flaw"], "deterministic", targets=targets)
    with pytest.raises(MandateViolation, match="referee"):
        execute_plan(reg, broker, plan)
    reg.log_verdict("dec1", "PASS", [], "deterministic", targets=targets)
    out = execute_plan(reg, broker, plan)
    assert out["state"] == "reconciled"


def test_deterministic_referee_catches_planted_flaw():
    from datetime import date
    from qlab.governance.referee import deterministic_referee
    from qlab.trader.mandate import load_mandate
    m = load_mandate()
    bad = {m.universe_whitelist[0]: 0.95, m.universe_whitelist[1]: 0.05}  # cap breach
    verdict, reasons = deterministic_referee(bad, m, date(2020, 1, 1))
    assert verdict == "FAIL" and any("cap" in r or "weight" in r for r in reasons)
    good = {t: 1.0 / len(m.universe_whitelist) for t in m.universe_whitelist}
    assert deterministic_referee(good, m, date(2020, 1, 1))[0] == "PASS"


def test_deterministic_referee_flags_ill_conditioned_covariance():
    from datetime import date
    from qlab.governance.referee import deterministic_referee
    from qlab.trader.mandate import load_mandate
    m = load_mandate()
    good = {t: 1.0 / len(m.universe_whitelist) for t in m.universe_whitelist}
    verdict, reasons = deterministic_referee(
        good, m, date(2020, 1, 1), moments_summary={"condition_number": 1e9})
    assert verdict == "FAIL"
    assert any("ill-conditioned" in r for r in reasons)


def test_verdict_does_not_transfer_to_different_targets(reg_and_broker):
    reg, broker = reg_and_broker
    mandate = load_mandate()
    tickers = mandate.universe_whitelist
    targets_a = {t: 1.0 / len(tickers) for t in tickers}
    reg.log_verdict("adhoc", "PASS", [], "deterministic", targets=targets_a)

    # A genuinely different (but still mandate-legal) target set under the
    # SAME decision_id - the old bug let a stale PASS cover this silently.
    targets_b = dict(targets_a)
    targets_b[tickers[0]] += 0.05
    targets_b[tickers[1]] -= 0.05

    plan_b = build_plan(reg, broker, mandate, targets_b, "adhoc")
    with pytest.raises(MandateViolation, match="does not cover"):
        execute_plan(reg, broker, plan_b)

    reg.log_verdict("adhoc", "PASS", [], "deterministic", targets=targets_b)
    out = execute_plan(reg, broker, plan_b)
    assert out["state"] == "reconciled"


def test_latest_verdict_wins_when_timestamps_collide(reg_and_broker, monkeypatch):
    """Same-timestamp risk: two verdicts logged in a tight loop must still be
    ordered correctly by the monotonic ``seq`` column, not ``created_at``."""
    import qlab.state.registry as registry_mod
    reg, broker = reg_and_broker
    mandate = load_mandate()
    tickers = mandate.universe_whitelist
    targets = {t: 1.0 / len(tickers) for t in tickers}

    frozen = "2024-01-01T00:00:00+00:00"
    monkeypatch.setattr(registry_mod, "_now", lambda: frozen)

    reg.log_verdict("dec-seq", "PASS", [], "deterministic", targets=targets)
    reg.log_verdict("dec-seq", "FAIL", ["reversed"], "deterministic", targets=targets)

    v = reg.get_verdict("dec-seq")
    assert v["created_at"] == frozen        # confirms the timestamps really collided
    assert v["verdict"] == "FAIL"           # the later verdict, not the earlier PASS

    plan = build_plan(reg, broker, mandate, targets, "dec-seq")
    with pytest.raises(MandateViolation, match="referee"):
        execute_plan(reg, broker, plan)
