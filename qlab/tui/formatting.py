"""Rendering helpers for compact terminal financial data."""

from __future__ import annotations

import math
import re
import textwrap


# A mis-decoded UTF-8 stream (locale cp1252 on Windows) turns common typographic
# glyphs into these sequences. The Popen is fixed to read UTF-8, but text already
# captured — or produced by another surface — is normalised here as well.
_MOJIBAKE = {
    "â€”": "—", "â€“": "–", "â€™": "’", "â€˜": "‘",
    "â€œ": "“", "â€\x9d": "”", "â€¦": "…", "Â·": "·",
    "â€¢": "•", "Â ": " ", "Â": "",
}

# One or more ``word_`` segments then ``id`` — decision_id, plan_id,
# moment_set_id, workflow_id, objective_id, verdict_id, run_id. Requires the
# underscore, so ordinary words ("grid", "valid") never match.
_ID_LABEL = r"(?:[A-Za-z][A-Za-z0-9]*_)+id"
# An id-looking value: back-ticked, or a token that carries a digit or a hyphen
# (so a following ordinary word like "was recorded" is left untouched). A
# trailing sentence period is deliberately not consumed.
_ID_VALUE = r"(?:`[^`]*`|(?=[\w.\-]*[\d-])[\w\-]+(?:\.[\w\-]+)*)"
_ID_PAREN = re.compile(rf"\s*[(\[]\s*{_ID_LABEL}\b\s*[:=]?\s*{_ID_VALUE}?\s*[)\]]")
_ID_INLINE = re.compile(rf"\b{_ID_LABEL}\b\s*[:=]?\s*{_ID_VALUE}")
_ID_BARE = re.compile(rf"\b{_ID_LABEL}\b")
_HEADING = re.compile(r"\s*#{1,6}\s+(.*\S)\s*$")


def demojibake(text: str) -> str:
    """Repair the cp1252-misread-UTF-8 sequences a stream can leave behind."""
    for bad, good in _MOJIBAKE.items():
        text = text.replace(bad, good)
    return text


def _strip_ids(text: str) -> str:
    """Drop internal ``*_id`` references from prose a person reads.

    These are audit keys the reader never types, so they read as noise in a
    memo. A trailing id-looking value goes with its label; a bare mention just
    loses the label. Empty brackets and doubled punctuation left behind are tidied.
    """
    text = _ID_PAREN.sub("", text)
    text = _ID_INLINE.sub("", text)
    text = _ID_BARE.sub("", text)
    text = re.sub(r"[(\[]\s*[)\]]", "", text)          # emptied brackets
    text = re.sub(r"\s+([,.;:])", r"\1", text)          # space before punctuation
    text = re.sub(r"([,;:])(\s*[,;:])+", r"\1", text)   # doubled separators
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def clean_report_line(
    line: str, *, strip_ids: bool = True
) -> tuple[bool, str]:
    """Normalise one line of agent prose for a terminal log.

    Returns ``(is_heading, text)``. Strips the markdown that renders literally in
    a RichLog — ``#`` headers, ``**`` bold, back-ticks — and repairs mojibake.
    Raw ``*_id`` audit keys are removed by default for narrative surfaces but
    can be retained for evidence and diagnostics. A header line is reported so
    the caller can style it like a section label.
    """
    line = demojibake(line)
    heading = _HEADING.match(line)
    body = heading.group(1) if heading else line
    if strip_ids:
        body = _strip_ids(body)
    body = body.replace("**", "").replace("`", "")
    body = re.sub(r"[ \t]{2,}", " ", body).rstrip()
    if heading:
        return True, body.rstrip(" :").upper()
    return False, body


def verdict_chip(verdict: dict | None) -> tuple[str, str]:
    """Return the semantic theme token and compact referee verdict text."""
    label = str((verdict or {}).get("verdict") or "").upper()
    if label == "PASS":
        return "UP", "PASS"
    if label == "FAIL":
        return "DOWN", "FAIL"
    return "MUTED", "—"


def key_number_lines(pairs: list[tuple[str, object]]) -> list[str]:
    """Render label/value pairs with one shared, visible value column."""
    rendered = [(str(label), str(value)) for label, value in pairs]
    if not rendered:
        return []
    label_width = max(len(label) for label, _value in rendered)
    return [
        f"{label:<{label_width}}  {value}"
        for label, value in rendered
    ]


