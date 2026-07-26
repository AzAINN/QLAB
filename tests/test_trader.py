"""Execution gateway: mandate enforcement, two-phase plans, kill-switch."""

from __future__ import annotations

from dataclasses import replace

import pytest

from qlab.state.registry import Registry
from qlab.trader.broker import SimulatedPaperBroker, default_price_provider
from qlab.trader.mandate import Mandate, MandateViolation, load_mandate, tier
from qlab.trader.plan import OrderLeg, OrderPlan, build_plan, execute_plan

CORE = ["ACWI", "BNDW", "GSG", "IGF", "GLD", "VNQ", "EMB"]


class _StubApp:
    """A framework-independent stand-in for FastMCP: records tool functions by
    name (mirrors ``tests/test_mcp_server.py::StubApp``) so ``execute_plan``
    can be exercised through the real MCP wiring without a live FastMCP app.
    """

    def __init__(self):
        self.tools = {}

    def tool(self, name: str):
        def deco(fn):
            self.tools[name] = fn
            return fn

        return deco


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


def test_replayed_plan_does_not_double_fill(reg_and_broker):
    reg, broker = reg_and_broker
    mandate = load_mandate()
    targets = {
        ticker: 1.0 / len(mandate.universe_whitelist)
        for ticker in mandate.universe_whitelist
    }
    plan = build_plan(reg, broker, mandate, targets, "dec-replay")
    reg.log_verdict(
        "dec-replay", "PASS", [], "deterministic", targets=targets,
    )

    execute_plan(reg, broker, plan)
    positions_after_first = {
        ticker: position["qty"]
        for ticker, position in reg.get_positions().items()
    }
    cash_after_first = reg.get_account()["cash"]

    # Simulate resuming from a session that still presents the checked plan.
    plan.state = "checked"
    reg.set_plan_state(plan.plan_id, "checked")
    replay = execute_plan(reg, broker, plan)

    positions_after_replay = {
        ticker: position["qty"]
        for ticker, position in reg.get_positions().items()
    }
    assert positions_after_replay == positions_after_first
    assert abs(reg.get_account()["cash"] - cash_after_first) < 1e-9
    assert replay["fills"]
    assert all(fill.get("replayed") for fill in replay["fills"])

    orders = reg.list_orders()
    assert len(orders) == len(plan.legs)
    assert all(order["plan_id"] == plan.plan_id for order in orders)


def test_simulated_fill_rolls_back_if_leg_crashes(reg_and_broker):
    reg, healthy_broker = reg_and_broker
    mandate = load_mandate()
    targets = {
        ticker: 1.0 / len(mandate.universe_whitelist)
        for ticker in mandate.universe_whitelist
    }
    plan = build_plan(reg, healthy_broker, mandate, targets, "dec-crash")
    reg.log_verdict(
        "dec-crash", "PASS", [], "deterministic", targets=targets,
    )

    class CrashAfterBookingBroker(SimulatedPaperBroker):
        def submit_notional(self, client_order_id, ticker, side, notional):
            super().submit_notional(client_order_id, ticker, side, notional)
            raise RuntimeError("injected crash after local booking")

    crashing_broker = CrashAfterBookingBroker(
        reg,
        default_price_provider(offline=True, seed=7),
        starting_cash=mandate.paper_capital,
        universe=mandate.universe_whitelist,
    )
    with pytest.raises(RuntimeError, match="injected crash"):
        execute_plan(reg, crashing_broker, plan)

    assert reg.get_positions() == {}
    assert reg.get_account()["cash"] == mandate.paper_capital
    assert reg.list_orders() == []

    # The same stable plan can now resume normally and applies each leg once.
    resumed = execute_plan(reg, healthy_broker, plan)
    assert resumed["state"] == "reconciled"
    assert len(reg.list_orders()) == len(plan.legs)


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
    from qlab.trader.plan import OrderLeg, OrderPlan, build_plan, execute_plan
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


@pytest.mark.parametrize(
    ("drawdown", "expected"),
    [
        (0.0, "none"),
        (0.049999, "none"),
        (0.05, "warning"),
        (0.099999, "warning"),
        (0.10, "control"),
        (0.149999, "control"),
        (0.15, "breaker"),
    ],
)
def test_drawdown_tier_boundaries(drawdown, expected):
    assert tier(drawdown) == expected


def test_drawdown_tier_thresholds_are_configurable():
    assert tier(0.06, warning=0.02, control=0.04, breaker=0.08) == "control"


