"""The referee gate, as code. Nothing trades without a PASS (research-plan §3).

The deterministic referee re-verifies the mandate-critical facts *independently*
of build_plan (defense in depth) and is the autopilot's gatekeeper. The LLM
referee agent adds qualitative review in interactive sessions and submits its
verdict through the same registry.log_verdict tool - one gate, two reviewers.
"""
from __future__ import annotations

from datetime import date

import numpy as np

from qlab.trader.mandate import Mandate


def cost_gate(pre_trade: dict, equity: float,
              current_weights: dict[str, float],
              targets: dict[str, float], mandate: Mandate) -> list[str]:
    """The net-alpha gate, in deterministic code: reasons why costs refuse a plan.

    qlab forecasts no returns, so "expected alpha" is replaced by a mandated,
    documented benefit assumption: closing drift toward the reviewed policy is
    worth ``costs.rebalance_benefit_bps`` per unit of traded notional, taken
    with the ``costs.live_haircut`` backtest-to-live discount. A plan passes
    only when that haircut benefit exceeds ``costs.safety_multiplier`` times
    its expected cost, and its cost stays under an absolute equity cap. An
    all-cash initial deployment is not a rebalance and is exempt, mirroring
    ``build_plan``'s turnover exemption.
    """
    reasons: list[str] = []
    if sum(current_weights.values()) < 0.01:
        return reasons  # initial deployment: nothing is being "rebalanced"
    cost_total = float((pre_trade.get("expected_cost") or {}).get("total", 0.0))
    if equity <= 0:
        return ["cost gate requires positive equity"]
    costs = mandate.costs
    cap = equity * costs.max_cost_bps_of_equity / 1e4
    if cost_total > cap:
        reasons.append(
            f"expected cost {cost_total:.2f} exceeds the absolute cap "
            f"{cap:.2f} ({costs.max_cost_bps_of_equity:.0f} bps of equity)")
    tickers = set(current_weights) | set(targets)
    drift_closed = 0.5 * sum(
        abs(float(targets.get(t, 0.0)) - float(current_weights.get(t, 0.0)))
        for t in tickers) * equity
    benefit = drift_closed * costs.rebalance_benefit_bps / 1e4 * costs.live_haircut
    hurdle = cost_total * costs.safety_multiplier
    if benefit <= hurdle:
        reasons.append(
            f"net-alpha gate: haircut benefit {benefit:.2f} does not clear "
            f"{costs.safety_multiplier:.1f}x expected cost {cost_total:.2f} "
            f"(drift closed {drift_closed:.2f}, benefit assumption "
            f"{costs.rebalance_benefit_bps:.0f} bps, haircut "
            f"{costs.live_haircut:.2f})")
    return reasons


def deterministic_referee(targets: dict[str, float], mandate: Mandate,
                          as_of: date, moments_summary: dict | None = None,
                          ) -> tuple[str, list[str]]:
    reasons: list[str] = []
    vals = np.array(list(targets.values()), dtype=float)
    if not np.all(np.isfinite(vals)):
        reasons.append("non-finite weight")
    for t in targets:
        if t not in mandate.universe_whitelist:
            reasons.append(f"{t} outside universe whitelist")
    if mandate.long_only and np.any(vals < -1e-4):
        reasons.append("long-only violated")
    if mandate.fully_invested and abs(vals.sum() - 1.0) > 1e-2:
        reasons.append(f"budget violated: sum={vals.sum():.4f}")
    over = {t: v for t, v in targets.items() if v > mandate.max_weight_per_asset + 1e-4}
    if over:
        reasons.append(f"per-asset cap breach: {over}")
    if isinstance(as_of, date) and as_of > date.today():
        reasons.append("look-ahead as_of")
    if moments_summary and moments_summary.get("condition_number", 0) > 1e8:
        reasons.append("ill-conditioned covariance")
    return ("PASS" if not reasons else "FAIL"), reasons
