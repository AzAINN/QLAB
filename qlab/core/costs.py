"""Pure transaction-cost estimates shared by research and pre-trade reporting."""

from __future__ import annotations

from typing import TypeAlias

import numpy as np

DEFAULT_SPREAD_BPS = 2.0
DEFAULT_COMMISSION_BPS = 0.0
DEFAULT_IMPACT_K = 1.0
DEFAULT_ADV_NOTIONAL = 50_000_000.0
DEFAULT_DAILY_VOL = 0.02
MINIMUM_TOTAL_BPS = 1.0

CostValue: TypeAlias = float | np.ndarray
CostBreakdown: TypeAlias = dict[str, CostValue]


def _result_value(value: np.ndarray) -> CostValue:
    return float(value) if value.ndim == 0 else value


def cost_model(
    trade_notional,
    price,
    adv_notional,
    daily_vol,
    spread_bps: float = DEFAULT_SPREAD_BPS,
    commission_bps: float = DEFAULT_COMMISSION_BPS,
    impact_k: float = DEFAULT_IMPACT_K,
) -> CostBreakdown:
    """Estimate absolute transaction cost in the notional's currency.

    Inputs follow NumPy broadcasting rules. ``trade_notional`` may be signed;
    costs are based on its absolute value. The fixed components are commission
    and half the quoted spread. Market impact follows the square-root model::

        impact_k * daily_vol * sqrt(trade_notional / adv_notional)
            * trade_notional

    A one-basis-point floor is applied to the total. ``minimum_adjustment``
    makes that floor explicit so the returned components always sum to
    ``total``.
    """
    try:
        (
            notional,
            px,
            adv,
            vol,
            spread,
            commission_rate,
            impact_coefficient,
        ) = np.broadcast_arrays(
            np.asarray(trade_notional, dtype=float),
            np.asarray(price, dtype=float),
            np.asarray(adv_notional, dtype=float),
            np.asarray(daily_vol, dtype=float),
            np.asarray(spread_bps, dtype=float),
            np.asarray(commission_bps, dtype=float),
            np.asarray(impact_k, dtype=float),
        )
    except ValueError as exc:
        raise ValueError("transaction-cost inputs are not broadcast-compatible") from exc

    named_inputs = {
        "trade_notional": notional,
        "price": px,
        "adv_notional": adv,
        "daily_vol": vol,
        "spread_bps": spread,
        "commission_bps": commission_rate,
        "impact_k": impact_coefficient,
    }
    for name, values in named_inputs.items():
        if not np.all(np.isfinite(values)):
            raise ValueError(f"{name} must contain only finite values")
    if np.any(px <= 0):
        raise ValueError("price must be positive")
    if np.any(adv <= 0):
        raise ValueError("adv_notional must be positive")
    for name, values in (
        ("daily_vol", vol),
        ("spread_bps", spread),
        ("commission_bps", commission_rate),
        ("impact_k", impact_coefficient),
    ):
        if np.any(values < 0):
            raise ValueError(f"{name} must be non-negative")

    notional = np.abs(notional)
    commission = notional * commission_rate / 1e4
    half_spread = notional * spread / (2e4)
    impact = (
        impact_coefficient
        * vol
        * np.sqrt(notional / adv)
        * notional
    )
    subtotal = commission + half_spread + impact
    minimum_total = notional * MINIMUM_TOTAL_BPS / 1e4
    minimum_adjustment = np.maximum(minimum_total - subtotal, 0.0)
    total = subtotal + minimum_adjustment

    return {
        "commission": _result_value(commission),
        "half_spread": _result_value(half_spread),
        "impact": _result_value(impact),
        "minimum_adjustment": _result_value(minimum_adjustment),
        "total": _result_value(total),
    }
