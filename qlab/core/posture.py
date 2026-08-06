"""Whether the desk is armed: read-only, or able to act on what it finds.

Posture is explicit, never inferred. A desk is armed because an operator said
so — not because a binary was launched with a flag that happened to be there,
and not because the credentials or the code to act exist. Absence of a stated
posture is "nobody has been asked yet", which is not the same as "read-only by
policy", and a client must be able to tell those apart.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from qlab.paths import state_path

_STATE_FILE = "posture.json"


@dataclass(frozen=True)
class Posture:
    armed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.armed, bool):
            raise ValueError(f"armed must be true or false, not {self.armed!r}")


#: What an unasked desk is served as. Named rather than open-coded so the one
#: place that answers for absence — ``UISession.posture_payload`` — says which
#: default it is falling back to. There was a ``Posture.label`` here too; it had
#: no caller in any client (both render their own words) and was deleted rather
#: than kept as a seam nothing walks (invariant 10).
DEFAULT_POSTURE = Posture(armed=False)


def load_posture() -> Posture | None:
    """The persisted choice, or None when absent or unusable.

    Same rule as ``load_desk_mode``: an unreadable or unrecognised file is
    "not chosen yet" rather than an error. The operator is about to be asked
    anyway, and the answer that absence falls back to is the safe one.
    """
    path = state_path(_STATE_FILE)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        armed = raw["armed"]
        if not isinstance(armed, bool):
            return None
        return Posture(armed)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_posture(posture: Posture) -> None:
    path = state_path(_STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(posture), indent=2), encoding="utf-8")