def test_referee_blocks_exposure_increase_at_control_drawdown():
    from datetime import date

    from qlab.governance.referee import deterministic_referee

    mandate = load_mandate()
    targets = {
        ticker: 1.0 / len(mandate.universe_whitelist)
        for ticker in mandate.universe_whitelist
    }
    state = {
        "drawdown": mandate.drawdown_tiers.control,
        "weights": {
            ticker: 0.10
            for ticker in mandate.universe_whitelist
        },
    }

    verdict, reasons = deterministic_referee(
        targets,
        mandate,
        date(2020, 1, 1),
        portfolio_state=state,
    )

    assert verdict == "FAIL"
    assert any("control tier blocks gross exposure increase" in reason for reason in reasons)


def test_referee_breaker_allows_only_liquidating_targets():
    from datetime import date

    from qlab.governance.referee import deterministic_referee

    mandate = load_mandate()
    targets = {
        ticker: 1.0 / len(mandate.universe_whitelist)
        for ticker in mandate.universe_whitelist
    }
    state = {
        "drawdown": mandate.drawdown_tiers.breaker,
        "weights": targets,
    }

    verdict, reasons = deterministic_referee(
        targets,
        mandate,
        date(2020, 1, 1),
        portfolio_state=state,
    )

    assert verdict == "FAIL"
    assert any("breaker tier permits liquidation only" in reason for reason in reasons)


def test_referee_enforces_abs_sum_gross_exposure_cap():
    from datetime import date

    from qlab.governance.referee import deterministic_referee

    mandate = replace(
        load_mandate(),
        long_only=False,
        max_weight_per_asset=2.0,
        max_gross_exposure=1.0,
    )
    targets = {
        mandate.universe_whitelist[0]: 1.10,
        mandate.universe_whitelist[1]: -0.10,
    }

    verdict, reasons = deterministic_referee(
        targets,
        mandate,
        date(2020, 1, 1),
    )

    assert verdict == "FAIL"
    assert any("gross exposure cap breached: 1.2000" in reason for reason in reasons)


def test_referee_stress_limit_is_audited_warning_not_failure():
    from datetime import date

    from qlab.governance.referee import deterministic_referee

    mandate = load_mandate()
    targets = {
        ticker: 1.0 / len(mandate.universe_whitelist)
        for ticker in mandate.universe_whitelist
    }
    vols = {ticker: 0.40 for ticker in targets}

    verdict, reasons = deterministic_referee(
        targets,
        mandate,
        date(2020, 1, 1),
        vols=vols,
    )

    assert verdict == "PASS"
    assert any(reason.startswith("stress: WARNING") for reason in reasons)


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


def test_execute_plan_tool_resumes_cross_session_from_registry_only(tmp_registry,
                                                                     monkeypatch):
    """A resumed session (fresh ``TraderState``, empty in-memory ``st.plans``)
    must rebuild REAL legs from the persisted plan row -- not silently execute
    a plan with zero legs, which used to record a bogus 'reconciled' with 0
    fills and never move a single position.
    """
    from qlab.mcp.quant_trader import TraderState, register_trader_tools

    monkeypatch.setenv("QLAB_HEADLESS_EXECUTE", "1")
    reg = tmp_registry
    st1 = TraderState(registry=reg, offline=True)
    targets = {t: 1.0 / len(st1.mandate.universe_whitelist)
               for t in st1.mandate.universe_whitelist}
    plan = build_plan(reg, st1.broker, st1.mandate, targets, "dec-cross-session")
    assert len(plan.legs) > 0
    reg.log_verdict("dec-cross-session", "PASS", [], "deterministic", targets=targets)

    # Simulate a fresh process: a brand-new TraderState/app sharing only the
    # registry -- exactly what execute_plan_tool sees when st.plans.get(plan_id)
    # misses because the process restarted.
    st2 = TraderState(registry=reg, offline=True)
    app = _StubApp()
    register_trader_tools(app, st2)
    execute_plan_tool = app.tools["execute_plan"]

    result = execute_plan_tool(plan.plan_id, human_confirmed=True)
    assert result["executed"] is True
    assert len(result["fills"]) == len(plan.legs)
    positions = reg.get_positions()
    assert positions
    assert all(abs(p["qty"]) > 0 for p in positions.values())


