"""State glyphs, with an ASCII fallback for terminals that cannot render them.

State is encoded twice -- glyph and colour -- so it survives a monochrome
terminal. Nothing in the client may communicate a status by colour alone.

Every glyph must occupy exactly one cell. A double-width character silently
shifts every column after it and breaks the label contract in
:mod:`qlab.tui.design.primitives`. East-Asian *ambiguous* glyphs are permitted
because they are single-width outside CJK locales; the ASCII table covers the
locales and code pages where they are not.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GlyphSpec:
    """One state's rendering: a preferred glyph, a fallback, and a colour role."""

    unicode: str
    ascii: str
    role: str


# Keys are a superset of the retiring theme.STATE_STYLE table, so a view can be
# migrated onto this module without losing a status. `stopping` and `stale` are
# new: an optimistic control state and a projection-age state that the previous
# table had no way to express.
STATES = {
    "queued": GlyphSpec("·", ".", "faint"),
    "waiting": GlyphSpec("◦", ":", "faint"),
    "working": GlyphSpec("●", "o", "accent"),
    "done": GlyphSpec("✓", "+", "up"),
    "failed": GlyphSpec("✗", "x", "down"),
    "blocked": GlyphSpec("!", "!", "blocked"),
    "stopping": GlyphSpec("◐", "~", "blocked"),
    "interrupted": GlyphSpec("‖", "=", "blocked"),
    "abandoned": GlyphSpec("⊘", "/", "faint"),
    "idle": GlyphSpec("◌", "-", "faint"),
    "stale": GlyphSpec("≈", "?", "faint"),
}


def supports_unicode(encoding: str) -> bool:
    """Whether `encoding` can represent every glyph in the preferred table.

    Probing the actual glyphs beats maintaining a list of blessed encodings:
    a code page either round-trips them or it does not.
    """
    for spec in STATES.values():
        try:
            spec.unicode.encode(encoding)
        except (UnicodeEncodeError, LookupError):
            return False
    return True


def badge(state: str, *, ascii_only: bool = False) -> tuple[str, str]:
    """Return `(glyph, colour_role)` for a state.

    Raises `KeyError` for an unknown state rather than rendering blank: a
    status the design system cannot express must fail loud.
    """
    try:
        spec = STATES[state]
    except KeyError as exc:
        raise KeyError(
            f"no glyph for state {state!r}; known states: {sorted(STATES)}"
        ) from exc
    return (spec.ascii if ascii_only else spec.unicode), spec.role
