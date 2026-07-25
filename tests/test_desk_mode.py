"""The desk's data/book mode is an explicit, persisted operator decision."""

from __future__ import annotations

import pytest

from qlab.core.desk_mode import (
    DEFAULT_DESK_MODE, DeskMode, load_desk_mode, save_desk_mode)


def test_default_is_the_safe_offline_desk():
    assert DEFAULT_DESK_MODE == DeskMode("synthetic", "simulated")
    assert DEFAULT_DESK_MODE.offline is True


def test_live_data_can_carry_either_book():
    assert DeskMode("live", "simulated").offline is False
    assert DeskMode("live", "alpaca").offline is False


def test_synthetic_data_cannot_use_the_alpaca_book():
    # Unreachable via the UI's progressive disclosure; still refused in code so
    # a flag combination or a hand-edited state file cannot produce it.
    with pytest.raises(ValueError, match="synthetic"):
        DeskMode("synthetic", "alpaca")


def test_labels_distinguish_all_three_states():
    labels = {
        DeskMode("synthetic", "simulated").label,
        DeskMode("live", "simulated").label,
        DeskMode("live", "alpaca").label,
    }
    assert len(labels) == 3
    assert DeskMode("live", "alpaca").label != DeskMode("live", "simulated").label


def test_round_trips_through_the_state_file(tmp_path, monkeypatch):
    monkeypatch.setenv("QLAB_STATE_DIR", str(tmp_path))
    assert load_desk_mode() is None          # nothing chosen yet
    save_desk_mode(DeskMode("live", "alpaca"))
    assert load_desk_mode() == DeskMode("live", "alpaca")


def test_unreadable_or_unknown_state_falls_back_to_none(tmp_path, monkeypatch):
    monkeypatch.setenv("QLAB_STATE_DIR", str(tmp_path))
    (tmp_path / "desk_mode.json").write_text("{ not json", encoding="utf-8")
    assert load_desk_mode() is None
    (tmp_path / "desk_mode.json").write_text(
        '{"data": "wormhole", "book": "simulated"}', encoding="utf-8")
    assert load_desk_mode() is None