def test_execute_plan_tool_resumes_mid_execution_submitted_plan(tmp_registry,
                                                                monkeypatch):
    """A plan that crashed mid-execution is persisted as 'submitted' (one leg
    already booked). Resuming it must fill only the remaining legs, replay the
    already-filled leg without re-applying its cash/position delta, and reach
    'reconciled' -- not brick forever because it isn't 'checked' anymore.
    """
    from qlab.mcp.quant_trader import TraderState, register_trader_tools

    monkeypatch.setenv("QLAB_HEADLESS_EXECUTE", "1")
    reg = tmp_registry
    st1 = TraderState(registry=reg, offline=True)
    targets = {t: 1.0 / len(st1.mandate.universe_whitelist)
               for t in st1.mandate.universe_whitelist}
    plan = build_plan(reg, st1.broker, st1.mandate, targets, "dec-mid-exec")
    assert len(plan.legs) >= 2
    reg.log_verdict("dec-mid-exec", "PASS", [], "deterministic", targets=targets)

    # Simulate a crash right after the FIRST leg was booked, before the plan
    # advanced past "submitted".
    first_leg = plan.legs[0]
    reg.set_plan_state(plan.plan_id, "submitted")
    reg.add_order(first_leg.client_order_id, plan.plan_id, first_leg.ticker,
                  first_leg.side, first_leg.notional, state="submitted")
    fill = st1.broker.submit_notional(first_leg.client_order_id, first_leg.ticker,
                                      first_leg.side, first_leg.notional)
    reg.update_order_state(first_leg.client_order_id, fill.get("state", "filled"))
    qty_after_prefill = reg.get_positions()[first_leg.ticker]["qty"]
    cash_after_prefill = reg.get_account()["cash"]

    # Fresh session resumes purely from the registry-persisted "submitted" plan.
    st2 = TraderState(registry=reg, offline=True)
    app = _StubApp()
    register_trader_tools(app, st2)
    execute_plan_tool = app.tools["execute_plan"]

    result = execute_plan_tool(plan.plan_id, human_confirmed=True)
    assert result["executed"] is True
    assert result["state"] == "reconciled"
    fills = result["fills"]
    assert len(fills) == len(plan.legs)
    by_coid = {f["client_order_id"]: f for f in fills}
    assert by_coid[first_leg.client_order_id]["replayed"] is True
    assert all(not by_coid[l.client_order_id].get("replayed")
               for l in plan.legs[1:])

    # no double-apply of the pre-filled leg's cash/position delta
    assert reg.get_positions()[first_leg.ticker]["qty"] == pytest.approx(qty_after_prefill)
    assert reg.get_account()["cash"] != pytest.approx(cash_after_prefill)


def test_execute_plan_tool_refuses_legacy_plan_with_no_persisted_legs(tmp_registry,
                                                                       monkeypatch):
    """A plan row from before the ``legs`` column existed (or any row somehow
    missing persisted legs) must never silently 'execute' zero legs -- it must
    refuse and point the caller at re-proposing.
    """
    from qlab.mcp.quant_trader import TraderState, register_trader_tools

    monkeypatch.setenv("QLAB_HEADLESS_EXECUTE", "1")
    reg = tmp_registry
    reg.create_plan("legacy-plan-1", "dec-legacy", {"ACWI": 1.0}, {"n_legs": 1})
    reg.set_plan_state("legacy-plan-1", "checked")
    reg.log_verdict("dec-legacy", "PASS", [], "deterministic", targets={"ACWI": 1.0})

    st = TraderState(registry=reg, offline=True)
    app = _StubApp()
    register_trader_tools(app, st)
    execute_plan_tool = app.tools["execute_plan"]

    result = execute_plan_tool("legacy-plan-1", human_confirmed=True)
    assert result["executed"] is False
    assert "re-propose" in result["error"]


def test_build_plan_sells_positions_omitted_from_targets(reg_and_broker):
    import dataclasses

    reg, broker = reg_and_broker
    mandate = dataclasses.replace(load_mandate(), max_turnover_per_rebalance=2.0)
    whitelist = mandate.universe_whitelist
    # Deploy all assets equally.
    full = {t: 1.0 / len(whitelist) for t in whitelist}
    p1 = build_plan(reg, broker, mandate, full, "dec-full")
    reg.log_verdict("dec-full", "PASS", [], "deterministic", targets=full)
    execute_plan(reg, broker, p1)

    # Now target only the first two names — the rest must be SOLD, not retained.
    subset = {whitelist[0]: 0.34, whitelist[1]: 0.33, whitelist[2]: 0.33}
    p2 = build_plan(reg, broker, mandate, subset, "dec-subset")
    sold = {leg.ticker for leg in p2.legs if leg.side == "sell"}
    for omitted in whitelist[3:]:
        assert omitted in sold, f"{omitted} was omitted from targets but not sold"


