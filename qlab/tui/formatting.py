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


# Braille cell = a 2-wide × 4-tall dot matrix (base codepoint U+2800). This
# table maps a (row, col) inside one cell to its dot bit, so a line can be
# plotted at 2× horizontal and 4× vertical the resolution of block glyphs —
# which is what makes a terminal price chart read as a curve, not a bar row.
_BRAILLE_DOTS = (
    (0x01, 0x08),
    (0x02, 0x10),
    (0x04, 0x20),
    (0x40, 0x80),
)


def braille_chart(values: list[float], width: int, height: int) -> list[str]:
    """Render ``values`` as a multi-row braille line chart.

    ``width``/``height`` are terminal cells; the dot grid is therefore
    ``2*width`` by ``4*height``. Returns exactly ``height`` strings of exactly
    ``width`` characters (blank braille where there is no line), so the caller
    can drop it straight into a fixed region without reflow. A flat or too-short
    series yields blank rows rather than raising.
    """
    width = max(1, int(width))
    height = max(1, int(height))
    blank = "⠀" * width
    finite = [float(v) for v in values if math.isfinite(float(v))]
    if len(finite) < 2:
        return [blank for _ in range(height)]

    cols = width * 2
    rows = height * 4
    n = len(finite)
    # Resample to one point per dot-column with linear interpolation, so a short
    # series (e.g. 40 daily bars) upsampled onto a wide terminal reads as a
    # smooth curve rather than a staircase of repeated samples.
    sampled = []
    for i in range(cols):
        pos = i * (n - 1) / (cols - 1)
        lo_i = int(pos)
        hi_i = min(n - 1, lo_i + 1)
        frac = pos - lo_i
        sampled.append(finite[lo_i] * (1.0 - frac) + finite[hi_i] * frac)
    lo, hi = min(sampled), max(sampled)
    span = (hi - lo) or 1.0
    # Dot-space y, 0 at the bottom of the grid.
    ys = [int(round((value - lo) / span * (rows - 1))) for value in sampled]

    grid = [[0] * width for _ in range(height)]
    prev = ys[0]
    for cx in range(cols):
        cur = ys[cx]
        # Fill the vertical run between adjacent samples so the line is
        # continuous instead of a scatter of dots.
        for dy in range(min(prev, cur), max(prev, cur) + 1):
            ry = (rows - 1) - dy  # dot rows are top-down
            grid[ry // 4][cx // 2] |= _BRAILLE_DOTS[ry % 4][cx % 2]
        prev = cur
    return [
        "".join(chr(0x2800 + cell) for cell in row)
        for row in grid
    ]


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
