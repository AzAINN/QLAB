"""The autopilot loop — one cold-boot iteration and the daily-ops heartbeat.

Two session types, mirroring research-plan §8.2:

* :func:`run_once` — a **rebalance session**: analyze (moments + regime) → solve
  the configured operational policy under the mandate → log the decision → propose → execute →
  write a memo. Runs the identical pipeline the interactive orchestrator drives.
* :func:`daily_ops` — a **small, cheap heartbeat**: reconcile, risk report, check
  drift/regime triggers. Its tool set does **not** include execution — it cannot
  trade. No trigger → log a heartbeat and exit.

Everything is booked against the registry, so a dying session resumes cleanly and
`--offline` guarantees the loop cannot be taken down by a data feed.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import numpy as np

from qlab.autopilot.scheduler import evaluate_triggers
from qlab.arms import MomentsConfig, estimate, solve_arm
from qlab.algorithms import get_operational_policy
from qlab.core import data as market
from qlab.core.moments import detect_regime
from qlab.core.types import Decision
from qlab.governance.referee import cost_gate, deterministic_referee
from qlab.governance.reflection import resolve_pending
from qlab.paths import state_path
from qlab.signals.hard import detect_regime_robust
from qlab.solvers.base import Constraints
from qlab.trader.broker import get_broker
from qlab.trader.mandate import Mandate, MandateViolation, load_mandate
from qlab.trader.plan import build_plan, execute_plan
from qlab.trader.reconcile import reconcile
from qlab.state.registry import Registry


def constraints_from_mandate(m: Mandate) -> Constraints:
    return Constraints(long_only=m.long_only, budget=1.0, min_weight=m.min_weight_per_asset,
                       max_weight=m.max_weight_per_asset)


def _asset_vols_from_moments(snapshot, config: MomentsConfig) -> dict[str, float]:
    """Return daily standalone vols from the covariance used by the solver."""
    moment_set = estimate(snapshot, config, higher=False)
    variances = np.diag(moment_set.cov)
    if not np.all(np.isfinite(variances)) or np.any(variances < -1e-12):
        raise ValueError("solved covariance has invalid per-asset variances")
    return {
        ticker: float(vol)
        for ticker, vol in zip(
            moment_set.tickers,
            np.sqrt(np.clip(variances, 0.0, None)),
        )
    }


# ---------------------------------------------------------------------------
# Rebalance session
# ---------------------------------------------------------------------------
def run_once(
    *,
    registry: Registry | None = None,
    mandate: Mandate | None = None,
    offline: bool = False,
    execute: bool = True,
    skew_lambda: float = 0.5,
    kurt_lambda: float = 0.5,
    lookback_days: int = 756,
    seed: int = 7,
    as_of: str | None = None,
) -> dict:
    """Run one full pipeline iteration and (optionally) paper-trade the result."""
    reg = registry or Registry()
    mandate = mandate or load_mandate()
    tickers = mandate.universe_whitelist
    broker = get_broker(reg, offline=offline, starting_cash=mandate.paper_capital,
                        seed=seed, universe=tickers)
    constraints = constraints_from_mandate(mandate)
    as_of = as_of or date.today().isoformat()

    # 1. analyze --------------------------------------------------------------
    snap = market.snapshot(tickers, as_of, offline=offline, seed=seed)
    reflections_resolved = resolve_pending(reg, snap.prices)
    regime = detect_regime(snap)

    # 2. solve the configured operational policy under the mandate ------------
    policy = get_operational_policy(mandate.operational_policy)
    champion = policy.arm()
    moments_config = MomentsConfig(lookback_days=lookback_days)
    weights, diag = solve_arm(
        champion,
        snap,
        moments=moments_config,
        constraints=constraints,
    )
    targets = {t: float(v) for t, v in zip(weights.tickers, weights.values)}
    daily_asset_vols = _asset_vols_from_moments(snap, moments_config)
    diag["portfolio_moments"]["asset_daily_vols"] = daily_asset_vols
    annualized_asset_vols = {
        ticker: daily_vol * np.sqrt(252.0)
        for ticker, daily_vol in daily_asset_vols.items()
    }
    est_vol = float(diag["portfolio_moments"]["vol"]) * np.sqrt(252)

    alt_lookback = 252 if lookback_days != 252 else 504
    alternate_weights, _alternate_diag = solve_arm(
        champion,
        snap,
        moments=MomentsConfig(lookback_days=alt_lookback),
        constraints=constraints,
    )
    weight_divergence = float(
        np.abs(alternate_weights.as_array() - weights.as_array()).sum()
    )
    if weight_divergence > 0.10:
        challenger_assessment = (
            "Material—the window choice is driving the allocation and must be "
            "defended explicitly."
        )
    else:
        challenger_assessment = (
            "Immaterial—the allocation is robust to this alternate window."
        )
    challenger_view = (
        f"Challenger (window={alt_lookback}d versus {lookback_days}d): "
        f"weight divergence L1={weight_divergence:.3f}. "
        f"{challenger_assessment}"
    )
    # 3. log the decision (the judgment record) ------------------------------
    decision = Decision(
        as_of=_as_date(as_of), kind="regime",
        choice={"targets": targets, "regime": regime["regime"],
                "arm": champion.id, "est_vol": est_vol,
                "lookback_days": lookback_days},
        rationale=(f"Regime={regime['regime']} (signal={regime.get('signal', 0):.3f}). "
                   f"Configured operational policy={policy.id} ({policy.label}) solved "
                   f"under mandate caps. {policy.rationale}"),
        challenger_view=challenger_view,
        alternatives_considered=[
            "min_variance", "risk_parity", "scenario_cvar", "mvsk (research only)"
        ],
    )
    decision_id = reg.log_decision(decision)

    # 3b. referee gate + reconcile (must run before any trade; research-plan §3) --
    state_before = broker.portfolio_state(tickers)
    verdict, reasons = deterministic_referee(
        targets,
        mandate,
        _as_date(as_of),
        moments_summary=diag.get("moments"),
        portfolio_state=state_before,
        vols=annualized_asset_vols,
    )
    reg.log_verdict(decision_id, verdict, reasons, source="deterministic", targets=targets)
    rec = reconcile(reg, broker, tickers)

    # 4. propose + execute (two-phase, mandate-checked) ----------------------
    trade_result: dict = {"executed": False}
    if verdict != "PASS":
        trade_result = {"executed": False, "blocked_by": "referee", "reasons": reasons}
    elif not rec["clean"]:
        trade_result = {"executed": False, "blocked_by": "reconcile", "diffs": rec["diffs"]}
        reg.record_event("reconcile_dirty", rec)
    else:
        try:
            plan = build_plan(reg, broker, mandate, targets, decision_id,
                              cost_bps=5.0)
            # Net-alpha gate: with the plan's realistic cost known, a failed
            # gate marks the plan itself terminally refused — execute_plan
            # re-reads registry state, so no later PASS can revive it — and
            # logs the FAIL verdict for the audit trail.
            weights_before = state_before.get("weights", {})
            gate_reasons = cost_gate(
                plan.pre_trade, float(state_before["equity"]),
                sum(abs(float(w)) for w in weights_before.values()),
                len(state_before.get("positions", {})), mandate)
            if gate_reasons:
                reg.set_plan_state(plan.plan_id, "refused")
                reg.log_verdict(decision_id, "FAIL", gate_reasons,
                                source="deterministic", targets=targets)
                verdict, reasons = "FAIL", gate_reasons
                trade_result = {"executed": False, "blocked_by": "cost_gate",
                                "reasons": gate_reasons,
                                "expected_cost": plan.pre_trade.get("expected_cost")}
                reg.record_event("cost_gate_refusal", {
                    "decision_id": decision_id, "plan_id": plan.plan_id,
                    "reasons": gate_reasons})
            elif execute:
                trade_result = execute_plan(reg, broker, plan)
                trade_result["executed"] = True
            else:
                trade_result = {"executed": False, "plan": plan.to_dict(),
                                "note": "execute=False (dry run)"}
        except MandateViolation as exc:
            trade_result = {"executed": False, "mandate_violation": str(exc)}
            reg.record_event("mandate_violation", {"error": str(exc)})

    state_after = broker.portfolio_state(tickers)

    summary = {
        "as_of": as_of, "regime": regime, "targets": targets,
        "decision_id": decision_id, "algorithm_id": policy.algorithm_id,
        "operational_policy": policy.to_dict(),
        "equity_before": state_before["equity"], "equity_after": state_after["equity"],
        "trade": trade_result, "diagnostics": {k: v for k, v in diag.items()
                                               if k not in ("moments",)},
        "referee": {"verdict": verdict, "reasons": reasons}, "reconcile": rec,
        "challenger_view": challenger_view,
        "reflections_resolved": reflections_resolved,
        "broker": broker.name, "offline": offline,
    }
    _write_summary(summary)
    reg.record_event("autopilot_run_once", {"as_of": as_of,
                                            "executed": trade_result.get("executed")})
    return summary


# ---------------------------------------------------------------------------
# Daily-ops heartbeat (cannot trade)
# ---------------------------------------------------------------------------
def daily_ops(
    *,
    registry: Registry | None = None,
    mandate: Mandate | None = None,
    offline: bool = False,
    seed: int = 7,
) -> dict:
    """Reconcile + risk report + drift/regime trigger check. Never trades."""
    reg = registry or Registry()
    mandate = mandate or load_mandate()
    tickers = mandate.universe_whitelist
    broker = get_broker(reg, offline=offline, starting_cash=mandate.paper_capital,
                        seed=seed, universe=tickers)

    state = broker.portfolio_state(tickers)
    rec = reconcile(reg, broker, tickers)
    snap = market.snapshot(tickers, date.today().isoformat(), offline=offline, seed=seed)
    reflections_resolved = resolve_pending(reg, snap.prices)
    regime = detect_regime(snap)
    robust_history = _latest_robust_regime_history(reg)
    robust_regime = detect_regime_robust(snap, history=robust_history)
    robust_observation = robust_regime.observation or robust_regime.regime
    robust_history = [
        *robust_history,
        robust_observation,
    ]

    dd_breached = mandate.drawdown_breached(state["equity"],
                                            state.get("high_water_mark", state["equity"]))
    alerts = []
    if dd_breached:
        reg.set_halt(True)
        alerts.append("kill_switch:drawdown")

    today = date.today()
    targets = _latest_policy_targets(reg)
    triggers = evaluate_triggers({
        "as_of": today,
        "last_rebalance_date": _last_rebalance_date(reg),
        "mandate": mandate,
        "current_weights": state.get("weights", {}),
        "target_weights": targets,
        "robust_regime": robust_regime.regime,
    })

    recommendation = None
    proposals = []
    proposal_plan_ids = []
    for trigger in triggers:
        if trigger["kind"] == "regime":
            proposal_targets = dict(mandate.defensive_targets)
            moments_summary = None
        else:
            if recommendation is None:
                from qlab.experiment import recommend

                recommendation = recommend(
                    as_of=today.isoformat(),
                    universe=mandate.universe_tier,
                    constraints=constraints_from_mandate(mandate),
                    offline=offline,
                    seed=seed,
                    policy_id=mandate.operational_policy,
                )
            proposal_targets = {
                str(ticker): float(weight)
                for ticker, weight in recommendation["recommended_weights"].items()
            }
            diagnostics = recommendation.get("diagnostics", {})
            moments_summary = (
                diagnostics.get("moments")
                if isinstance(diagnostics, dict)
                else None
            )

        proposal = _build_trigger_proposal(
            reg,
            broker,
            mandate,
            trigger,
            proposal_targets,
            today,
            state,
            rec,
            robust_regime.regime,
            moments_summary=moments_summary,
        )
        proposals.append({
            "trigger_kind": trigger["kind"],
            **proposal,
        })
        if proposal.get("plan_id"):
            proposal_plan_ids.append(proposal["plan_id"])
        reg.record_event("autopilot_trigger", {
            "kind": trigger["kind"],
            "detail": trigger["detail"],
            "proposal": proposal,
        })

    result = {
        "kind": "daily_ops", "equity": state["equity"], "regime": regime["regime"],
        "robust_regime": robust_regime.regime,
        "robust_regime_observation": robust_observation,
        "robust_regime_history": robust_history,
        "triggers": triggers, "rebalance_recommended": bool(triggers),
        "proposals": proposals, "proposal_plan_ids": proposal_plan_ids,
        "alerts": alerts,
        "halted": state["halted"] or dd_breached, "reconcile": rec,
        "reflections_resolved": reflections_resolved,
    }
    reg.record_event("daily_ops", result)
    _write_summary(result, prefix="dailyops")
    return result


def _latest_robust_regime_history(registry: Registry) -> list[str]:
    """Recover raw observations persisted by the newest daily-ops heartbeat."""
    permitted = {"calm", "normal", "stress", "uncertain"}
    events = registry._rows(
        "SELECT * FROM events WHERE kind=? "
        "ORDER BY ts DESC, event_id DESC LIMIT 1",
        ["daily_ops"],
    )
    if not events:
        return []
    payload = events[0].get("payload")
    if not isinstance(payload, dict):
        raise ValueError("daily_ops event payload must be a mapping")
    raw_history = payload.get("robust_regime_history")
    if raw_history is None:
        prior = payload.get("robust_regime")
        if prior is None:
            return []
        raw_history = [prior]
    if not isinstance(raw_history, list):
        raise ValueError("daily_ops robust_regime_history must be a list")
    history = [str(observation).strip().lower() for observation in raw_history]
    invalid = sorted(set(history) - permitted)
    if invalid:
        raise ValueError(
            f"daily_ops robust_regime_history has invalid states: {invalid}"
        )
    return history


def _latest_policy_targets(registry: Registry) -> dict[str, float]:
    """Return the newest non-autopilot target judgment for drift comparison."""
    for decision in registry.recent_decisions(limit=100):
        choice = decision.get("choice")
        if not isinstance(choice, dict) or choice.get("source") == "autopilot_trigger":
            continue
        targets = choice.get("targets")
        if not isinstance(targets, dict) or not targets:
            continue
        return {
            str(ticker): float(weight)
            for ticker, weight in targets.items()
        }
    return {}


def _last_rebalance_date(registry: Registry) -> date | None:
    """Read the latest actual execution date; checked proposals do not count."""
    for event in reversed(registry.read_events(limit=500)):
        if event.get("kind") != "plan_executed":
            continue
        timestamp = str(event.get("ts") or "")
        try:
            return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).date()
        except ValueError as exc:
            raise ValueError(
                f"plan_executed event has invalid timestamp {timestamp!r}"
            ) from exc
    return None


def _build_trigger_proposal(
    registry: Registry,
    broker,
    mandate: Mandate,
    trigger: dict,
    targets: dict[str, float],
    as_of: date,
    portfolio_state: dict,
    reconcile_result: dict,
    robust_regime: str,
    *,
    moments_summary: dict | None,
) -> dict:
    """Persist one referee-bound, cost-gated proposal without executing it."""
    detail = json.dumps(trigger["detail"], sort_keys=True, separators=(",", ":"))
    decision = Decision(
        as_of=as_of,
        kind="rebalance_gate",
        choice={
            "source": "autopilot_trigger",
            "trigger": trigger["kind"],
            "trigger_detail": trigger["detail"],
            "targets": targets,
            "regime": robust_regime,
            "operational_policy": mandate.operational_policy,
        },
        rationale=(
            f"Deterministic {trigger['kind']} trigger proposed a checked plan "
            f"without execution authority. detail={detail}"
        ),
        challenger_view=(
            "The trigger establishes proposal timing only; mandate, referee, "
            "cost gate, and explicit human confirmation still govern execution."
        ),
        alternatives_considered=[
            "hold current allocation",
            "explicit human-confirmed run-once",
        ],
    )
    decision_id = registry.log_decision(decision)
    verdict, reasons = deterministic_referee(
        targets,
        mandate,
        as_of,
        moments_summary=moments_summary,
        portfolio_state=portfolio_state,
    )
    registry.log_verdict(
        decision_id,
        verdict,
        reasons,
        source="deterministic",
        targets=targets,
    )
    if verdict != "PASS":
        return {
            "accepted": False,
            "decision_id": decision_id,
            "blocked_by": "referee",
            "reasons": reasons,
        }
    if not reconcile_result.get("clean"):
        return {
            "accepted": False,
            "decision_id": decision_id,
            "blocked_by": "reconcile",
            "reconcile": reconcile_result,
        }
    try:
        plan = build_plan(registry, broker, mandate, targets, decision_id)
    except MandateViolation as exc:
        registry.record_event("mandate_violation", {
            "decision_id": decision_id,
            "trigger": trigger["kind"],
            "error": str(exc),
        })
        return {
            "accepted": False,
            "decision_id": decision_id,
            "blocked_by": "mandate",
            "mandate_violation": str(exc),
        }

    weights = portfolio_state.get("weights", {})
    gate_reasons = cost_gate(
        plan.pre_trade,
        float(portfolio_state["equity"]),
        sum(abs(float(weight)) for weight in weights.values()),
        len(portfolio_state.get("positions", {})),
        mandate,
    )
    if gate_reasons:
        registry.set_plan_state(plan.plan_id, "refused")
        registry.log_verdict(
            decision_id,
            "FAIL",
            gate_reasons,
            source="deterministic",
            targets=targets,
        )
        registry.record_event("cost_gate_refusal", {
            "decision_id": decision_id,
            "plan_id": plan.plan_id,
            "reasons": gate_reasons,
        })
        return {
            "accepted": False,
            "decision_id": decision_id,
            "plan_id": plan.plan_id,
            "state": "refused",
            "blocked_by": "cost_gate",
            "reasons": gate_reasons,
            "expected_cost": plan.pre_trade.get("expected_cost"),
        }
    if plan.state != "checked":
        return {
            "accepted": False,
            "decision_id": decision_id,
            "plan_id": plan.plan_id,
            "state": plan.state,
            "blocked_by": "plan_state",
        }
    return {
        "accepted": True,
        "decision_id": decision_id,
        "plan_id": plan.plan_id,
        "state": "checked",
        "pre_trade": plan.pre_trade,
        "n_legs": len(plan.legs),
        "note": "proposal only; execution requires explicit human confirmation",
    }


# ---------------------------------------------------------------------------
# summaries
# ---------------------------------------------------------------------------
def render_summary(summary: dict) -> str:
    """Human-readable text memo (the polymarket-autopilot 'summarize' step)."""
    lines = ["=" * 64, f"qlab autopilot | {summary.get('as_of', '')}", "=" * 64]
    if "regime" in summary and isinstance(summary["regime"], dict):
        lines.append(f"Regime      : {summary['regime'].get('regime')}")
    if "targets" in summary:
        top = sorted(summary["targets"].items(), key=lambda x: -x[1])[:5]
        lines.append("Targets     : " + ", ".join(f"{t} {w:.0%}" for t, w in top))
    if summary.get("algorithm_id"):
        lines.append(f"Algorithm   : {summary['algorithm_id']}")
    if "equity_after" in summary:
        lines.append(f"Equity      : {summary.get('equity_before', 0):.2f} -> "
                     f"{summary['equity_after']:.2f}")
    trade = summary.get("trade", {})
    if trade.get("executed"):
        lines.append(f"Trade       : executed {len(trade.get('fills', []))} legs "
                     f"(plan {trade.get('plan_id')})")
    elif trade.get("mandate_violation"):
        lines.append(f"Trade       : BLOCKED - {trade['mandate_violation']}")
    else:
        lines.append("Trade       : none")
    if summary.get("triggers") is not None:
        lines.append(f"Triggers    : {summary['triggers'] or 'none (heartbeat)'}")
    lines.append("NOTE: paper capital only - never places a real order.")
    lines.append("=" * 64)
    return "\n".join(lines)


def _write_summary(summary: dict, prefix: str = "run") -> Path:
    summary_dir = state_path("summaries")
    summary_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = summary_dir / f"{prefix}_{stamp}.txt"
    path.write_text(render_summary(summary), encoding="utf-8")
    return path


def _as_date(d: str) -> date:
    return date.fromisoformat(d) if isinstance(d, str) else d
