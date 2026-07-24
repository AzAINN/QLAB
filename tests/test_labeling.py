"""Unit tests for the research-only labeling and sizing scaffold."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from qlab.algorithms import get_algorithm
from qlab.research.labeling import (
    confidence_to_size,
    meta_labels,
    triple_barrier_labels,
)


def _price_path(future: list[float]) -> tuple[pd.Series, pd.Timestamp]:
    index = pd.bdate_range("2026-01-05", periods=3 + len(future))
    prices = pd.Series([100.0, 101.0, 100.0, *future], index=index)
    return prices, index[2]


@pytest.mark.parametrize(
    ("future", "expected_label", "touch_offset", "expected_return"),
    [
        ([102.0, 97.0, 100.0], 1, 1, 0.02),
        ([98.0, 103.0, 100.0], -1, 1, -0.02),
        ([100.5, 100.4, 100.3], 0, 3, 0.003),
    ],
    ids=["up-first", "down-first", "vertical-expiry"],
)
def test_triple_barrier_first_touch_is_known(
    future: list[float],
    expected_label: int,
    touch_offset: int,
    expected_return: float,
) -> None:
    prices, event = _price_path(future)

    labels = triple_barrier_labels(
        prices,
        [event],
        k_up=1.0,
        k_down=1.0,
        horizon_days=3,
        vol_lookback=2,
    )

    assert labels.loc[event, "label"] == expected_label
    assert labels.loc[event, "touch_date"] == prices.index[2 + touch_offset]
    assert labels.loc[event, "return_at_touch"] == pytest.approx(
        expected_return
    )


def test_asymmetric_three_to_one_barrier_changes_the_first_touch() -> None:
    prices, event = _price_path([102.0, 98.0, 100.0])

    labels = triple_barrier_labels(
        prices,
        [event],
        k_up=3.0,
        k_down=1.0,
        horizon_days=3,
        vol_lookback=2,
    )

    assert labels.loc[event, "label"] == -1
    assert labels.loc[event, "touch_date"] == prices.index[4]
    assert labels.loc[event, "return_at_touch"] == pytest.approx(-0.02)


def test_meta_labels_require_agreement_and_side_adjusted_profit() -> None:
    index = pd.bdate_range("2026-02-02", periods=6)
    realized = pd.DataFrame(
        {
            "label": [1, -1, 1, -1, 0, 1],
            "touch_date": index,
            "return_at_touch": [0.10, -0.08, -0.03, 0.04, 0.02, 0.05],
        },
        index=index,
    )
    side_by_event = {
        index[0]: 1,
        index[1]: -1,
        index[2]: 1,
        index[3]: -1,
        index[4]: 0,
        index[5]: -1,
    }
    primary_side = pd.Series(
        [side_by_event[event] for event in reversed(index)],
        index=index[::-1],
    )

    labels, summary = meta_labels(primary_side, realized)

    expected = pd.Series(
        [1, 1, 0, 0, 0, 0],
        index=index,
        name="meta_label",
    )
    pd.testing.assert_series_equal(labels, expected)
    assert summary == {"hit_rate": pytest.approx(1.0 / 3.0), "n": 6}


@pytest.mark.parametrize(
    ("confidence", "scheme", "expected"),
    [
        (0.4, "linear", 0.4),
        (0.59, "threshold", 0.0),
        (0.6, "threshold", 0.6),
        (0.5, "convex", 0.25),
    ],
)
def test_confidence_to_size_scalar_schemes(
    confidence: float,
    scheme: str,
    expected: float,
) -> None:
    assert confidence_to_size(confidence, scheme) == pytest.approx(expected)


def test_confidence_to_size_preserves_series_alignment_and_clamps() -> None:
    confidence = pd.Series(
        [0.1, 0.6, 0.95],
        index=["low", "middle", "high"],
        name="confidence",
    )

    sized = confidence_to_size(
        confidence,
        "convex",
        floor=0.05,
        cap=0.8,
    )

    expected = pd.Series(
        [0.05, 0.36, 0.8],
        index=confidence.index,
        name="confidence",
    )
    pd.testing.assert_series_equal(sized, expected)
    assert confidence_to_size(
        0.1, "linear", floor=0.2, cap=0.8
    ) == pytest.approx(0.2)
    assert confidence_to_size(
        0.9, "linear", floor=0.2, cap=0.8
    ) == pytest.approx(0.8)


def test_triple_barrier_refuses_short_or_incomplete_history() -> None:
    prices, event = _price_path([102.0, 101.0, 100.0])

    with pytest.raises(ValueError, match="insufficient history"):
        triple_barrier_labels(
            prices,
            [prices.index[1]],
            horizon_days=2,
            vol_lookback=2,
        )
    with pytest.raises(ValueError, match="insufficient forward history"):
        triple_barrier_labels(
            prices,
            [prices.index[-2]],
            horizon_days=2,
            vol_lookback=2,
        )


def test_triple_barrier_refuses_nan_prices() -> None:
    prices, event = _price_path([102.0, 101.0, 100.0])
    prices.iloc[1] = np.nan

    with pytest.raises(ValueError, match="complete and finite"):
        triple_barrier_labels(
            prices,
            [event],
            horizon_days=3,
            vol_lookback=2,
        )


def test_meta_labeling_catalog_entry_is_visible_but_not_staged() -> None:
    spec = get_algorithm("meta_labeling_scaffold")

    assert spec.category == "prediction"
    assert spec.stage == "research"
    assert spec.objective_forms == ("meta_labeling",)
    assert spec.solver is None
    assert spec.agent_tool is None
    assert spec.agent_usable is False
    assert spec.description == (
        "labeling + sizing scaffold for the future prediction lane; no signal, "
        "no execution path"
    )
