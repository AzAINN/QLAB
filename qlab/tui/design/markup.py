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
# "$100" as a variable name. `(?<!\\)` respects the escape `rich.markup.escape`
# emits, so text the caller already neutralised stays neutral.
_TAG = re.compile(r"(?<!\\)\[[^\[\]]*\]")
# A name must start with a letter or underscore. No theme variable is digit-led,
# so this keeps a currency amount from ever looking like a reference.
_VARIABLE = re.compile(r"\$([a-zA-Z_][a-zA-Z0-9_\-]*)")


def resolve(markup: str, *, theme: str = tokens.DEFAULT_THEME) -> str:
    """Replace ``$variable`` references inside markup tags with real colours.

    For the Rich-rendered console only.

    Only *known* names are substituted. An unknown one is left as written,
    because this function sees operator prose as well as our own markup, and
    the two are not distinguishable by shape: `rich.markup.escape` does not
    escape `[$SPY]` — Rich does not consider that a tag — while this module's
    tag pattern necessarily does. Raising there killed the TUI on a normal
    chat message.

    Our own tokens are still pinned, just at the right time:
    `test_every_exported_markup_name_resolves` fails the build on a mistyped
    constant, which is where a typo in this repo's markup actually surfaces.
    A stylesheet naming a missing token still fails loud at import, because
    `Template.substitute` is strict.
    """
    variables = tokens.THEMES[theme].variables

    def substitute(match: re.Match[str]) -> str:
        return variables.get(match.group(1), match.group(0))

    return _TAG.sub(lambda tag: _VARIABLE.sub(substitute, tag.group(0)), markup)