def test_execute_refuses_a_forged_plan_object(reg_and_broker):
    reg, broker = reg_and_broker
    mandate = load_mandate()
    w = mandate.universe_whitelist
    targets = {w[0]: 0.34, w[1]: 0.33, w[2]: 0.33}
    plan = build_plan(reg, broker, mandate, targets, "dec-legit")
    reg.log_verdict("dec-legit", "PASS", [], "deterministic", targets=targets)

    # Forge an object with the real plan_id but a different decision and legs.
    forged = OrderPlan(
        plan_id=plan.plan_id, decision_id="dec-other",
        targets={w[0]: 0.34, w[1]: 0.33, w[3]: 0.33},
        legs=[OrderLeg(w[3], "buy", 999.0, "forged-1")],
        pre_trade=plan.pre_trade, state="checked")
    reg.log_verdict("dec-other", "PASS", [],
                    "deterministic", targets=forged.targets)
    with pytest.raises(MandateViolation, match="does not match the persisted"):
        execute_plan(reg, broker, forged, mandate)


def test_execute_refuses_a_plan_checked_against_a_stale_book(reg_and_broker):
    reg, broker = reg_and_broker
    mandate = load_mandate()
    w = mandate.universe_whitelist
    full = {t: 1.0 / len(w) for t in w}
    # Two plans built while the book is all-cash, both fully deploying.
    p1 = build_plan(reg, broker, mandate, full, "dec-a")
    p2 = build_plan(reg, broker, mandate, dict(full), "dec-b")
    reg.log_verdict("dec-a", "PASS", [], "deterministic", targets=full)
    reg.log_verdict("dec-b", "PASS", [], "deterministic", targets=full)
    execute_plan(reg, broker, p1, mandate)  # deploys the book
    # p2 assumed all-cash; the book is now invested → must refuse, not double-deploy.
    with pytest.raises(MandateViolation, match="stale book"):
        execute_plan(reg, broker, p2, mandate)


def test_execute_plan_tool_is_not_agent_reachable_without_human(tmp_registry,
                                                                monkeypatch):
    from qlab.mcp.quant_trader import TraderState, register_trader_tools

    class _App:
        def __init__(self):
            self.tools = {}

        def tool(self, name):
            def deco(fn):
                self.tools[name] = fn
                return fn
            return deco

    st = TraderState(registry=tmp_registry, offline=True)
    app = _App()
    register_trader_tools(app, st)
    # No human_confirmed → refused, execution is not agent-reachable.
    monkeypatch.delenv("QLAB_HEADLESS_EXECUTE", raising=False)
    out = app.tools["execute_plan"]("any-plan")
    assert out["executed"] is False
    assert "human_confirmed" in out["error"]

    # A bare human_confirmed=True is self-attestation an agent can forge; it is
    # NOT sufficient without the operator's out-of-band process authorization
    # (authority-matrix invariant #14). It must still refuse when the operator
    # has not set QLAB_HEADLESS_EXECUTE.
    out = app.tools["execute_plan"]("any-plan", human_confirmed=True)
    assert out["executed"] is False
    assert "not agent-reachable" in out["error"]


def test_daily_order_cap_is_cumulative_across_plans(reg_and_broker):
    import dataclasses

    reg, broker = reg_and_broker
    mandate = dataclasses.replace(
        load_mandate(), max_orders_per_day=4,
        max_turnover_per_rebalance=5.0)
    w = mandate.universe_whitelist
    t1 = {w[0]: 0.34, w[1]: 0.33, w[2]: 0.33}
    p1 = build_plan(reg, broker, mandate, t1, "dec-day1")
    reg.log_verdict("dec-day1", "PASS", [], "deterministic", targets=t1)
    execute_plan(reg, broker, p1, mandate)
    # A second plan the same day must count the first plan's orders and refuse.
    t2 = {w[0]: 0.4, w[3]: 0.3, w[4]: 0.3}
    with pytest.raises(MandateViolation, match="daily cap"):
        build_plan(reg, broker, mandate, t2, "dec-day2")


