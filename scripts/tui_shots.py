"""Render every TUI view to PNG so a design pass can actually look at it.

Textual only emits SVG, which an agent cannot see; `resvg` rasterises it so the
images can be read back. Nothing here touches `.lab/registry.duckdb` — the app
is driven against an in-memory `UISession`, the same way the TUI tests are, so
this is safe to run while an owner is up.

    python scripts/tui_shots.py                       # every view, default theme
    python scripts/tui_shots.py --views atlas market  # just these
    python scripts/tui_shots.py --themes all          # every theme
    python scripts/tui_shots.py --size 200x60         # wider terminal

Output lands in `.lab/shots/<theme>/<view>.png` (gitignored). Each run rewrites
its own files so a before/after comparison means copying the directory first.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qlab.tui.app import _VIEWS, QlabTui  # noqa: E402
from qlab.tui.design import tokens  # noqa: E402

# The app settles asynchronously: the first snapshot arrives on a worker thread
# and several tiles only paint once it lands. Screenshotting before that gives a
# picture of the loading state rather than the interface under review.
_SETTLE_S = 0.6
_VIEW_SWITCH_S = 0.25


def _client():
    """An in-process owner backed by an in-memory registry.

    Imported from the test suite deliberately: a second stub would drift from
    the contract the tests pin, and these images would then show a desk that
    cannot occur.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from test_tui import InProcessClient

    return InProcessClient()


async def _shoot(views, theme, size, out_dir):
    app = QlabTui(_client(), refresh_interval=0, claude_start="off")
    written = []
    async with app.run_test(size=size) as pilot:
        if theme != tokens.DEFAULT_THEME:
            app.action_theme(theme)
        await pilot.pause(_SETTLE_S)
        for view in views:
            app.action_view(view)
            await pilot.pause(_VIEW_SWITCH_S)
            svg = app.export_screenshot(title=f"qlab · {view} · {theme}")
            written.append((view, svg))
    return written


def _rasterise(svg: str, target: Path) -> None:
    import resvg_py

    png = resvg_py.svg_to_bytes(svg_string=svg)
    target.write_bytes(bytes(png))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--views", nargs="*", default=None,
                        help=f"subset of: {' '.join(_VIEWS)}")
    parser.add_argument("--themes", nargs="*", default=[tokens.DEFAULT_THEME],
                        help="theme names, or 'all'")
    parser.add_argument("--size", default="180x50", help="COLSxROWS")
    parser.add_argument("--out", default=".lab/shots")
    args = parser.parse_args()

    views = args.views or list(_VIEWS)
    unknown = [v for v in views if v not in _VIEWS]
    if unknown:
        parser.error(f"unknown view(s): {unknown}; known: {list(_VIEWS)}")

    themes = list(tokens.THEMES) if args.themes == ["all"] else args.themes
    unknown = [t for t in themes if t not in tokens.THEMES]
    if unknown:
        parser.error(f"unknown theme(s): {unknown}; known: {list(tokens.THEMES)}")

    cols, _, rows = args.size.partition("x")
    size = (int(cols), int(rows))

    root = Path(__file__).resolve().parents[1]
    for theme in themes:
        out_dir = root / args.out / theme
        out_dir.mkdir(parents=True, exist_ok=True)
        shots = asyncio.run(_shoot(views, theme, size, out_dir))
        for view, svg in shots:
            target = out_dir / f"{view}.png"
            _rasterise(svg, target)
            print(f"{target.relative_to(root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
