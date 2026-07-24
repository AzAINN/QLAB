"""Master-detail catalog view: what the desk is made of, champion marked."""

from __future__ import annotations

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
        view = self.query_one("#atlas-list", ListView)
        view.clear()
        self._row_entries = []
        items = []
        for group, group_title in _GROUP_TITLES:
            members = [e for e in entries if e.get("group") == group]
            if not members:
                continue
            items.append(ListItem(
                Label(f"[{DIM}]{group_title}[/]", markup=True),
                disabled=True))
            self._row_entries.append(None)
            for entry in members:
                star = f" [{AMBER}]★[/]" if entry.get("champion") else ""
                items.append(ListItem(Label(
                    f"[{TEXT}]{escape(entry['title'])}[/]{star}",
                    markup=True)))
                self._row_entries.append(entry)
        view.extend(items)
        first = next(
            (index for index, entry in enumerate(self._row_entries) if entry),
            None)
        if first is None:
            return
        # The highlight has to start on the entry the detail pane is showing,
        # otherwise arrow keys walk from a row the reader never selected.
        view.index = first
        self._render_detail(self._row_entries[first])

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
            ablation = entry.get("ablation")
            if ablation:
                cells = "  ".join(
                    f"[{LABEL_GOLD}]{key}[/] {value:.3f}"
                    for key, value in sorted(ablation.items())
                    if isinstance(value, (int, float)))
                parts.append(f"latest ablation  {cells}")
            else:
                # Honest absence: no evidence is stated, never implied by a blank.
                parts.append(f"[{MUTED}]no ablation recorded for this arm yet[/]")
        if entry.get("arm_id"):
            parts.extend(["", f"[{DIM}]ablation id: {entry['arm_id']}[/]"])
        self.query_one("#atlas-detail", Static).update("\n".join(parts))