# --- governance-batch regressions (Phase 0 hardening) ------------------------


def test_costconfig_rejects_nonfinite_fields():
    """A NaN gate bound makes every comparison false, so the net-alpha gate
    would silently pass. Direct construction (bypassing the YAML loader's
    finiteness check) must still fail closed.
    """
    from qlab.trader.mandate import CostConfig

    for field_name in ("safety_multiplier", "live_haircut",
                       "rebalance_benefit_bps", "max_cost_bps_of_equity"):
        with pytest.raises(ValueError, match="finite"):
            CostConfig(**{field_name: float("nan")})


def test_cost_gate_refuses_negative_cost_even_on_initial_deployment():
    """A malformed cost decomposition (negative total) is a data fault that must
    fail closed BEFORE the all-cash initial-deployment exemption — otherwise a
    plan with garbage costs deploys unchecked on first use.
    """
    from qlab.governance.referee import cost_gate

    mandate = load_mandate()
    pre_trade = {"n_legs": 2,
                 "expected_cost": {"total": -5.0,
                                   "legs": [{"notional": 100.0},
                                            {"notional": 100.0}]}}
    reasons = cost_gate(pre_trade, 10000.0, 0.0, 0, mandate)
    assert reasons, "negative cost on an all-cash book must not be exempted"
    assert any("negative" in r for r in reasons)


def test_simulator_sell_never_oversells_into_a_short(reg):
    """A sell notional fixed at plan time can exceed the position if the price
    fell before execution; the simulator must clamp the sell to the held
    quantity so a full liquidation lands at flat, never short (long-only).
    """
    broker = _broker(reg)
    broker.submit_notional("coid-buy", "GLD", "buy", 1000.0)
    held = reg.get_positions()["GLD"]["qty"]
    assert held > 0
    fill = broker.submit_notional("coid-sell", "GLD", "sell", 100000.0)
    assert fill["qty"] == pytest.approx(held)
    assert reg.get_positions().get("GLD", {}).get("qty", 0.0) >= -1e-9
    assert reg.get_positions().get("GLD", {}).get("qty", 0.0) == pytest.approx(0.0)


def test_execute_revalidates_submitted_plan_with_zero_fills(reg_and_broker):
    """A plan that reached 'submitted' but crashed before ANY fill must still
    revalidate against the live book. Two all-cash plans, the first executed:
    the second, forced to 'submitted' with zero fills, must refuse rather than
    deploy on top of the positions the first created.
    """
    reg, broker = reg_and_broker
    mandate = load_mandate()
    w = mandate.universe_whitelist
    full = {t: 1.0 / len(w) for t in w}
    p1 = build_plan(reg, broker, mandate, full, "dec-a")
    p2 = build_plan(reg, broker, mandate, dict(full), "dec-b")
    reg.log_verdict("dec-a", "PASS", [], "deterministic", targets=full)
    reg.log_verdict("dec-b", "PASS", [], "deterministic", targets=full)
    execute_plan(reg, broker, p1, mandate)  # deploys the book

    reg.set_plan_state(p2.plan_id, "submitted")  # crashed before any fill
    p2_submitted = OrderPlan(p2.plan_id, p2.decision_id, p2.targets, p2.legs,
                             p2.pre_trade, state="submitted")
    with pytest.raises(MandateViolation, match="stale book"):
        execute_plan(reg, broker, p2_submitted, mandate)


def test_rotation_into_a_new_name_is_not_a_liquidation(reg_and_broker):
    """Aggregate gross reduction alone must NOT count as a liquidation: a plan
    that lowers total gross while RAISING a new position is a rotation. The old
    code waived the fully-invested rule for any gross reduction; a rotation must
    instead be held to it and rejected (not silently granted the waiver).
    """
    reg, broker = reg_and_broker
    mandate = replace(load_mandate(), max_turnover_per_rebalance=3.0)
    w = mandate.universe_whitelist
    full = {w[0]: 0.4, w[1]: 0.35, w[2]: 0.25}
    p1 = build_plan(reg, broker, mandate, full, "dec-full")
    reg.log_verdict("dec-full", "PASS", [], "deterministic", targets=full)
    execute_plan(reg, broker, p1, mandate)

    # Gross falls 1.0 -> 0.9, but w[3] grows 0 -> 0.3: a rotation, not de-risking.
    # Not a liquidation → fully-invested is enforced → the sub-1.0 book refuses.
    rot = {w[0]: 0.3, w[1]: 0.3, w[3]: 0.3}
    with pytest.raises(MandateViolation, match="fully-invested"):
        build_plan(reg, broker, mandate, rot, "dec-rot")


