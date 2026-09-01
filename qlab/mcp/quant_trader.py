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

        Execution is NOT agent-reachable. ``human_confirmed=True`` is a boolean
        in the tool arguments — self-attestation an autonomous agent can forge —
        so it never suffices on its own (authority-matrix invariant #14). The
        headless server books a trade only when the operator authorized THIS
        process out of band by setting ``QLAB_HEADLESS_EXECUTE=1``, which an
        agent connected over MCP cannot set. Per-plan persisted approvals that
        bind a specific plan to a specific human replace this coarse gate in a
        later phase; until then a bare confirmation cannot execute.

        A fresh session (``st.plans`` empty after a restart) rebuilds the plan
        from the registry-persisted row, including its real order legs -- a
        legacy row with no persisted legs is refused rather than silently
        "executed" with zero legs.
        """
        operator_authorized = os.environ.get("QLAB_HEADLESS_EXECUTE") == "1"
        if human_confirmed is not True or not operator_authorized:
            return {"executed": False,
                    "error": "execution is not agent-reachable: a bare "
                             "human_confirmed flag cannot book a paper trade; "
                             "the operator must authorize this process out of "
                             "band (QLAB_HEADLESS_EXECUTE=1)"}
        plan = st.plans.get(plan_id)
        stored = st.registry.get_plan(plan_id)
        if plan is None:
            if not stored:
                return {"executed": False, "error": f"unknown plan_id {plan_id!r}"}
            stored_legs = stored.get("legs") or []
            if not stored_legs:
                return {"executed": False,
                        "error": f"plan {plan_id!r} has no persisted legs; re-propose"}
            legs = [OrderLeg(**leg) for leg in stored_legs]
            plan = OrderPlan(plan_id, stored["decision_id"], stored["targets"],
                             legs, stored["pre_trade"], state=stored["state"])

        # The book must reconcile with the ledger before ANY new booking: a
        # dirty book means broker truth and our ledger disagree, and trading on
        # top of that compounds the divergence.
        recon = _reconcile(st.registry, st.broker, st.tickers)
        if not recon.get("clean", False):
            return {"executed": False, "blocked_by": "reconcile",
                    "diffs": recon.get("diffs", {})}

        # The net-alpha cost gate runs ONLY for a fresh, not-yet-started plan.
        # A plan that already began executing (some legs filled) or is terminal
        # must delegate straight to execute_plan for an idempotent replay or a
        # clean terminal refusal — re-gating it against the now-invested book
        # would spuriously refuse it and, worse, rewrite a terminal 'reconciled'
        # plan to 'refused'.
        stored_state = stored["state"] if stored else plan.state
        started = any(
            (st.registry.get_order(leg.client_order_id) or {}).get("state") == "filled"
            for leg in plan.legs)
        if stored_state == "checked" and not started:
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
        """Halt trading (kill-switch). Only liquidation permitted while halted.

        Deliberately ungated: it only ever moves the desk to the safe side, and
        an agent that can stop trading faster than a human is a feature.

        Named by book: the halt is per-book, so a defaulted call halts
        DEFAULT_BOOK — which on an Alpaca desk is a book nobody is trading,
        leaving the traded one live. The book is returned because an operator
        told only "halted" cannot tell which desk stopped.
        """
        book = st.broker.name
        st.registry.set_halt(True, book=book)
        st.registry.record_event("halt", {"by": "tool", "book": book})
        return {"halted": True, "book": book}

    @app.tool(name="resume")
    def resume() -> dict:
        """Clear the kill switch. Gated exactly as ``execute_plan`` is.

        Re-arming tradability reopens the execution path, so it is an authority
        act and not a research call: a bare tool invocation an agent can make
        must not undo a halt. The operator authorizes THIS process out of band
        with ``QLAB_HEADLESS_EXECUTE=1``, which an agent connected over MCP
        cannot set.
        """
        book = st.broker.name
        if os.environ.get("QLAB_HEADLESS_EXECUTE") != "1":
            # The real state, not a hardcoded True: the refusal must describe
            # the switch as it stands, or a caller learns the wrong fact — and
            # the switch it must describe is this desk's book, not DEFAULT_BOOK,
            # which on an Alpaca desk is a row nobody trades against.
            return {"halted": bool(st.registry.get_account(book).get("halted")),
                    "book": book,
                    "error": "clearing the kill switch is not agent-reachable: "
                             "the operator must authorize this process out of "
                             "band (QLAB_HEADLESS_EXECUTE=1)"}
        st.registry.set_halt(False, book=book)
        st.registry.record_event("resume", {"by": "tool", "book": book})
        return {"halted": False, "book": book}

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
