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

# A variable reference only ever occurs inside a markup tag, because that is the
# only place a colour can be applied. Substituting across the whole line instead
# treats body text as markup: a console line reading "equity $100,000.00" parses
# "$100" as a variable name and the write raises. On this desk that is most
# lines, and the text can come from the operator's own chat message.
_TAG = re.compile(r"\[[^\[\]]*\]")
# A name must start with a letter or underscore. No theme variable is digit-led,
# so this keeps a currency amount from ever looking like a reference.
_VARIABLE = re.compile(r"\$([a-zA-Z_][a-zA-Z0-9_\-]*)")


def resolve(markup: str, *, theme: str = tokens.DEFAULT_THEME) -> str:
    """Replace ``$variable`` references inside markup tags with real colours.

    For the Rich-rendered console only. Raises `KeyError` on an unknown variable
    *inside a tag* rather than emitting a literal ``$name`` that Rich would
    either reject or silently print — a mistyped token name must still fail
    loud. Text outside a tag is returned untouched, dollar signs and all.
    """
    variables = tokens.THEMES[theme].variables

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        try:
            return variables[name]
        except KeyError as exc:
            raise KeyError(
                f"no theme variable ${name} in {theme}") from exc

    return _TAG.sub(lambda tag: _VARIABLE.sub(substitute, tag.group(0)), markup)