def test_genuine_liquidation_executes_under_halt(reg_and_broker):
    """A halted account must still permit a genuine de-risking to execute — the
    kill switch stops new risk, it must not trap the desk in its positions.
    """
    reg, broker = reg_and_broker
    mandate = replace(load_mandate(), max_turnover_per_rebalance=3.0)
    w = mandate.universe_whitelist
    full = {t: 1.0 / len(w) for t in w}
    p1 = build_plan(reg, broker, mandate, full, "dec-full")
    reg.log_verdict("dec-full", "PASS", [], "deterministic", targets=full)
    execute_plan(reg, broker, p1, mandate)

    reg.set_halt(True)
    # Halve every weight: gross falls, no position grows — a pure liquidation.
    reduced = {t: 0.5 / len(w) for t in w}
    p2 = build_plan(reg, broker, mandate, reduced, "dec-liq")
    assert p2.pre_trade["liquidating"] is True
    reg.log_verdict("dec-liq", "PASS", [], "deterministic", targets=reduced)
    result = execute_plan(reg, broker, p2, mandate)
    assert result["state"] == "reconciled"


def test_non_liquidation_refused_under_halt(reg_and_broker):
    """The other side of the halt gate: a plan that is NOT a genuine de-risking
    must be refused while the account is halted.
    """
    reg, broker = reg_and_broker
    mandate = replace(load_mandate(), max_turnover_per_rebalance=3.0)
    w = mandate.universe_whitelist
    full = {t: 1.0 / len(w) for t in w}
    p1 = build_plan(reg, broker, mandate, full, "dec-full")
    reg.log_verdict("dec-full", "PASS", [], "deterministic", targets=full)
    execute_plan(reg, broker, p1, mandate)

    reg.set_halt(True)
    # Keep gross == 1.0 by moving weight between two names (raising one, lowering
    # another): a non-liquidation move that must be refused under the halt.
    reshuffle = {t: 1.0 / len(w) for t in w}
    reshuffle[w[0]] = reshuffle[w[0]] + 0.02
    reshuffle[w[1]] = reshuffle[w[1]] - 0.02
    p2 = build_plan(reg, broker, mandate, reshuffle, "dec-reshuffle")
    reg.log_verdict("dec-reshuffle", "PASS", [], "deterministic", targets=reshuffle)
    with pytest.raises(MandateViolation, match="halted"):
        execute_plan(reg, broker, p2, mandate)


def test_simulated_positions_carry_unrealized_pl():
    reg = Registry(":memory:")
    broker = SimulatedPaperBroker(
        reg, price_provider=lambda tickers: {t: 110.0 for t in tickers},
        starting_cash=10_000.0)
    reg.apply_fill("ACWI", 10.0, 100.0, -1_000.0)  # bought at 100, marked 110
    state = broker.portfolio_state(["ACWI"])
    assert state["positions"]["ACWI"]["unrealized_pl"] == pytest.approx(100.0)


def test_simulated_broker_has_no_portfolio_history():
    reg = Registry(":memory:")
    broker = SimulatedPaperBroker(reg)
    assert not hasattr(broker, "portfolio_history")


def test_simulated_book_wins_over_discoverable_credentials(monkeypatch):
    """The regression this design exists to prevent.

    A discoverable `alpaca profile login` token must not silently route an
    operator who chose the simulated book to their real paper account.
    """
    from qlab.trader import broker as broker_mod
    from qlab.trader.alpaca_auth import AlpacaCredentials

    monkeypatch.setattr(
        broker_mod, "resolve_alpaca_credentials",
        lambda: AlpacaCredentials("oauth", None, None, "tok", "paper", "/x"))
    got = broker_mod.get_broker(Registry(":memory:"), offline=True,
                                book="simulated")
    assert got.name == "simulated_paper"


def _discoverable_oauth_profile(tmp_path, monkeypatch):
    """Lay out a real `alpaca profile login` session on disk, as the CLI does."""
    (tmp_path / "profiles").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.yaml").write_text(
        "default_profile: paper\noutput: json\n", encoding="utf-8")
    (tmp_path / "profiles" / "paper.yaml").write_text(
        "access_token: tok-on-disk\nscopes: account:write trading\n",
        encoding="utf-8")
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(tmp_path))


