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

from qlab.core.costs import DEFAULT_DAILY_VOL, cost_model
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


def _expected_cost(
    broker: Broker,
    mandate: Mandate,
    legs: list[OrderLeg],
) -> dict:
    """Build a display-only realistic cost estimate for the actual plan legs."""
    totals = {
        "commission": 0.0,
        "half_spread": 0.0,
        "impact": 0.0,
        "minimum_adjustment": 0.0,
        "total": 0.0,
    }
    prices = broker.prices([leg.ticker for leg in legs]) if legs else {}
    leg_costs = []
    for leg in legs:
        price = float(prices[leg.ticker])
        adv_notional = mandate.costs.adv_for(leg.ticker)
        breakdown = cost_model(
            leg.notional,
            price,
            adv_notional,
            DEFAULT_DAILY_VOL,
            spread_bps=mandate.costs.spread_bps,
            commission_bps=mandate.costs.commission_bps,
            impact_k=mandate.costs.impact_k,
        )
        scalar_breakdown = {
            component: float(value)
            for component, value in breakdown.items()
        }
        for component in totals:
            totals[component] += scalar_breakdown[component]
        leg_costs.append({
            "ticker": leg.ticker,
            "side": leg.side,
            "notional": leg.notional,
            "price": price,
            "adv_notional": adv_notional,
            "daily_vol": DEFAULT_DAILY_VOL,
            **scalar_breakdown,
        })
    return {
        "currency": mandate.base_currency,
        "daily_vol_assumption": DEFAULT_DAILY_VOL,
        **totals,
        "legs": leg_costs,
    }


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
    # Every held ticker must be part of the plan: a position omitted from the
    # targets is a position to SELL (target weight 0), not one to leave on the
    # book. Iterating only the target tickers would silently retain it and push
    # gross exposure past the mandate.
    state = broker.portfolio_state(list(targets))
    equity = float(state["equity"])
    current_w = state.get("weights", {})
    held = {t for t, w in current_w.items() if abs(float(w)) > 1e-6}
    tickers = sorted(set(targets) | held)

    current_gross = sum(abs(float(w)) for w in current_w.values())
    target_gross = sum(abs(float(w)) for w in targets.values())
    # A "liquidation" is PURE de-risking: overall gross strictly falls AND no
    # single position grows in magnitude. A plan that rotates into a new name —
    # raising one weight while lowering aggregate gross — is NOT a liquidation
    # and stays subject to the kill switch and the fully-invested rule, so it
    # cannot use the liquidation waiver to open a position under a halt.
    no_position_increased = all(
        abs(float(targets.get(t, 0.0))) <= abs(float(current_w.get(t, 0.0))) + 1e-6
        for t in set(current_w) | set(targets))
    is_liquidating = (target_gross < current_gross - 1e-6) and no_position_increased

    # kill-switch — a breached mandate refuses everything EXCEPT de-risking.
    if mandate.drawdown_breached(equity, float(state.get("high_water_mark", equity))):
        # Scoped to the venue that breached: halting every book because one did
        # is the same cross-book leak the account partitioning exists to stop.
        registry.set_halt(True, book=broker.name)
        if not is_liquidating:
            raise MandateViolation(
                f"trailing-drawdown kill-switch fired (>{mandate.trailing_drawdown_pct:.0%}); "
                "trading halted, only liquidation permitted, human paged")

    # An all-cash book being deployed for the first time is not a "rebalance";
    # its turnover is necessarily 1.0, so the rebalance turnover cap is exempt.
    # Gross (not net) exposure: an offsetting book is not all-cash.
    is_initial_deployment = current_gross < 0.01

    plan_id = _plan_id(decision_id, targets)
    stored = registry.get_plan(plan_id)
    if stored is not None:
        stored_legs = [OrderLeg(**leg) for leg in (stored.get("legs") or [])]
        return OrderPlan(
            plan_id=stored["plan_id"],
            decision_id=stored["decision_id"],
            targets=stored["targets"],
            legs=stored_legs,
            pre_trade=stored["pre_trade"],
            state=stored["state"],
        )
    legs: list[OrderLeg] = []
    turnover = 0.0
    for t in tickers:
        tgt_w = float(targets.get(t, 0.0))
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
        "liquidating": is_liquidating,
        "current_weights": current_w, "target_weights": targets,
    }

    # --- mandate checks (any failure aborts before the plan is 'checked') ---
    mandate.check_targets(targets, allow_liquidation=is_liquidating)
    if not is_initial_deployment:
        mandate.check_turnover(turnover)
    # The daily order cap is cumulative across plans, not per-plan: count what
    # today already booked so two plans cannot together exceed it.
    from datetime import datetime, timezone
    already_today = registry.count_orders_on(
        datetime.now(timezone.utc).isoformat())
    mandate.check_order_count(already_today + len(legs))
    pre_trade["expected_cost"] = _expected_cost(broker, mandate, legs)
    pre_trade["mandate_ok"] = True

    plan = OrderPlan(plan_id, decision_id, targets, legs, pre_trade, state="checked")
    registry.create_plan(plan_id, decision_id, targets, pre_trade,
                         legs=[vars(l) for l in legs])
    registry.set_plan_state(plan_id, "checked")
    registry.record_event("plan_checked", {"plan_id": plan_id, "turnover": turnover})
    return plan


