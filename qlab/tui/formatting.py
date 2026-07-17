"""Rendering helpers for compact terminal financial data."""

from __future__ import annotations

import math


def sparkline(values: list[float]) -> str:
    """Render a stable unicode sparkline; flat and empty series are valid."""
    ticks = "▁▂▃▄▅▆▇█"
    if not values:
        return ""
    finite = [float(value) for value in values if math.isfinite(float(value))]
    if not finite:
        return ""
    lo, hi = min(finite), max(finite)
    if hi <= lo:
        return ticks[3] * len(finite)
    return "".join(
        ticks[min(7, max(0, round((value - lo) / (hi - lo) * 7)))]
        for value in finite
    )


def weight_bar(value: float, width: int = 16) -> str:
    value = min(1.0, max(0.0, float(value)))
    filled = min(width, max(0, round(value * width)))
    return "█" * filled + "░" * (width - filled)


def pct(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}%}"


def money(value: float | None) -> str:
    return "—" if value is None else f"${value:,.2f}"
