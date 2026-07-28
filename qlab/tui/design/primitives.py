"""The only sanctioned way to emit styled content in the operator client.

These are pure functions over `rich.text.Text`. They take no widget, touch no
screen, and import nothing from Textual, so the alignment and colour
invariants are unit-testable without a terminal.

Two contracts live here and are enforced by tests rather than by review:

* the label column is exactly `LABEL_WIDTH`, so every pane in every view aligns
  its values on the same column;
* `signed=True` marks a *delta*, which is the only thing permitted to carry a
  direction colour, and it always emits an explicit sign character. Colour is
  never the sole encoding of direction -- the light themes cannot separate up
  from down by luminance at all.
"""

from __future__ import annotations

from rich.text import Text

from qlab.tui.design import glyphs, tokens


# Values begin at column 15 (index 14). Widening this is a design change, not a
# tuning knob: it moves every value in every view.
LABEL_WIDTH = 14

RULE_CHARACTER = "─"

# Unit -> formatter. `pct` and `bps` take a fraction, not a percentage, so a
# caller cannot accidentally render 4.2 as 4.20% when it meant 420%.
_UNITS = {
    "pct": lambda value: f"{value * 100:.2f}%",
    "bps": lambda value: f"{value * 10_000:.0f}bp",
    "money": lambda value: f"{value:,.2f}",
    "ratio": lambda value: f"{value:.2f}",
    "count": lambda value: f"{int(value):,d}",
}

_ABSENT = {
    # An uncomputed value is a dash, never a zero: displaying 0.00 for
    # something that was not measured is a false reading.
    "unknown": ("—", "faint"),
    "gated": ("gated", "blocked"),
}


def _colour(role: str, theme: str) -> str:
    return tokens.role(theme, role)


def section(title: str, *, theme: str = tokens.DEFAULT_THEME) -> Text:
    """A section heading: the only accented text in a pane besides state."""
    return Text(title.upper(), style=f"bold {_colour('accent', theme)}")


def field(
    label: str,
    value: str,
    *,
    meta: str | None = None,
    width: int | None = None,
    theme: str = tokens.DEFAULT_THEME,
) -> Text:
    """One `label / value / trailing meta` row obeying the column contract.

    An over-long label is truncated rather than allowed to push the value
    column, because one long label would otherwise misalign a whole pane.
    """
    rendered = Text()
    rendered.append(label[:LABEL_WIDTH].ljust(LABEL_WIDTH),
                    style=_colour("muted", theme))
    rendered.append(value, style=_colour("text", theme))

    if meta is not None:
        if width is None:
            raise ValueError("meta needs a width to right-align against")
        used = LABEL_WIDTH + len(value) + len(meta)
        rendered.append(" " * max(1, width - used))
        rendered.append(meta, style=_colour("faint", theme))

    return rendered


def num(
    value: float,
    unit: str,
    *,
    signed: bool = False,
    width: int | None = None,
    theme: str = tokens.DEFAULT_THEME,
) -> Text:
    """Format a number, right-aligned and tabular.

    `signed=True` declares the value a delta: it gains an explicit sign and a
    direction colour. An unsigned level gets neither -- a price is not an
    opinion. Exactly zero is unsigned and neutral in both modes, because zero
    has no direction.
    """
    try:
        formatter = _UNITS[unit]
    except KeyError as exc:
        raise KeyError(
            f"unknown unit {unit!r}; known units: {sorted(_UNITS)}") from exc

    body = formatter(value)
    if signed and value > 0:
        body = f"+{body}"

    if width is not None:
        body = body.rjust(width)

    if signed and value > 0:
        style = _colour("up", theme)
    elif signed and value < 0:
        style = _colour("down", theme)
    else:
        style = _colour("text", theme)

    return Text(body, style=style)


def state_badge(
    state: str,
    *,
    glyph: str | None = None,
    fallback: str | None = None,
    ascii_only: bool = False,
    theme: str = tokens.DEFAULT_THEME,
) -> Text:
    """A state's glyph in its role colour -- the two encodings, together.

    `glyph` overrides the character while keeping the role colour, which is how
    the working state animates without the colour flickering.

    `fallback` names a known state to degrade to when `state` is unmapped.
    Strict by default: a status the design system cannot express should fail
    loud. A *live* view opts in, because crashing the workstation is worse than
    an approximate glyph -- and the state name is rendered as text beside it, so
    the true status stays visible either way.
    """
    try:
        resolved, role = glyphs.badge(state, ascii_only=ascii_only)
    except KeyError:
        if fallback is None:
            raise
        # A bad fallback is a design-system bug and must still fail loud.
        resolved, role = glyphs.badge(fallback, ascii_only=ascii_only)
    return Text(glyph if glyph is not None else resolved,
                style=_colour(role, theme))


def absent(kind: str = "unknown", *, theme: str = tokens.DEFAULT_THEME) -> Text:
    """Render a value that is missing, distinguishing *why* it is missing."""
    try:
        body, role = _ABSENT[kind]
    except KeyError as exc:
        raise KeyError(
            f"unknown absence {kind!r}; known kinds: {sorted(_ABSENT)}") from exc
    return Text(body, style=_colour(role, theme))


def rule(width: int, *, theme: str = tokens.DEFAULT_THEME) -> Text:
    """A quiet horizontal divider. Chrome is always neutral, never accented."""
    return Text(RULE_CHARACTER * width, style=_colour("border", theme))
