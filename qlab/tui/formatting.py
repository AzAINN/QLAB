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


def phase_elapsed(started_at: str | None, completed_at: str | None,
                  now: str | None = None) -> str:
    """Compact elapsed label between two ISO timestamps ('', '8s', '2m10s').

    Open phases measure against ``now``; malformed input renders as '' so a
    partial registry row can never break a paint.
    """
    from datetime import datetime, timezone

    if not started_at:
        return ""
    try:
        start = datetime.fromisoformat(str(started_at))
        end_raw = completed_at or now
        end = (datetime.fromisoformat(str(end_raw)) if end_raw
               else datetime.now(timezone.utc))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        seconds = max(0, int((end - start).total_seconds()))
    except (TypeError, ValueError):
        return ""
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"