def bulletin(
    lines: list[str], max_len: int = 200, *, strip_ids: bool = True
) -> list[str]:
    """Clean agent prose into non-empty, length-bounded bulletin lines."""
    limit = max(0, int(max_len))
    if not limit:
        return []
    rendered = []
    for raw in lines:
        _is_heading, text = clean_report_line(
            str(raw), strip_ids=strip_ids
        )
        text = text[:limit].rstrip()
        if text:
            rendered.append(text)
    return rendered


_REPORT_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_REPORT_BULLET = re.compile(r"^[-*•]\s+(.*)$")
_REPORT_NUMBERED = re.compile(r"^\d+[.)]\s")
# A box-drawing or pipe lead means the sender aligned the line by hand; wrapping
# or truncating it destroys the alignment that makes the table readable.
_REPORT_TABLE_LEAD = "|┌┬┐├┼┤└┴┘─━│┃┏┳┓┡╇┩┗┻┛╭╮╰╯═╔╗╚╝"


def _plain(text: str) -> str:
    """Drop the inline markers a RichLog would otherwise print literally."""
    return text.replace("**", "").replace("`", "").strip()


def _is_fence(stripped: str) -> bool:
    return stripped.startswith("```") or stripped.startswith("~~~")


def is_numbered_item(text: str) -> bool:
    """True when a list item already carries its own ordinal marker.

    Distinguishes "1. bind the targets" from a bullet that merely opens with a
    count ("3 of 7 arms admitted"), which still needs its glyph.
    """
    return _REPORT_NUMBERED.match(text) is not None


def fence_state_after(lines: list[str], fenced: bool = False) -> bool:
    """Fence state ``lines`` leave behind, so a caller can carry it forward.

    The console streams one token at a time: a ``` opener and the code it
    introduces usually arrive in different calls, so fence state cannot live
    inside a single ``report_lines`` call. This is the caller's memory of it.
    """
    for raw in lines:
        if _is_fence(str(raw).strip()):
            fenced = not fenced
    return fenced


def report_lines(lines: list[str], max_len: int = 260, *,
                 fenced: bool = False) -> list[tuple[str, str]]:
    """Normalize agent-report markdown into (kind, text) console lines.

    Tone-free by design — the app maps kinds to theme markup. Tables and
    code stay verbatim (truncating them mangles alignment); paragraphs wrap
    instead of truncating so long reports stay readable. ``fenced`` says whether
    the caller is already inside a ``` block; pair it with ``fence_state_after``
    to render a report that arrives in pieces.
    """
    out: list[tuple[str, str]] = []
    for raw in lines:
        line = demojibake(str(raw)).rstrip()
        stripped = line.strip()
        if not stripped:
            if out and out[-1][0] != "blank":
                out.append(("blank", ""))
            continue
        if _is_fence(stripped):
            fenced = not fenced
            continue                       # fence markers carry no content
        if fenced:
            out.append(("code", line))     # fenced code needs no indent to be code
            continue
        heading = _REPORT_HEADING.match(stripped)
        if heading:
            kind = "h1" if len(heading.group(1)) <= 2 else "h2"
            out.append((kind, _plain(heading.group(2))))
            continue
        if stripped[0] in _REPORT_TABLE_LEAD:
            out.append(("table", line))
            continue
        # List items are tested before the indent rule: a nested item is indented
        # markdown, not code, and dimming it as code reads as a broken report.
        bullet = _REPORT_BULLET.match(stripped)
        if bullet:
            for piece in textwrap.wrap(_plain(bullet.group(1)), max_len) or [""]:
                out.append(("bullet", piece))
            continue
        if _REPORT_NUMBERED.match(stripped):
            for piece in textwrap.wrap(_plain(stripped), max_len) or [""]:
                out.append(("bullet", piece))
            continue
        if line.startswith(("    ", "\t")):
            out.append(("code", line))
            continue
        for piece in textwrap.wrap(_plain(stripped), max_len):
            out.append(("text", piece))
    return out


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


