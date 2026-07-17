"""The autopilot loop — one cold-boot iteration and the daily-ops heartbeat.

Two session types, mirroring research-plan §8.2:

* :func:`run_once` — a **rebalance session**: analyze (moments + regime) → solve
  the champion policy under the mandate → log the decision → propose → execute →
  write a memo. Runs the identical pipeline the interactive orchestrator drives.
* :func:`daily_ops` — a **small, cheap heartbeat**: reconcile, risk report, check
  drift/regime triggers. Its tool set does **not** include execution — it cannot
  trade. No trigger → log a heartbeat and exit.

Everything is booked against the registry, so a dying session resumes cleanly and
`--offline` guarantees the loop cannot be taken down by a data feed.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from qlab.arms import Arm, MomentsConfig, solve_arm
from qlab.core import data as market
from qlab.core.moments import detect_regime
from qlab.core.types import Decision
from qlab.experiment import compare_classical_quantum
from qlab.governance.referee import deterministic_referee
from qlab.solvers.base import Constraints
from qlab.trader.broker import get_broker
from qlab.trader.mandate import Mandate, MandateViolation, load_mandate
from qlab.trader.plan import build_plan, execute_plan
from qlab.trader.reconcile import reconcile
from qlab.state.registry import Registry

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SUMMARY_DIR = _REPO_ROOT / ".lab" / "summaries"


def constraints_from_mandate(m: Mandate) -> Constraints:
    return Constraints(long_only=m.long_only, budget=1.0, min_weight=m.min_weight_per_asset,
                       max_weight=m.max_weight_per_asset)


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
    run_qaoa: bool = False,
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
    regime = detect_regime(snap)

    # 2. solve the champion policy under the mandate --------------------------
    champion = Arm("A3", "mvsk", "classical_multistart",
                   {"skew_lambda": skew_lambda, "kurt_lambda": kurt_lambda})
    weights, diag = solve_arm(champion, snap, moments=MomentsConfig(lookback_days=lookback_days),
                              constraints=constraints)
    targets = {t: float(v) for t, v in zip(weights.tickers, weights.values)}
    compare = compare_classical_quantum(snap, MomentsConfig(lookback_days=lookback_days),
                                        constraints, run_qaoa=run_qaoa)

    # 3. log the decision (the judgment record) ------------------------------
    decision = Decision(
        as_of=_as_date(as_of), kind="regime",
        choice={"targets": targets, "regime": regime["regime"], "arm": champion.id},
        rationale=(f"Regime={regime['regime']} (signal={regime.get('signal', 0):.3f}). "
                   f"MVSK champion (λ_skew={skew_lambda}, λ_kurt={kurt_lambda}) solved "
                   f"under mandate caps; classical vs quantum compared on the same cov."),
        alternatives_considered=["min_variance", "hrp", "scenario_cvar"],
    )
    decision_id = reg.log_decision(decision)

    # 3b. referee gate + reconcile (must run before any trade; research-plan §3) --
    verdict, reasons = deterministic_referee(targets, mandate, _as_date(as_of),
                                             moments_summary=diag.get("moments"))
    reg.log_verdict(decision_id, verdict, reasons, source="deterministic", targets=targets)
    rec = reconcile(reg, broker, tickers)

    # 4. propose + execute (two-phase, mandate-checked) ----------------------
    state_before = broker.portfolio_state(tickers)
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
            if execute:
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
        "decision_id": decision_id, "classical_vs_quantum": compare,
        "equity_before": state_before["equity"], "equity_after": state_after["equity"],
        "trade": trade_result, "diagnostics": {k: v for k, v in diag.items()
                                               if k not in ("moments",)},
        "referee": {"verdict": verdict, "reasons": reasons}, "reconcile": rec,
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
    regime = detect_regime(snap)

    # drift vs the last logged decision's targets
    last = reg.recent_decisions(limit=1)
    triggers = []
    if last:
        targets = last[0].get("choice", {}).get("targets", {})
        for t, tgt in targets.items():
            cur = state["weights"].get(t, 0.0)
            if abs(cur - tgt) > mandate.drift_band_pct:
                triggers.append(f"drift:{t}({cur:.2f}vs{tgt:.2f})")
    if mandate.regime_triggered and regime["regime"] == "stress":
        triggers.append("regime:stress")

    dd_breached = mandate.drawdown_breached(state["equity"],
                                            state.get("high_water_mark", state["equity"]))
    if dd_breached:
        reg.set_halt(True)
        triggers.append("kill_switch:drawdown")

    result = {
        "kind": "daily_ops", "equity": state["equity"], "regime": regime["regime"],
        "triggers": triggers, "rebalance_recommended": bool(triggers),
        "halted": state["halted"] or dd_breached, "reconcile": rec,
    }
    reg.record_event("daily_ops", result)
    _write_summary(result, prefix="dailyops")
    return result


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
    if "classical_vs_quantum" in summary:
        c = summary["classical_vs_quantum"].get("classical", {})
        q = summary["classical_vs_quantum"].get("quantum", {})
        lines.append(f"Classical   : obj={c.get('objective_value'):.3e} "
                     f"({c.get('wall_clock_s', 0):.2f}s)")
        if "objective_value" in q:
            lines.append(f"Quantum     : obj={q.get('objective_value'):.3e} "
                         f"({q.get('wall_clock_s', 0):.2f}s)")
        elif q:
            lines.append("Quantum     : unavailable (install qlab[quantum])")
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
    _SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = _SUMMARY_DIR / f"{prefix}_{stamp}.txt"
    path.write_text(render_summary(summary), encoding="utf-8")
    return path


def _as_date(d: str) -> date:
    return date.fromisoformat(d) if isinstance(d, str) else d
