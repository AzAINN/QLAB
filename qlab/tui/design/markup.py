"""Colour names for inline markup, bound to theme variables instead of hex.

`qlab.tui.app` builds roughly five hundred markup fragments as
``f"[{MUTED}]...[/]"``. Importing those names from here instead of from
:mod:`qlab.tui.theme` makes every one of them theme-reactive without editing a
single call site: Textual resolves ``$muted`` against the active theme at render
time, where a hex literal was frozen at import time.

Two consumers cannot use variables and must keep real colours:

* :mod:`qlab.desk_cli`, which prints through Rich, not Textual;
* ``RichLog``, which renders with ``rich.text.Text.from_markup``.

The log path calls :func:`resolve` to substitute variables itself. `desk_cli`
continues importing hex constants straight from :mod:`qlab.tui.theme`.
"""

from __future__ import annotations

import re

from qlab.tui.design import tokens


# Names mirror the retiring qlab.tui.theme constants exactly, so app.py changes
# its import source and nothing else.
BG = "$bg"
BG_PANEL = "$bg_panel"
BG_RAISED = "$bg_raised"
SEL_BG = "$sel_bg"
BORDER = "$border"
BORDER_HI = "$border_hi"
TEXT = "$text"
TEXT_HI = "$text_hi"
MUTED = "$muted"
DIM = "$dim"
AMBER = "$amber"
AMBER_HI = "$amber_hi"
GOLD = "$gold"
LABEL_GOLD = "$label_gold"
CYAN = "$cyan"
UP = "$up"
DOWN = "$down"
ALLOCATION_TRACK = "$allocation_track"
CHART_AXIS = "$chart_axis"

__all__ = [
    "ALLOCATION_TRACK", "AMBER", "AMBER_HI", "BG", "BG_PANEL", "BG_RAISED",
    "BORDER", "BORDER_HI", "CHART_AXIS", "CYAN", "DIM", "DOWN", "GOLD",
    "LABEL_GOLD", "MUTED", "SEL_BG", "TEXT", "TEXT_HI", "UP",
]

_VARIABLE = re.compile(r"\$([a-zA-Z0-9_\-]+)")


def resolve(markup: str, *, theme: str = tokens.DEFAULT_THEME) -> str:
    """Replace ``$variable`` references with the theme's colours.

    For the Rich-rendered console only. Raises `KeyError` on an unknown
    variable rather than emitting a literal ``$name`` that Rich would either
    reject or silently print.
    """
    variables = tokens.THEMES[theme].variables

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        try:
            return variables[name]
        except KeyError as exc:
            raise KeyError(
                f"no theme variable ${name} in {theme}") from exc

    return _VARIABLE.sub(substitute, markup)
