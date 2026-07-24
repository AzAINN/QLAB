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

``python -m qlab.mcp.quant_trader`` delegates to the guarded combined server;
the retired two-process topology is not an executable path.
"""

from __future__ import annotations

import os

from qlab.mcp.guardrails import require_fastmcp
from qlab.trader.broker import get_broker
from qlab.trader.mandate import MandateViolation, load_mandate
from qlab.trader.plan import OrderLeg, OrderPlan, build_plan, execute_plan
from qlab.trader.reconcile import reconcile as _reconcile
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


def register_trader_tools(app, st: TraderState) -> None:
    """Mount every execution-gateway tool on ``app``, bound to state ``st``.

    Split out of ``build_server`` so the combined single-process server
    (``qlab.mcp.server``) can mount the lab and trader namespaces on one
    FastMCP app over one shared Registry (one DuckDB writer). There is still
    deliberately no raw order tool.
    """

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
        return _reconcile(st.registry, st.broker, st.tickers)

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
    def execute_plan_tool(plan_id: str, human_confirmed: bool = False) -> dict:
        """Phase 2: execute a checked (or resumed submitted) plan. Idempotent;
        refuses anything else.

        Execution is not agent-reachable: ``human_confirmed=True`` is required,
        the same affirmation the owner runtime demands, so an autonomous agent
        connected to the headless server cannot book a trade on its own. The
        net-alpha cost gate runs here too, matching the owner path.

        A fresh session (``st.plans`` empty after a restart) rebuilds the plan
        from the registry-persisted row, including its real order legs -- a
        legacy row with no persisted legs is refused rather than silently
        "executed" with zero legs.
        """
        if human_confirmed is not True:
            return {"executed": False,
                    "error": "human_confirmed=true is required to execute a "
                             "paper plan; execution is not agent-reachable"}
        plan = st.plans.get(plan_id)
        if plan is None:
            stored = st.registry.get_plan(plan_id)
            if not stored:
                return {"executed": False, "error": f"unknown plan_id {plan_id!r}"}
            stored_legs = stored.get("legs") or []
            if not stored_legs:
                return {"executed": False,
                        "error": f"plan {plan_id!r} has no persisted legs; re-propose"}
            legs = [OrderLeg(**leg) for leg in stored_legs]
            plan = OrderPlan(plan_id, stored["decision_id"], stored["targets"],
                             legs, stored["pre_trade"], state=stored["state"])
        # Net-alpha gate before execution, matching the owner path: a refused
        # plan is marked terminally refused so it cannot execute here either.
        from qlab.governance.referee import cost_gate

        book = st.broker.portfolio_state(st.tickers)
        weights = book.get("weights", {})
        gate_reasons = cost_gate(
            plan.pre_trade, float(book["equity"]),
            sum(abs(float(w)) for w in weights.values()),
            len(book.get("positions", {})), st.mandate)
        if gate_reasons:
            st.registry.set_plan_state(plan.plan_id, "refused")
            return {"executed": False, "blocked_by": "cost_gate",
                    "reasons": gate_reasons}
        try:
            result = execute_plan(st.registry, st.broker, plan, st.mandate)
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


def build_server(state: TraderState | None = None):
    """Build an isolated trader app for embedding and tests.

    The executable module path delegates to :mod:`qlab.mcp.server` so normal
    users always receive the owner-runtime guard and one-writer topology.
    """
    FastMCP = require_fastmcp()
    st = state or TraderState(offline=os.environ.get("QLAB_OFFLINE") == "1")
    app = FastMCP("quant-trader")
    register_trader_tools(app, st)
    return app


def main() -> None:  # pragma: no cover
    from qlab.mcp.server import main as combined_main

    combined_main()


if __name__ == "__main__":  # pragma: no cover
    main()
