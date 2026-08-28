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

    # Validate the cost decomposition BEFORE any exemption: a malformed cost
    # (missing, non-finite, or negative) is a data fault and must fail closed
    # even on an otherwise-exempt initial deployment.
    expected = pre_trade.get("expected_cost")
    n_legs = int(pre_trade.get("n_legs", 0))
    if n_legs > 0:
        if not isinstance(expected, dict) or "total" not in expected:
            return ["plan carries no expected_cost decomposition; refusing"]
        cost_total = float(expected["total"])
        legs = expected.get("legs")
        if not isinstance(legs, list) or len(legs) != n_legs:
            return ["expected_cost legs do not match the plan's legs; refusing"]
        traded_notional = sum(abs(float(leg.get("notional", 0.0))) for leg in legs)
        if not np.isfinite(cost_total) or not np.isfinite(traded_notional):
            return ["non-finite cost or traded notional; refusing"]
        if cost_total < 0:
            return [f"expected cost is negative ({cost_total:.2f}); malformed "
                    "decomposition, refusing"]

    if n_positions == 0 and gross_exposure < 0.01:
        return []  # true all-cash initial deployment
    if gross_exposure < 0.01:
        return ["inconsistent portfolio state: positions exist with ~zero "
                "gross exposure"]
    if n_legs == 0:
        return []  # nothing trades; nothing to gate

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
                          registry=None,
                          ) -> tuple[str, list[str]]:
    failures: list[str] = []
    audit_reasons: list[str] = []
    failures.extend(_views_lineage_failures(moments_summary, registry))
    audit_reasons.extend(_views_lineage_audit(moments_summary, registry))
    vals = np.array(list(targets.values()), dtype=float)
    target_gross = float(np.abs(vals).sum())
    drawdown: float | None = None
    drawdown_tier: str | None = None
    if portfolio_state is not None:
        try:
            drawdown = _portfolio_drawdown(portfolio_state)
            drawdown_tier = mandate.drawdown_tier(drawdown)
        except (TypeError, ValueError) as exc:
            failures.append(f"invalid portfolio drawdown state: {exc}")
    liquidation_mode = (
        drawdown_tier == "breaker"
        and np.isfinite(target_gross)
        and target_gross <= 1e-4
    )

    if not np.all(np.isfinite(vals)):
        failures.append("non-finite weight")
    for t in targets:
        if t not in mandate.universe_whitelist:
            failures.append(f"{t} outside universe whitelist")
    if mandate.long_only and np.any(vals < -1e-4):
        failures.append("long-only violated")
    if (
        mandate.fully_invested
        and not liquidation_mode
        and abs(vals.sum() - 1.0) > 1e-2
    ):
        failures.append(f"budget violated: sum={vals.sum():.4f}")
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

    if drawdown_tier is not None and drawdown is not None:
        if liquidation_mode:
            audit_reasons.append(
                "drawdown tier: breaker; liquidation mode permits target gross "
                f"exposure {target_gross:.4f}"
            )
        else:
            audit_reasons.append(
                f"drawdown tier: {drawdown_tier} at {drawdown:.2%}"
            )
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
                audit_reasons.append(
                    "stress: WARNING correlation-to-one volatility "
                    f"{stressed_vol:.2%} exceeds limit "
                    f"{mandate.stress_vol_limit:.2%}"
                )
            else:
                audit_reasons.append(
                    "stress: correlation-to-one volatility "
                    f"{stressed_vol:.2%} is within limit "
                    f"{mandate.stress_vol_limit:.2%}"
                )

    return ("PASS" if not failures else "FAIL"), [*failures, *audit_reasons]


def _views_run_id(moments_summary: dict | None) -> str | None:
    provenance = (moments_summary or {}).get("provenance")
    if not isinstance(provenance, Mapping):
        return None
    run_id = provenance.get("views_run_id")
    return str(run_id) if run_id else None


def _views_lineage_failures(moments_summary: dict | None,
                            registry) -> list[str]:
    """Why a views-conditioned covariance may not be traded on.

    A conditioned moment set is a covariance an *interpretation* moved. The
    quarantine that kept that interpretation bounded upstream — a named run, a
    KL budget it stayed inside, provenance that was actually checked — has to
    be re-verified here, or it ends the moment the optimizer reads the tensor.
    No registry to check against is a refusal, not an exemption: the referee
    cannot certify lineage it cannot read.
    """
    run_id = _views_run_id(moments_summary)
    if run_id is None:
        return []
    if registry is None:
        return [f"conditioned on views run {run_id!r}, whose lineage cannot be "
                "verified: the referee was given no registry to read it from"]
    run = registry.get_run(run_id)
    if run is None or run.get("kind") != "views":
        return [f"conditioned on views run {run_id!r}, which is not in the "
                "registry"]
    spec = run.get("spec") or {}
    reasons: list[str] = []
    try:
        kl_total = float(spec.get("kl_total", 0.0))
        kl_budget = float(spec.get("kl_budget", 0.0))
    except (TypeError, ValueError):
        return [f"views run {run_id!r} carries an unreadable KL decomposition"]
    if kl_total > kl_budget:
        reasons.append(
            f"conditioned on a views run over its KL budget "
            f"({kl_total:.4f} > {kl_budget:.4f})")
    if spec.get("provenance_verified") is not True:
        reasons.append(
            f"conditioned on views run {run_id!r} whose provenance was never "
            "verified against an excerpt or the archive")
    return reasons


def _views_lineage_audit(moments_summary: dict | None, registry) -> list[str]:
    """Name a lineage that checked out; silence would not distinguish it from none."""
    run_id = _views_run_id(moments_summary)
    if run_id is None or registry is None:
        return []
    if _views_lineage_failures(moments_summary, registry):
        return []
    provenance = (moments_summary or {}).get("provenance") or {}
    drift = provenance.get("mean_pinning_max_abs")
    tail = "" if drift is None else f", mean drift discarded {float(drift):.2e}"
    return [f"views lineage: conditioned on run {run_id} within its KL "
            f"budget, provenance verified{tail}"]


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
