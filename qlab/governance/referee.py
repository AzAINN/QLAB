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


def cost_gate(pre_trade: dict, equity: float, gross_exposure: float,
              n_positions: int, mandate: Mandate) -> list[str]:
    """The net-alpha gate, in deterministic code: reasons why costs refuse a plan.

    qlab forecasts no returns, so "expected alpha" is replaced by a mandated,
    documented benefit assumption: the notional actually traded (from the
    plan's own legs) is worth ``costs.rebalance_benefit_bps`` per unit, taken
    with the ``costs.live_haircut`` backtest-to-live discount. A plan passes
    only when that haircut benefit exceeds ``costs.safety_multiplier`` times
    its expected cost, and its cost stays under an absolute equity cap.

    Fail-closed by construction: malformed inputs (non-finite equity or cost,
    a plan without its cost decomposition, a book whose positions contradict
    its exposure) are refusals, never exemptions. The only exemption is a
    genuinely all-cash initial deployment — zero positions AND zero gross
    exposure — mirroring ``build_plan``'s turnover exemption; gross exposure
    is a sum of absolute weights, so offsetting long/short books never
    qualify.
    """
    costs = mandate.costs
    if not np.isfinite(equity) or equity <= 0:
        return ["cost gate requires finite positive equity"]
    if not np.isfinite(gross_exposure):
        return ["cost gate requires finite gross exposure"]
    if n_positions == 0 and gross_exposure < 0.01:
        return []  # true all-cash initial deployment
    if gross_exposure < 0.01:
        return ["inconsistent portfolio state: positions exist with ~zero "
                "gross exposure"]

    expected = pre_trade.get("expected_cost")
    if not isinstance(expected, dict) or "total" not in expected:
        return ["plan carries no expected_cost decomposition; refusing"]
    cost_total = float(expected["total"])
    legs = expected.get("legs")
    n_legs = int(pre_trade.get("n_legs", 0))
    if n_legs == 0:
        return []  # nothing trades; nothing to gate
    if not isinstance(legs, list) or len(legs) != n_legs:
        return ["expected_cost legs do not match the plan's legs; refusing"]
    traded_notional = sum(abs(float(leg.get("notional", 0.0))) for leg in legs)
    if not np.isfinite(cost_total) or not np.isfinite(traded_notional):
        return ["non-finite cost or traded notional; refusing"]

    reasons: list[str] = []
    cap = equity * costs.max_cost_bps_of_equity / 1e4
    if cost_total > cap:
        reasons.append(
            f"expected cost {cost_total:.2f} exceeds the absolute cap "
            f"{cap:.2f} ({costs.max_cost_bps_of_equity:.0f} bps of equity)")
    benefit = (traded_notional * costs.rebalance_benefit_bps / 1e4
               * costs.live_haircut)
    hurdle = cost_total * costs.safety_multiplier
    if benefit <= hurdle:
        reasons.append(
            f"net-alpha gate: haircut benefit {benefit:.2f} does not clear "
            f"{costs.safety_multiplier:.1f}x expected cost {cost_total:.2f} "
            f"(traded notional {traded_notional:.2f}, benefit assumption "
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
