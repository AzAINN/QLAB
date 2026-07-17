"""quant-trader MCP server — the execution gateway.

Exposed tools (research-plan §8.1) — and *only* these:

    get_portfolio_state()                    broker-truth positions, cash, drift
    reconcile()                              ledger vs broker; must be clean first
    propose_rebalance(targets, decision_id)  plan_id + pre-trade report (two-phase)
    execute_plan(plan_id)                    only checked plans; idempotent
    halt() / resume() / risk_report()

There is deliberately **no raw order tool**. The mandate is enforced in
deterministic code inside ``propose_rebalance``/``execute_plan``, so it cannot be
bypassed by a prompt. This is why we do not mount a broker's generic
``place_order`` MCP for the autonomous loop. Paper mode is hard-coded.

Run standalone:  ``python -m qlab.mcp.quant_trader``  (needs ``pip install qlab[mcp]``)
"""

from __future__ import annotations

import os

from qlab.mcp.guardrails import require_fastmcp
from qlab.trader.broker import get_broker
from qlab.trader.mandate import MandateViolation, load_mandate
from qlab.trader.plan import OrderLeg, OrderPlan, build_plan, execute_plan
from qlab.state.registry import Registry


class TraderState:
    def __init__(self, registry: Registry | None = None, offline: bool = False,
                 seed: int = 7):
        self.registry = registry or Registry()
        self.mandate = load_mandate()
        self.offline = offline
        self.seed = seed
        self.broker = get_broker(self.registry, offline=offline,
                                 starting_cash=self.mandate.paper_capital,
                                 seed=seed, universe=self.mandate.universe_whitelist)
        self.plans: dict[str, OrderPlan] = {}

    @property
    def tickers(self) -> list[str]:
        return self.mandate.universe_whitelist


def build_server(state: TraderState | None = None):
    FastMCP = require_fastmcp()
    st = state or TraderState(offline=os.environ.get("QLAB_OFFLINE") == "1")
    app = FastMCP("quant-trader")

    @app.tool(name="get_portfolio_state")
    def get_portfolio_state() -> dict:
        """Broker-truth positions, cash, equity, weights, and drift vs last target."""
        s = st.broker.portfolio_state(st.tickers)
        last = st.registry.recent_decisions(limit=1)
        targets = last[0].get("choice", {}).get("targets", {}) if last else {}
        drift = {t: round(s["weights"].get(t, 0.0) - targets.get(t, 0.0), 4)
                 for t in set(list(s["weights"]) + list(targets))}
        return {**s, "target_weights": targets, "drift": drift, "broker": st.broker.name}

    @app.tool(name="reconcile")
    def reconcile() -> dict:
        """Diff the registry ledger against broker truth. Must be clean before trading."""
        broker_pos = st.broker.portfolio_state(st.tickers)["positions"]
        ledger_pos = st.registry.get_positions()
        diffs = {}
        for t in set(list(broker_pos) + list(ledger_pos)):
            bq = broker_pos.get(t, {}).get("qty", 0.0)
            lq = ledger_pos.get(t, {}).get("qty", 0.0)
            if abs(bq - lq) > 1e-6:
                diffs[t] = {"broker_qty": bq, "ledger_qty": lq}
        return {"clean": not diffs, "diffs": diffs}

    @app.tool(name="propose_rebalance")
    def propose_rebalance(targets: dict, decision_id: str = "adhoc") -> dict:
        """Phase 1: validate targets against the mandate; return plan_id + report."""
        try:
            plan = build_plan(st.registry, st.broker, st.mandate, targets, decision_id)
        except MandateViolation as exc:
            return {"accepted": False, "mandate_violation": str(exc)}
        st.plans[plan.plan_id] = plan
        return {"accepted": True, "plan_id": plan.plan_id, "state": plan.state,
                "pre_trade": plan.pre_trade, "n_legs": len(plan.legs)}

    @app.tool(name="execute_plan")
    def execute_plan_tool(plan_id: str) -> dict:
        """Phase 2: execute a checked plan. Idempotent; refuses anything else."""
        plan = st.plans.get(plan_id)
        if plan is None:
            stored = st.registry.get_plan(plan_id)
            if not stored:
                return {"executed": False, "error": f"unknown plan_id {plan_id!r}"}
            plan = OrderPlan(plan_id, stored["decision_id"], stored["targets"],
                             [], stored["pre_trade"], state=stored["state"])
        try:
            result = execute_plan(st.registry, st.broker, plan)
            return {"executed": True, **result}
        except MandateViolation as exc:
            return {"executed": False, "mandate_violation": str(exc)}

    @app.tool(name="halt")
    def halt() -> dict:
        """Halt trading (kill-switch). Only liquidation permitted while halted."""
        st.registry.set_halt(True)
        st.registry.record_event("halt", {"by": "tool"})
        return {"halted": True}

    @app.tool(name="resume")
    def resume() -> dict:
        st.registry.set_halt(False)
        st.registry.record_event("resume", {"by": "tool"})
        return {"halted": False}

    @app.tool(name="risk_report")
    def risk_report() -> dict:
        """Equity, drawdown vs high-water, cap headroom, kill-switch distance."""
        s = st.broker.portfolio_state(st.tickers)
        hwm = s.get("high_water_mark", s["equity"])
        drawdown = 1.0 - s["equity"] / hwm if hwm > 0 else 0.0
        cap = st.mandate.max_weight_per_asset
        breaches = {t: w for t, w in s["weights"].items() if w > cap + 1e-6}
        return {
            "equity": s["equity"], "high_water_mark": hwm,
            "drawdown": round(drawdown, 4),
            "kill_switch_at": st.mandate.trailing_drawdown_pct,
            "kill_switch_distance": round(st.mandate.trailing_drawdown_pct - drawdown, 4),
            "cap_breaches": breaches, "halted": s["halted"],
        }

    return app


def main() -> None:  # pragma: no cover
    build_server().run()


if __name__ == "__main__":  # pragma: no cover
    main()
