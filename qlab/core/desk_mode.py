"""What the desk is pointed at: which data, and whose book.

Kept explicit rather than inferred from whether credentials happen to exist —
otherwise discovering an Alpaca login on disk would silently route an operator
who only wanted to look at synthetic data to their real paper account.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Literal

from qlab.paths import state_path

_STATE_FILE = "desk_mode.json"
_DATA = ("synthetic", "live")
_BOOK = ("simulated", "alpaca")


@dataclass(frozen=True)
class DeskMode:
    data: Literal["synthetic", "live"]
    book: Literal["simulated", "alpaca"]

    def __post_init__(self) -> None:
        if self.data not in _DATA:
            raise ValueError(f"unknown data source {self.data!r}")
        if self.book not in _BOOK:
            raise ValueError(f"unknown book {self.book!r}")
        if self.data == "synthetic" and self.book != "simulated":
            raise ValueError(
                "synthetic data cannot trade the Alpaca book; a synthetic desk "
                "always uses the simulated book")

    @property
    def offline(self) -> bool:
        return self.data == "synthetic"

    @property
    def label(self) -> str:
        if self.data == "synthetic":
            return "SYNTHETIC"
        return "LIVE · ALPACA BOOK" if self.book == "alpaca" else "LIVE · SIM BOOK"


DEFAULT_DESK_MODE = DeskMode("synthetic", "simulated")


def load_desk_mode() -> DeskMode | None:
    """The persisted choice, or None when absent or unusable.

    An unreadable or unrecognised file is treated as "not chosen yet" rather
    than an error: the operator is about to be asked anyway, and refusing to
    start the desk over a scratch file would be worse than re-asking.
    """
    path = state_path(_STATE_FILE)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return DeskMode(str(raw["data"]), str(raw["book"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_desk_mode(mode: DeskMode) -> None:
    path = state_path(_STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(mode), indent=2), encoding="utf-8")