def execute_plan(registry: Registry, broker: Broker, plan: OrderPlan,
                 mandate: Mandate | None = None) -> dict:
    """Execute a *checked* (or resumed *submitted*) plan. Idempotent per leg;
    advances the state machine.

    A process that dies mid-execution persists the plan as ``submitted``
    (``execute_plan`` sets that state before iterating legs, below). Resuming
    such a plan is safe: the referee-PASS + targets-hash gate still applies,
    and each leg replays through its stable ``client_order_id`` if it was
    already filled, so re-running never double-books a leg.
    """
    if mandate is None:
        from qlab.trader.mandate import load_mandate
        mandate = load_mandate()
    if plan.state not in ("checked", "submitted"):
        raise MandateViolation(
            f"plan {plan.plan_id} is {plan.state!r}, not 'checked' or 'submitted'")
    # Registry truth, not object memory: a plan the cost gate refused stays
    # refused even if the caller still holds a 'checked' OrderPlan and a
    # later PASS verdict exists. This is the single execution choke point.
    stored = registry.get_plan(plan.plan_id)
    if stored is None:
        raise MandateViolation(
            f"plan {plan.plan_id} is not persisted; re-propose")
    if stored["state"] not in ("checked", "submitted"):
        raise MandateViolation(
            f"plan {plan.plan_id} is {stored['state']!r} in the registry "
            "(terminal); re-propose")
    v = registry.get_verdict(plan.decision_id)
    if not v or v.get("verdict") != "PASS":
        raise MandateViolation(
            f"no referee PASS for decision {plan.decision_id!r}; "
            "log_verdict must record PASS before execution")
    if v.get("targets_hash") != targets_hash(plan.targets):
        raise MandateViolation(
            f"referee PASS for decision {plan.decision_id!r} does not cover these "
            "targets; re-review required")

    # Execute the PERSISTED plan, never the caller's object: a forged OrderPlan
    # carrying plan_id P but a different decision, targets, or legs cannot slip
    # past the storage-backed checks above and then book its own legs.
    stored_decision = stored["decision_id"]
    stored_targets = stored["targets"]
    stored_legs = [OrderLeg(**leg) for leg in (stored.get("legs") or [])]
    if (plan.decision_id != stored_decision
            or targets_hash(plan.targets) != targets_hash(stored_targets)
            or {l.client_order_id for l in plan.legs}
            != {l.client_order_id for l in stored_legs}):
        raise MandateViolation(
            f"supplied plan {plan.plan_id} does not match the persisted plan; "
            "re-propose")

    # Freshly-checked plans are revalidated against the CURRENT book: a plan
    # built against a stale snapshot (e.g. a second plan proposed while the
    # first was all-cash) must not deploy on top of positions that now exist.
    # A plan that has ALREADY begun executing legitimately sees its own fills,
    # so the stale-book refusal applies only to a plan that has never run.
    already_started = any(
        (registry.get_order(leg.client_order_id) or {}).get("state") == "filled"
        for leg in stored_legs)
    # A plan with NO filled legs must revalidate against the book regardless of
    # its persisted state: a plan that reached 'submitted' and crashed before
    # any fill would otherwise skip this and deploy on top of positions a
    # sibling plan created.
    if not already_started:
        assumed = (stored.get("pre_trade") or {}).get("current_weights", {})
        live = broker.portfolio_state(
            sorted(set(assumed) | set(stored_targets)))
        live_w = live.get("weights", {})
        # Recompute liquidation status against the LIVE book, never trusting the
        # plan's stored flag: pure de-risking (gross strictly falls, no position
        # grows) is the only thing permitted to execute under a halt.
        target_gross = sum(abs(float(w)) for w in stored_targets.values())
        current_gross = sum(abs(float(w)) for w in live_w.values())
        no_position_increased = all(
            abs(float(stored_targets.get(t, 0.0)))
            <= abs(float(live_w.get(t, 0.0))) + 1e-6
            for t in set(live_w) | set(stored_targets))
        is_liquidating = (target_gross < current_gross - 1e-6) and no_position_increased
        # A halted account (kill switch already latched) executes ONLY a genuine
        # liquidation; a fresh non-liquidation plan is refused even if its own
        # drawdown check would pass.
        #
        # Named by book, because the halt is per-book and both latches write
        # `broker.name`. Defaulted, this read landed on DEFAULT_BOOK: the switch
        # fired on the Alpaca book, latched `alpaca_paper`, and the next plan's
        # check read `simulated_paper`, found it clear, and executed. It leaked
        # the other way too — the simulated book's halt stopped a venue nobody
        # had halted.
        if registry.get_account(broker.name).get("halted") and not is_liquidating:
            raise MandateViolation(
                f"the {broker.name} book is halted; only liquidation may execute")
        drift = max((abs(float(live_w.get(t, 0.0)) - float(assumed.get(t, 0.0)))
                     for t in set(assumed) | set(live_w)), default=0.0)
        if drift > 1e-3:
            raise MandateViolation(
                f"plan {plan.plan_id} was checked against a stale book "
                f"(max weight drift {drift:.4f}); re-propose against the "
                "current positions")
        if mandate.drawdown_breached(
                float(live["equity"]),
                float(live.get("high_water_mark", live["equity"]))):
            if not is_liquidating:
                registry.set_halt(True, book=broker.name)
                raise MandateViolation(
                    "trailing-drawdown kill-switch fired since this plan was "
                    "checked; only liquidation may execute")

    registry.set_plan_state(plan.plan_id, "submitted")
    fills = []
    for leg in stored_legs:
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
