"""Semantic colour roles and the themes that bind them.

This is the only module in the client permitted to contain a colour literal.
A containment test enforces that; widgets that need a colour ask for a role.

Hue choices are functional. The accent is blue because it must be
chromatically distant from *both* profit-and-loss signs -- a teal accent
collides with `up`, a warm accent collides with `down`. `up` is teal-green and
`down` is rose-red rather than the hue-120/hue-0 pair, which widens their
separation under deuteranomaly while still reading as green and red.

Contrast is asserted against the raised panel as well as the canvas. The panel
is the stricter base and is where the retired amber palette's dim tier
measured 3.05:1, below a 3.5 target.
"""

from __future__ import annotations

from textual.theme import Theme


# Roles every theme must define. Chrome roles (bg/surface/panel/border) carry
# no text target -- giving them one would force them brighter and reintroduce
# the coloured chrome this system exists to avoid.
ROLE_NAMES = (
    "bg", "surface", "panel", "border", "selection",
    "text", "text_strong", "muted", "faint",
    "accent", "up", "down", "blocked",
)

# Legacy token name -> semantic role. The retiring stylesheet and roughly five
# hundred inline markup sites already speak this vocabulary; aliasing it onto
# roles makes every one of them theme-reactive without editing a call site.
# The aliases are a migration surface, not a permanent API -- they shrink as
# views move onto the primitives.
_ALIASES = {
    "bg": "bg",
    "bg_panel": "surface",
    "bg_raised": "panel",
    "sel_bg": "selection",
    "border": "border",
    "border_hi": "accent",
    "text": "text",
    "text_hi": "text_strong",
    "muted": "muted",
    "dim": "faint",
    # Amber and gold stop being decoration and collapse onto one meaning.
    "amber": "blocked",
    "amber_hi": "blocked",
    "gold": "blocked",
    "label_gold": "muted",
    # Cyan was the interaction accent; the accent role now owns that job.
    "cyan": "accent",
    "cyan_pale": "accent",
    "up": "up",
    "down": "down",
    "disabled_text": "faint",
    "disabled_border": "border",
    "disabled_bg": "surface",
    "allocation_track": "border",
    "chart_axis": "border",
    "flow_working_bg": "surface",
    "flow_queued_border": "border",
    "flow_queued_text": "faint",
    "flow_done_border": "up",
    "flow_done_text": "up",
    "flow_failed_border": "down",
    "flow_failed_text": "down",
    "flow_blocked_border": "blocked",
    "flow_blocked_text": "blocked",
    "queued_border": "border",
    "success_pale": "up",
    "success_bg": "surface",
    "success_border": "up",
    "danger_pale": "down",
    "danger_bg": "surface",
    "danger_border": "down",
    "warning_pale": "blocked",
    "warning_border": "blocked",
}

# Minimum WCAG contrast against both `bg` and `panel`. `faint` carries
# non-essential text (timestamps, hashes, key hints) and so targets 3.5 rather
# than 4.5; everything else here is read as data and targets AA or better.
CONTRAST_TARGETS = {
    "text": 7.0,
    "muted": 4.5,
    "faint": 3.5,
    "accent": 4.5,
    "up": 4.5,
    "down": 4.5,
    "blocked": 4.5,
}

_DARK = {
    # Lifted off pure black on purpose: on a true-black field every chromatic
    # value reads as neon.
    "bg": "#101419",
    "surface": "#161b22",
    "panel": "#1d242e",
    "border": "#262f3a",
    "selection": "#22303f",
    "text": "#c6d0da",
    "text_strong": "#eef4fa",
    "muted": "#8895a4",
    "faint": "#76828f",
    "accent": "#86aed4",
    "up": "#6ed3a6",
    "down": "#cf6b7d",
    "blocked": "#d9a55a",
}

_LIGHT = {
    # Not pure white: white leaves no room for a legible muted ramp. Hues are
    # held constant from the dark theme, then darkened and desaturated; dark
    # values are never reused on a light field.
    "bg": "#f7f9fb",
    "surface": "#eef2f6",
    "panel": "#e4eaf1",
    "border": "#d0d9e2",
    "selection": "#d3e0ec",
    "text": "#1b242e",
    "text_strong": "#0b1218",
    "muted": "#54626f",
    "faint": "#6e7b88",
    "accent": "#2d5f8c",
    "up": "#0b6b4e",
    "down": "#9c2a40",
    "blocked": "#8a5a12",
}

# Colour-vision-safe variants: blue/orange replaces green/red and the accent
# steps back to neutral slate so it cannot be mistaken for the positive sign.
#
# `up` is a pale blue rather than a mid blue because orange sits naturally high
# in luminance: a mid blue against this orange separates by only 1.23:1, and
# deepening the orange instead drops it under the 4.5 text target on a raised
# panel. Lifting the blue is the only move that satisfies both.
_CVD_DARK = dict(_DARK, up="#bcdcf5", down="#d98428", accent="#9aa7b4")
_CVD_LIGHT = dict(_LIGHT, up="#1f5f96", down="#8f4e07", accent="#5a6672")

ROLES = {
    "qlab-dark": _DARK,
    "qlab-light": _LIGHT,
    "qlab-cvd-dark": _CVD_DARK,
    "qlab-cvd-light": _CVD_LIGHT,
}

DEFAULT_THEME = "qlab-dark"

_DARK_THEMES = frozenset({"qlab-dark", "qlab-cvd-dark"})


def relative_luminance(colour: str) -> float:
    """WCAG relative luminance of an ``#rrggbb`` string."""
    raw = colour.lstrip("#")
    if len(raw) != 6:
        raise ValueError(f"expected #rrggbb, got {colour!r}")
    channels = []
    for offset in (0, 2, 4):
        value = int(raw[offset:offset + 2], 16) / 255
        channels.append(
            value / 12.92 if value <= 0.03928
            else ((value + 0.055) / 1.055) ** 2.4
        )
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG contrast ratio; symmetric in its arguments, bounded by 21:1."""
    first = relative_luminance(foreground)
    second = relative_luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def _build(name: str, roles: dict[str, str]) -> Theme:
    """Bind semantic roles onto a Textual theme.

    Roles that Textual has no slot for are exposed as CSS variables, so stylesheets
    reference `$text-muted` and `$border-quiet` instead of a literal.
    """
    return Theme(
        name=name,
        dark=name in _DARK_THEMES,
        background=roles["bg"],
        surface=roles["surface"],
        panel=roles["panel"],
        foreground=roles["text"],
        primary=roles["accent"],
        secondary=roles["muted"],
        accent=roles["accent"],
        success=roles["up"],
        error=roles["down"],
        warning=roles["blocked"],
        variables={
            "text-muted": roles["muted"],
            "text-faint": roles["faint"],
            "text-strong": roles["text_strong"],
            "border-quiet": roles["border"],
            "state-up": roles["up"],
            "state-down": roles["down"],
            "state-blocked": roles["blocked"],
            "state-working": roles["accent"],
            # The modal scrim is the one alias needing alpha, so it is built
            # here rather than carried as a role.
            "overlay": roles["bg"] + "cc",
            **{alias: roles[target] for alias, target in _ALIASES.items()},
        },
    )


THEMES = {name: _build(name, roles) for name, roles in ROLES.items()}


def role(theme_name: str, role_name: str) -> str:
    """Resolve a semantic role to its colour for one theme."""
    try:
        return ROLES[theme_name][role_name]
    except KeyError as exc:
        raise KeyError(f"unknown theme/role: {theme_name}/{role_name}") from exc