def spark(values: list[float], width: int = 12) -> str:
    """A one-row trend line, or "" when there is nothing to draw.

    Block glyphs fill from the baseline, so at sparkline size a noisy series
    renders as a solid filled mass and the shape is unreadable. A braille row
    marks only the value, so it reads as a line, and it carries two samples per
    cell — twice the history in the same width.

    Returns "" rather than a row of blanks for an unplottable series: a blank
    row is indistinguishable from a flat one, and those mean different things.
    """
    finite = [float(v) for v in values if math.isfinite(float(v))]
    if len(finite) < 2:
        return ""
    return braille_chart(finite, width=width, height=1)[0]


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


def braille_chart(values: list[float], width: int, height: int, *,
                  fill: bool = False) -> list[str]:
    """Render ``values`` as a multi-row braille line chart.

    ``width``/``height`` are terminal cells; the dot grid is therefore
    ``2*width`` by ``4*height``. Returns exactly ``height`` strings of exactly
    ``width`` characters (blank braille where there is no line), so the caller
    can drop it straight into a fixed region without reflow. A flat or too-short
    series yields blank rows rather than raising.

    ``fill`` shades from the line down to the baseline. A bare line is one dot
    thick, so across a tall plot it is mostly empty space and reads as scatter
    even though it is continuous; filling under it gives the eye a shape to
    follow, which is why price charts are drawn that way. Use it whenever the
    plot is given real height.
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

    # Two regimes, because point-sampling is only safe in one of them.
    #
    # Upsampling (fewer points than columns): interpolate, so 40 daily bars on
    # a wide terminal read as a curve rather than a staircase.
    #
    # Downsampling (more points than columns): aggregate each column's whole
    # bucket to its low and high. Point-sampling here silently drops whatever
    # falls between the sampled indices — plotting ~4500 daily bars into ~200
    # columns visits under 5% of them, and a spike landing in the other 95%
    # disappears from the chart AND from its axis labels. On a price chart the
    # high and the low are the two facts most worth having; a line that can
    # omit them is not a smaller picture, it is a wrong one.
    if n <= cols:
        spans: list[tuple[float, float]] = []
        for i in range(cols):
            pos = i * (n - 1) / (cols - 1) if cols > 1 else 0.0
            lo_i = int(pos)
            hi_i = min(n - 1, lo_i + 1)
            frac = pos - lo_i
            value = finite[lo_i] * (1.0 - frac) + finite[hi_i] * frac
            spans.append((value, value))
    else:
        spans = []
        for i in range(cols):
            start = (i * n) // cols
            stop = max(start + 1, ((i + 1) * n) // cols)
            bucket = finite[start:stop]
            spans.append((min(bucket), max(bucket)))

    lo = min(low for low, _ in spans)
    hi = max(high for _, high in spans)
    span = (hi - lo) or 1.0

    def _dot_y(value: float) -> int:
        return int(round((value - lo) / span * (rows - 1)))

    grid = [[0] * width for _ in range(height)]
    prev_top = _dot_y(spans[0][1])
    for cx in range(cols):
        col_lo, col_hi = spans[cx]
        bottom, top = _dot_y(col_lo), _dot_y(col_hi)
        # The column's own range, plus the run back to the previous column, so
        # the line stays continuous across a gap.
        low_dot = min(bottom, top, prev_top)
        high_dot = max(bottom, top, prev_top)
        if fill:
            # Down to the baseline rather than just the column's own run.
            low_dot = 0
        for dy in range(low_dot, high_dot + 1):
            ry = (rows - 1) - dy  # dot rows are top-down
            grid[ry // 4][cx // 2] |= _BRAILLE_DOTS[ry % 4][cx % 2]
        prev_top = top
    return [
        "".join(chr(0x2800 + cell) for cell in row)
        for row in grid
    ]


def connection_chip(age_seconds: float | None, failures: int) -> tuple[str, str]:
    """(text, level) for snapshot freshness; level is ok | warn | down.

    Three consecutive refresh failures mean the owner is gone, not merely a
    slow request — one timeout must not scream OWNER DOWN during an action.
    """
    if failures >= 3:
        if age_seconds is None:
            return "OWNER DOWN", "down"
        return f"OWNER DOWN · last {int(age_seconds)}s", "down"
    if age_seconds is None:
        return "CONNECTING", "warn"
    if age_seconds > 10:
        minutes, seconds = divmod(int(age_seconds), 60)
        return f"STALE {minutes}:{seconds:02d}", "warn"
    return "LIVE", "ok"


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
