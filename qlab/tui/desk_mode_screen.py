"""Startup choice: which data, and whose book.

Two steps in one screen. The book question only appears once LIVE is chosen, so
the nonsensical combination (synthetic data against the real paper account) is
unreachable by construction rather than rejected by a validation message.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from qlab.core.desk_mode import DeskMode
from qlab.tui.theme import DESK_MODAL_CSS


class DeskModeScreen(ModalScreen[DeskMode]):
    BINDINGS = [Binding("escape", "cancel", "Synthetic", show=False)]
    CSS = DESK_MODAL_CSS

    def __init__(self, credentials: str, credentials_ok: bool) -> None:
        super().__init__()
        self.credentials = credentials
        self.credentials_ok = credentials_ok
        self._data = "synthetic"
        self._book = "simulated"

    def compose(self) -> ComposeResult:
        with Vertical(id="desk-dialog"):
            yield Static("DATA SOURCE", id="desk-dialog-title")
            with Horizontal(id="desk-data-row"):
                yield Button("SYNTHETIC", id="desk-data-synthetic")
                yield Button("LIVE", id="desk-data-live",
                             disabled=not self.credentials_ok)
            # Owner text and exception reprs, not markup the TUI authored: a
            # bracketed word would be parsed as a tag and silently dropped.
            yield Static(self.credentials, id="desk-credentials", markup=False)
            with Vertical(id="desk-book-row"):
                yield Static("WHICH BOOK", id="desk-book-title")
                with Horizontal(id="desk-book-buttons"):
                    yield Button("SIMULATED", id="desk-book-simulated")
                    yield Button("ALPACA PAPER", id="desk-book-alpaca")
                yield Static(
                    "simulated uses real prices but never sends an order to "
                    "Alpaca. either way, executing a plan still needs your "
                    "explicit confirmation.",
                    id="desk-book-copy")
            with Horizontal(id="desk-actions"):
                yield Button("Start", id="desk-confirm", variant="warning")

    def on_mount(self) -> None:
        self._sync()

    def _sync(self) -> None:
        row = self.query_one("#desk-book-row")
        row.styles.display = "block" if self._data == "live" else "none"
        # Start applies whatever is selected now, so what is selected now has to
        # be visible — above all for the one button that reaches a real account.
        for widget_id, selected in (
            ("#desk-data-synthetic", self._data == "synthetic"),
            ("#desk-data-live", self._data == "live"),
            ("#desk-book-simulated", self._book == "simulated"),
            ("#desk-book-alpaca", self._book == "alpaca"),
        ):
            self.query_one(widget_id, Button).variant = (
                "primary" if selected else "default")

    def action_cancel(self) -> None:
        self.dismiss(DeskMode("synthetic", "simulated"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        pressed = event.button.id or ""
        if pressed == "desk-data-synthetic":
            self._data, self._book = "synthetic", "simulated"
        elif pressed == "desk-data-live" and self.credentials_ok:
            self._data = "live"
        elif pressed == "desk-book-simulated":
            self._book = "simulated"
        elif pressed == "desk-book-alpaca":
            self._book = "alpaca"
        elif pressed == "desk-confirm":
            self.dismiss(DeskMode(self._data, self._book))
            return
        self._sync()
