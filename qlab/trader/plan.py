"""Order plans — a two-phase, idempotent, resumable state machine.

An order plan is a registry object with states
``proposed → checked → submitted → filled → reconciled`` (research-plan §8.1).
Two-phase (``propose`` then ``execute``) means only *checked* plans trade;
``client_order_id = hash(plan_id, leg)`` makes execution idempotent, so a session
that dies mid-rebalance resumes instead of double-ordering.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from qlab.trader.broker import Broker
from qlab.trader.mandate import Mandate, MandateViolation
from qlab.state.registry import Registry, targets_hash

_MIN_LEG_NOTIONAL = 1.0  # ignore dust legs


@dataclass
class OrderLeg:
    ticker: str
    side: str            # "buy" | "sell"
    notional: float
    client_order_id: str


@dataclass
class OrderPlan:
    plan_id: str
    decision_id: str
    targets: dict[str, float]
    legs: list[OrderLeg] = field(default_factory=list)
    pre_trade: dict = field(default_factory=dict)
    state: str = "proposed"

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id, "decision_id": self.decision_id,
            "state": self.state, "targets": self.targets,
            "pre_trade": self.pre_trade,
            "legs": [vars(l) for l in self.legs],
        }


def _plan_id(decision_id: str, targets: dict[str, float]) -> str:
    key = decision_id + "|" + ",".join(f"{k}:{v:.6f}" for k, v in sorted(targets.items()))
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _client_order_id(plan_id: str, ticker: str) -> str:
    return hashlib.sha256(f"{plan_id}|{ticker}".encode()).hexdigest()[:16]


def build_plan(
    registry: Registry,
    broker: Broker,
    mandate: Mandate,
    targets: dict[str, float],
    decision_id: str,
    *,
    cost_bps: float = 5.0,
) -> OrderPlan:
    """Validate targets against the mandate and build a checked order plan.

    Raises :class:`MandateViolation` if any hard limit is breached — the plan
    never reaches ``checked`` and therefore can never be executed.
    """
    tickers = list(targets)
    state = broker.portfolio_state(tickers)
    equity = float(state["equity"])
    current_w = state.get("weights", {})

    # kill-switch first — a breached mandate refuses everything non-liquidating
    if mandate.drawdown_breached(equity, float(state.get("high_water_mark", equity))):
        registry.set_halt(True)
        raise MandateViolation(
            f"trailing-drawdown kill-switch fired (>{mandate.trailing_drawdown_pct:.0%}); "
            "trading halted, human paged")

    # An all-cash book being deployed for the first time is not a "rebalance";
    # its turnover is necessarily 1.0, so the rebalance turnover cap is exempt.
    is_initial_deployment = sum(current_w.values()) < 0.01

    plan_id = _plan_id(decision_id, targets)
    legs: list[OrderLeg] = []
    turnover = 0.0
    for t in tickers:
        tgt_w = float(targets[t])
        cur_w = float(current_w.get(t, 0.0))
        turnover += abs(tgt_w - cur_w)
        delta_notional = (tgt_w - cur_w) * equity
        if abs(delta_notional) < _MIN_LEG_NOTIONAL:
            continue
        side = "buy" if delta_notional > 0 else "sell"
        legs.append(OrderLeg(t, side, abs(delta_notional),
                             _client_order_id(plan_id, t)))

    est_cost = turnover * equity * cost_bps / 1e4
    pre_trade = {
        "equity": equity, "turnover": turnover, "est_cost": est_cost,
        "n_legs": len(legs), "order_type": mandate.order_type,
        "initial_deployment": is_initial_deployment,
        "current_weights": current_w, "target_weights": targets,
    }

    # --- mandate checks (any failure aborts before the plan is 'checked') ---
    mandate.check_targets(targets)
    if not is_initial_deployment:
        mandate.check_turnover(turnover)
    mandate.check_order_count(len(legs))
    pre_trade["mandate_ok"] = True

    plan = OrderPlan(plan_id, decision_id, targets, legs, pre_trade, state="checked")
    registry.create_plan(plan_id, decision_id, targets, pre_trade)
    registry.set_plan_state(plan_id, "checked")
    registry.record_event("plan_checked", {"plan_id": plan_id, "turnover": turnover})
    return plan


def execute_plan(registry: Registry, broker: Broker, plan: OrderPlan) -> dict:
    """Execute a *checked* plan. Idempotent per leg; advances the state machine."""
    if plan.state != "checked":
        raise MandateViolation(f"plan {plan.plan_id} is {plan.state!r}, not 'checked'")
    if registry.get_account().get("halted"):
        raise MandateViolation("account is halted; only liquidation is permitted")
    v = registry.get_verdict(plan.decision_id)
    if not v or v.get("verdict") != "PASS":
        raise MandateViolation(
            f"no referee PASS for decision {plan.decision_id!r}; "
            "log_verdict must record PASS before execution")
    if v.get("targets_hash") != targets_hash(plan.targets):
        raise MandateViolation(
            f"referee PASS for decision {plan.decision_id!r} does not cover these "
            "targets; re-review required")

    registry.set_plan_state(plan.plan_id, "submitted")
    fills = []
    for leg in plan.legs:
        existing = registry.get_order(leg.client_order_id)
        if existing and existing["state"] == "filled":
            fills.append({
                "client_order_id": leg.client_order_id,
                "ticker": leg.ticker,
                "side": leg.side,
                "notional": leg.notional,
                "state": "filled",
                "replayed": True,
            })
            continue
        # The simulator books cash/positions through this same registry, so the
        # ledger row, fill, and terminal state commit as one unit. External
        # paper brokers provide the corresponding guarantee through the stable
        # client_order_id.
        with registry.transaction():
            registry.add_order(
                leg.client_order_id,
                plan.plan_id,
                leg.ticker,
                leg.side,
                leg.notional,
                state="submitted",
            )
            fill = broker.submit_notional(
                leg.client_order_id,
                leg.ticker,
                leg.side,
                leg.notional,
            )
            registry.update_order_state(
                leg.client_order_id,
                fill.get("state", "filled"),
            )
        fills.append(fill)
    registry.set_plan_state(plan.plan_id, "filled")
    registry.set_plan_state(plan.plan_id, "reconciled")
    registry.record_event("plan_executed",
                          {"plan_id": plan.plan_id,
                           "n_fills": sum(not f.get("replayed", False) for f in fills),
                           "n_replayed": sum(bool(f.get("replayed")) for f in fills)})
    return {"plan_id": plan.plan_id, "state": "reconciled", "fills": fills}