def test_book_none_infers_from_the_environment_only(tmp_path, monkeypatch):
    """A discoverable CLI login must not move a caller that chose no book.

    ``book=None`` is the historical default of every caller that predates the
    desk mode, and its signal has always been ALPACA_API_KEY/SECRET. Inferring
    the Alpaca book from a profile on disk instead would put autopilot on a real
    paper account the moment the operator logs in with the Alpaca CLI.
    """
    from qlab.trader import broker as broker_mod

    _discoverable_oauth_profile(tmp_path, monkeypatch)
    # The profile really is discoverable: the simulator below is a decision,
    # not a failure to find the credentials.
    assert broker_mod.resolve_alpaca_credentials() is not None

    got = broker_mod.get_broker(Registry(":memory:"), offline=True)
    assert got.name == "simulated_paper"


def test_the_alpaca_book_still_reaches_a_disk_profile(tmp_path, monkeypatch):
    """The other side of env-only inference: asking for Alpaca uses the login."""
    from qlab.trader import broker as broker_mod

    _discoverable_oauth_profile(tmp_path, monkeypatch)
    seen = {}

    class Recorder:
        name = "alpaca_paper"

        def __init__(self, _registry, credentials=None):
            seen["kind"] = credentials.kind
            seen["profile"] = credentials.profile_name

    monkeypatch.setattr(broker_mod, "AlpacaPaperBroker", Recorder)
    got = broker_mod.get_broker(Registry(":memory:"), book="alpaca")
    assert got.name == "alpaca_paper"
    assert (seen["kind"], seen["profile"]) == ("oauth", "paper")


@pytest.mark.parametrize("book", [None, "simulated"])
def test_a_partial_env_credential_pair_is_refused_for_every_book(monkeypatch, book):
    """The loud guard predates the explicit book and must still fire.

    Before ``book`` existed this refusal was unconditional. The simulated book
    needs no credential — but half a pair is a broken setup the operator has to
    see, not something the simulated lane steps over on the way past.
    """
    from qlab.trader import broker as broker_mod
    from qlab.trader.alpaca_auth import AlpacaAuthError

    monkeypatch.setenv("ALPACA_API_KEY", "PKONLY")
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    with pytest.raises(AlpacaAuthError, match="ALPACA_API_SECRET"):
        broker_mod.get_broker(Registry(":memory:"), offline=True, book=book)


def test_an_unknown_book_is_refused():
    """Garbage must not resolve to a venue by accident, in either direction."""
    from qlab.trader import broker as broker_mod

    with pytest.raises(ValueError, match="unknown book"):
        broker_mod.get_broker(Registry(":memory:"), book="alpaca_paper")


def test_alpaca_book_without_credentials_refuses_with_the_remedy(monkeypatch):
    from qlab.trader import broker as broker_mod

    monkeypatch.setattr(broker_mod, "resolve_alpaca_credentials", lambda: None)
    with pytest.raises(RuntimeError, match="alpaca profile login"):
        broker_mod.get_broker(Registry(":memory:"), book="alpaca")


def test_oauth_credentials_build_the_clients_with_a_token(monkeypatch):
    """OAuth must reach BOTH the trading and the market-data client."""
    from qlab.trader import broker as broker_mod
    from qlab.trader.alpaca_auth import AlpacaCredentials

    seen = {}

    class FakeTrading:
        def __init__(self, *args, **kwargs):
            seen["trading"] = kwargs

    class FakeData:
        def __init__(self, *args, **kwargs):
            seen["data"] = kwargs

    monkeypatch.setitem(
        __import__("sys").modules, "alpaca.trading.client",
        type("M", (), {"TradingClient": FakeTrading}))
    monkeypatch.setitem(
        __import__("sys").modules, "alpaca.data.historical",
        type("M", (), {"StockHistoricalDataClient": FakeData}))

    creds = AlpacaCredentials("oauth", None, None, "tok-123", "paper", "/x")
    broker_mod.AlpacaPaperBroker(Registry(":memory:"), credentials=creds)
    assert seen["trading"]["oauth_token"] == "tok-123"
    assert seen["trading"]["paper"] is True      # never configurable
    assert seen["data"]["oauth_token"] == "tok-123"
    assert "api_key" not in seen["trading"] or seen["trading"]["api_key"] is None
