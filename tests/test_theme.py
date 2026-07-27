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


def test_rendered_css_uses_shared_tokens():
    assert theme.BG in theme.APP_CSS
    assert theme.AMBER in theme.APP_CSS
    assert "$" not in theme.APP_CSS
    assert all((
        theme.APP_CSS,
        theme.PAPER_MODAL_CSS,
        theme.WORKFORCE_MODAL_CSS,
    ))


def test_state_style_covers_every_workflow_state():
    assert set(theme.STATE_STYLE) == {
        "working", "queued", "waiting", "done", "failed", "blocked", "idle",
        "interrupted", "abandoned",
    }


def test_app_source_has_no_literal_six_digit_hex():
    assert re.search(r"#[0-9a-fA-F]{6}", APP_PATH.read_text(encoding="utf-8")) is None
