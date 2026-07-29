"""Shared TUI theme contracts."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
THEME_PATH = ROOT / "qlab" / "tui" / "theme.py"
APP_PATH = ROOT / "qlab" / "tui" / "app.py"


def _load_theme():
    spec = importlib.util.spec_from_file_location("qlab_theme_test", THEME_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


theme = _load_theme()


def test_theme_imports_without_textual_or_rich_side_effects():
    script = f"""
import builtins
import importlib.util
import sys

original_import = builtins.__import__

def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name.partition(".")[0] in {{"rich", "textual"}}:
        raise AssertionError(f"unexpected UI import: {{name}}")
    return original_import(name, globals, locals, fromlist, level)

builtins.__import__ = guarded_import
spec = importlib.util.spec_from_file_location("qlab.tui.theme", {str(THEME_PATH)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert "rich" not in sys.modules
assert "textual" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_no_single_row_band_spends_its_only_row_on_a_border():
    # A one-row band has exactly one row for its text. Adding a border to it
    # consumes that row, and the band renders as an unlabelled strip — which
    # is what happened to every dashboard tile title and the whole agent rail
    # at once: EQUITY, ALLOCATION, MARKET PULSE, AGENTS and SELECTED WORK all
    # went blank while every test still passed, because no assertion reads a
    # heading.
    import re

    from qlab.tui.theme import APP_CSS

    # Each `selector { ... }` block, with its declarations.
    blocks = re.findall(r"([^{}]+)\{([^{}]*)\}", APP_CSS)
    offenders = []
    for selector, body in blocks:
        declarations = [d.strip() for d in body.split(";") if d.strip()]
        heights = [d for d in declarations if d.startswith("height:")]
        if heights and heights[-1].replace(" ", "") == "height:1":
            if any(d.startswith("border") and "none" not in d
                   for d in declarations):
                offenders.append(selector.strip().splitlines()[-1].strip())
    assert not offenders, (
        f"one-row bands whose border eats their text: {offenders}")


def test_rendered_css_references_shared_tokens_instead_of_baking_them():
    # The stylesheets deliberately keep `$token` references so a theme switch
    # repaints chrome; baking literals is what previously made the palette
    # fixed at import time. qlab.tui.design.tokens publishes every name as a
    # theme variable, and an unpublished one is a startup stylesheet error.
    import re

    assert "$bg" in theme.APP_CSS
    assert "$amber" in theme.APP_CSS
    assert theme.BG not in theme.APP_CSS

    for css in (theme.APP_CSS, theme.PAPER_MODAL_CSS, theme.ATLAS_DRAWER_CSS,
                theme.WORKFORCE_MODAL_CSS):
        assert css
        assert not re.search(r"#[0-9a-fA-F]{6}\b", css)
        for name in set(re.findall(r"\$([a-zA-Z0-9_]+)", css)):
            assert name in theme.TOKENS, f"CSS references unknown token ${name}"


def test_state_style_covers_every_workflow_state():
    assert set(theme.STATE_STYLE) == {
        "working", "queued", "waiting", "done", "failed", "blocked", "idle",
        "interrupted", "abandoned",
    }


def test_app_source_has_no_literal_six_digit_hex():
    assert re.search(r"#[0-9a-fA-F]{6}", APP_PATH.read_text(encoding="utf-8")) is None
