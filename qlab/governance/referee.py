"""The referee gate, as code. Nothing trades without a PASS (research-plan §3).

The deterministic referee re-verifies the mandate-critical facts *independently*
of build_plan (defense in depth) and is the autopilot's gatekeeper. The LLM
referee agent adds qualitative review in interactive sessions and submits its
verdict through the same registry.log_verdict tool - one gate, two reviewers.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

import numpy as np

from qlab.core.stress import stress_correlation_to_one
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
                          portfolio_state: dict | None = None,
                          vols: Mapping[str, float] | Sequence[float] | None = None,
                          ) -> tuple[str, list[str]]:
    failures: list[str] = []
    warnings: list[str] = []
    vals = np.array(list(targets.values()), dtype=float)
    if not np.all(np.isfinite(vals)):
        failures.append("non-finite weight")
    for t in targets:
        if t not in mandate.universe_whitelist:
            failures.append(f"{t} outside universe whitelist")
    if mandate.long_only and np.any(vals < -1e-4):
        failures.append("long-only violated")
    if mandate.fully_invested and abs(vals.sum() - 1.0) > 1e-2:
        failures.append(f"budget violated: sum={vals.sum():.4f}")
    target_gross = float(np.abs(vals).sum())
    if np.isfinite(target_gross) and (
        target_gross > mandate.max_gross_exposure + 1e-4
    ):
        failures.append(
            f"gross exposure cap breached: {target_gross:.4f} exceeds "
            f"{mandate.max_gross_exposure:.4f}"
        )
    over = {t: v for t, v in targets.items() if v > mandate.max_weight_per_asset + 1e-4}
    if over:
        failures.append(f"per-asset cap breach: {over}")
    if isinstance(as_of, date) and as_of > date.today():
        failures.append("look-ahead as_of")
    if moments_summary and moments_summary.get("condition_number", 0) > 1e8:
        failures.append("ill-conditioned covariance")

    if portfolio_state is not None:
        try:
            drawdown = _portfolio_drawdown(portfolio_state)
            drawdown_tier = mandate.drawdown_tier(drawdown)
        except (TypeError, ValueError) as exc:
            failures.append(f"invalid portfolio drawdown state: {exc}")
        else:
            if drawdown_tier in {"control", "breaker"} and np.isfinite(target_gross):
                try:
                    current_gross = _current_gross_exposure(portfolio_state)
                except (TypeError, ValueError) as exc:
                    failures.append(f"invalid current gross exposure: {exc}")
                else:
                    if target_gross > current_gross + 1e-4:
                        failures.append(
                            f"drawdown {drawdown_tier} tier blocks gross exposure "
                            f"increase ({current_gross:.4f} -> {target_gross:.4f})"
                        )
            if (
                drawdown_tier == "breaker"
                and np.isfinite(target_gross)
                and target_gross > 1e-4
            ):
                failures.append(
                    "drawdown breaker tier permits liquidation only; "
                    f"target gross exposure is {target_gross:.4f}"
                )

    if vols is not None:
        try:
            stress_weights: Mapping[str, float] | Sequence[float]
            stress_weights = (
                targets if isinstance(vols, Mapping) else list(targets.values())
            )
            stressed_vol = stress_correlation_to_one(stress_weights, vols)
        except (TypeError, ValueError) as exc:
            failures.append(f"invalid correlation stress inputs: {exc}")
        else:
            if stressed_vol > mandate.stress_vol_limit:
                # Correlation-to-one is a conservative bound, not a forecast, so
                # exceeding it is audited as a warning without vetoing a valid plan.
                warnings.append(
                    "stress: WARNING correlation-to-one volatility "
                    f"{stressed_vol:.2%} exceeds limit "
                    f"{mandate.stress_vol_limit:.2%}"
                )

    return ("PASS" if not failures else "FAIL"), [*failures, *warnings]


def _portfolio_drawdown(portfolio_state: dict) -> float:
    if not isinstance(portfolio_state, dict):
        raise TypeError("portfolio_state must be a mapping")
    if "drawdown" in portfolio_state:
        drawdown = float(portfolio_state["drawdown"])
    else:
        equity = float(portfolio_state["equity"])
        high_water_mark = float(portfolio_state["high_water_mark"])
        if high_water_mark <= 0:
            raise ValueError("high_water_mark must be positive")
        drawdown = 1.0 - equity / high_water_mark
    if not np.isfinite(drawdown):
        raise ValueError("drawdown must be finite")
    return drawdown


def _current_gross_exposure(portfolio_state: dict) -> float:
    if "gross_exposure" in portfolio_state:
        gross = float(portfolio_state["gross_exposure"])
    else:
        weights = portfolio_state.get("weights")
        if not isinstance(weights, dict):
            raise ValueError("weights or gross_exposure is required")
        gross = sum(abs(float(weight)) for weight in weights.values())
    if not np.isfinite(gross) or gross < 0:
        raise ValueError("gross exposure must be finite and non-negative")
    return gross
