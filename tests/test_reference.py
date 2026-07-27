"""The curated component catalog stays in lockstep with the real system."""

from qlab.core.reference import (
    REFERENCE_ENTRIES, ARM_NAMES, arm_algorithm_key, arm_display_name)


def test_every_ablation_arm_has_a_display_name():
    assert set(ARM_NAMES) == {
        "B0", "B1", "B2", "B3", "B4", "A1", "A2", "A3", "A4", "A3t"}


def test_arm_algorithm_keys_exist_in_catalog():
    from qlab.algorithms import list_algorithms
    catalog_ids = {row["id"] for row in list_algorithms()}
    for entry in REFERENCE_ENTRIES:
        if entry.algorithm_key is not None:
            assert entry.algorithm_key in catalog_ids, entry.entry_id


def test_entry_ids_unique_and_content_nonempty():
    ids = [entry.entry_id for entry in REFERENCE_ENTRIES]
    assert len(ids) == len(set(ids))
    for entry in REFERENCE_ENTRIES:
        assert entry.group in {"arm", "metric", "role", "governance"}
        assert entry.title and entry.one_liner and entry.body


def test_unknown_arm_id_passes_through_unchanged():
    assert arm_display_name("Z9") == "Z9"
    assert arm_algorithm_key("Z9") is None


def test_display_names_carry_no_arm_codes():
    for arm_id, name in ARM_NAMES.items():
        assert arm_id not in name
