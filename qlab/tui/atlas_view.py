"""Master-detail catalog view: what the desk is made of, champion marked."""

from __future__ import annotations

import math

from rich.markup import escape
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Label, ListItem, ListView, Static

from qlab.tui.theme import AMBER, DIM, LABEL_GOLD, MUTED, TEXT, TEXT_HI

_GROUP_TITLES = (
    ("arm", "RESEARCH ARMS"),
    ("metric", "METRICS"),
    ("role", "WORKFORCE ROLES"),
    ("governance", "GOVERNANCE"),
)
# The ablation payload is a full compute_metrics bundle (13 keys, n_obs first).
# The overlay shows the same five the owner's leaderboard ranks on, in reading
# order, so the champion's line stays one glance instead of a metric dump.
_OVERLAY_METRICS = (
    "sharpe", "ann_return", "max_drawdown", "cvar_95", "deflated_sharpe",
)


def _overlay_cells(ablation: object) -> str:
    """Curated metrics as markup cells; absent or non-finite values are dropped."""
    if not isinstance(ablation, dict):
        return ""
    cells = []
    for key in _OVERLAY_METRICS:
        value = ablation.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if not math.isfinite(value):
            continue  # a NaN is not a number to report
        cells.append(f"[{LABEL_GOLD}]{key}[/] {value:.3f}")
    return "  ".join(cells)


class AtlasView(Vertical):
    """Grouped index on the left, one entry's prose and live facts on the right."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # Row index -> entry dict; None rows are group headers.
        self._row_entries: list[dict | None] = []

    def compose(self):
        yield Static(f"[{AMBER}]▍[/] ATLAS", classes="canvas-title",
                     markup=True)
        with Horizontal(id="atlas-split"):
            yield ListView(id="atlas-list")
            yield VerticalScroll(
                Static(
                    f"[{MUTED}]Waiting for the owner's atlas payload…[/]",
                    id="atlas-detail", markup=True),
                id="atlas-detail-scroll")

    def set_entries(self, entries: list[dict]) -> None:
        """Rebuild the index from an owner payload and show its first entry."""
        rows: list[dict | None] = []
        items = []
        for group, group_title in _GROUP_TITLES:
            members = [e for e in entries if e.get("group") == group]
            if not members:
                continue
            items.append(ListItem(
                Label(f"[{DIM}]{group_title}[/]", markup=True),
                disabled=True))
            rows.append(None)
            for entry in members:
                star = f" [{AMBER}]★[/]" if entry.get("champion") else ""
                items.append(ListItem(Label(
                    f"[{TEXT}]{escape(entry['title'])}[/]{star}",
                    markup=True)))
                rows.append(entry)
        view = self.query_one("#atlas-list", ListView)

        async def rebuild() -> None:
            # ListView.clear() only schedules the removal, so the mount has to
            # wait for it — otherwise a second payload maps its row indices onto
            # rows that are still on screen.
            await view.clear()
            self._row_entries = rows
            await view.extend(items)
            first = next(
                (index for index, entry in enumerate(rows) if entry), None)
            if first is None:
                return
            # The highlight has to start on the entry the detail pane is showing,
            # otherwise arrow keys walk from a row the reader never selected.
            view.index = first
            self._render_detail(rows[first])

        self.run_worker(rebuild(), group="atlas-rebuild", exclusive=True)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        index = event.list_view.index
        if index is None or index >= len(self._row_entries):
            return
        entry = self._row_entries[index]
        if entry:
            self._render_detail(entry)

    def _render_detail(self, entry: dict) -> None:
        title = f"[bold {TEXT_HI}]{escape(entry['title'])}[/]"
        if entry.get("subtitle"):
            title += f"  [{MUTED}]{escape(entry['subtitle'])}[/]"
        if entry.get("champion"):
            title += f"  [{AMBER}]★ CHAMPION[/]"
        parts = [title]
        if entry.get("stage"):
            parts.append(f"[{LABEL_GOLD}]stage[/] [{TEXT}]{entry['stage']}[/]")
        parts.extend(["", f"[{TEXT}]{escape(entry['body'])}[/]"])
        if entry.get("group") == "arm":
            parts.append("")
            cells = _overlay_cells(entry.get("ablation"))
            if cells:
                parts.append(f"latest ablation  {cells}")
            else:
                # Honest absence: no evidence is stated, never implied by a blank
                # or by a header with nothing reportable under it.
                parts.append(f"[{MUTED}]no ablation recorded for this arm yet[/]")
        if entry.get("arm_id"):
            parts.extend(["", f"[{DIM}]ablation id: {entry['arm_id']}[/]"])
        self.query_one("#atlas-detail", Static).update("\n".join(parts))
