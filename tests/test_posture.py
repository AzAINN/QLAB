"""The persisted armed/read-only posture. Offline; state root is isolated by
the autouse ``isolated_state_root`` fixture in conftest."""

from __future__ import annotations

from qlab.core.posture import (
    DEFAULT_POSTURE, Posture, load_posture, save_posture)
from qlab.paths import state_path


def test_a_desk_never_asked_has_not_chosen():
    assert load_posture() is None          # absence, not an error
    assert DEFAULT_POSTURE.armed is False  # the safe answer is the default


def test_a_choice_survives_the_owner():
    save_posture(Posture(armed=True))
    assert load_posture() == Posture(armed=True)


def test_a_read_only_choice_is_a_choice_not_an_absence():
    """The other side of the comparison: False persisted still reads as chosen."""
    save_posture(Posture(armed=False))
    assert load_posture() == Posture(armed=False)
    assert load_posture() is not None


def test_an_unreadable_file_reads_as_not_chosen():
    path = state_path("posture.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert load_posture() is None          # never an exception


def test_a_non_boolean_armed_on_disk_reads_as_not_chosen():
    path = state_path("posture.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"armed": "yes"}')
    assert load_posture() is None
