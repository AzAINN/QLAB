"""Pure realized-risk calibration scoring."""

from __future__ import annotations

import numpy as np
import pytest

from qlab.news.calibration import (
    ViewScore,
    calibration_summary,
    reliability_score,
    view_realization,
)


def _tail_panel(n_tail: int, n_obs: int = 100) -> np.ndarray:
    values = np.zeros(n_obs)
    half = n_tail // 2
    values[:half] = 1.0
    values[half:n_tail] = -1.0
    return values[:, None]


def test_fatter_tail_view_scores_true_for_fatter_realization():
    score = view_realization(
        "tail",
        {
            "ticker": "AAA",
            "direction": "fatter",
            "pre_view_tail_mass": 0.05,
        },
        _tail_panel(n_tail=10),
        ["AAA"],
    )

    assert score.view_label == "tail(AAA fatter)"
    assert score.direction_correct is True
    assert score.realized_value == pytest.approx(0.10)
    assert score.expected_direction == "fatter"
    assert 0.0 < score.magnitude_score <= 1.0


def test_fatter_tail_view_scores_false_for_calmer_realization():
    score = view_realization(
        "tail",
        {
            "ticker": "AAA",
            "direction": "fatter",
            "pre_view_tail_mass": 0.05,
        },
        _tail_panel(n_tail=2),
        ["AAA"],
    )

    assert score.direction_correct is False
    assert score.realized_value == pytest.approx(0.02)
    assert -1.0 <= score.magnitude_score < 0.0


def test_vol_up_view_distinguishes_realized_up_from_down():
    payload = {
        "ticker": "AAA",
        "target_vol": 2.0,
        "pre_view_vol": 1.0,
    }
    realized_up = np.array([[-2.0], [2.0], [-2.0], [2.0]])
    realized_down = np.array([[-0.5], [0.5], [-0.5], [0.5]])

    up = view_realization("vol", payload, realized_up, ["AAA"])
    down = view_realization("vol", payload, realized_down, ["AAA"])

    assert up.direction_correct is True
    assert up.expected_direction == "up"
    assert up.realized_value == pytest.approx(2.0)
    assert up.magnitude_score == pytest.approx(1.0)
    assert down.direction_correct is False
    assert down.realized_value == pytest.approx(0.5)
    assert down.magnitude_score == pytest.approx(-0.5)


def test_corr_view_scores_realized_pair_direction():
    payload = {
        "ticker_a": "AAA",
        "ticker_b": "BBB",
        "target_corr": 0.8,
        "pre_view_corr": 0.0,
    }
    left = np.array([-2.0, -1.0, 1.0, 2.0])
    realized_up = np.column_stack([left, left])
    realized_down = np.column_stack([left, -left])

    up = view_realization("corr", payload, realized_up, ["AAA", "BBB"])
    down = view_realization(
        "corr", payload, realized_down, ["AAA", "BBB"]
    )

    assert up.direction_correct is True
    assert up.realized_value == pytest.approx(1.0)
    assert up.magnitude_score == pytest.approx(1.0)
    assert down.direction_correct is False
    assert down.realized_value == pytest.approx(-1.0)
    assert down.magnitude_score == pytest.approx(-1.0)


def test_calibration_summary_and_reliability_aggregate():
    scores = [
        ViewScore("vol(AAA→0.0200)", True, 0.021, "up", 0.8),
        ViewScore("vol(BBB→0.0100)", False, 0.015, "down", -0.4),
        ViewScore("tail(AAA fatter)", True, 0.10, "fatter", 0.2),
    ]

    summary = calibration_summary(scores)

    assert summary == {
        "hit_rate": pytest.approx(2.0 / 3.0),
        "mean_magnitude": pytest.approx(0.2),
        "n": 3,
        "by_view_kind": {
            "vol": {
                "hit_rate": pytest.approx(0.5),
                "mean_magnitude": pytest.approx(0.2),
                "n": 2,
            },
            "tail": {
                "hit_rate": pytest.approx(1.0),
                "mean_magnitude": pytest.approx(0.2),
                "n": 1,
            },
        },
    }
    assert reliability_score(scores) == pytest.approx(2.0 / 3.0)


@pytest.mark.parametrize(
    ("kind", "payload", "panel", "tickers", "message"),
    [
        (
            "return",
            {"ticker": "AAA", "target_return": 0.1},
            [[0.0], [0.1]],
            ["AAA"],
            "view_kind",
        ),
        (
            "vol",
            {"ticker": "AAA", "target_vol": 0.2},
            [[0.0], [0.1]],
            ["AAA"],
            "baseline",
        ),
        (
            "vol",
            {
                "ticker": "AAA",
                "target_vol": 0.2,
                "pre_view_vol": 0.1,
            },
            [0.0, 0.1],
            ["AAA"],
            "T x N",
        ),
        (
            "vol",
            {
                "ticker": "AAA",
                "target_vol": 0.2,
                "pre_view_vol": 0.1,
            },
            [[0.0], [np.nan]],
            ["AAA"],
            "non-finite",
        ),
        (
            "corr",
            {
                "ticker_a": "AAA",
                "ticker_b": "BBB",
                "target_corr": 0.5,
                "pre_view_corr": 0.0,
            },
            [[0.0, 1.0], [0.0, 2.0]],
            ["AAA", "BBB"],
            "constant",
        ),
        (
            "tail",
            {
                "ticker": "AAA",
                "direction": "wide",
                "pre_view_tail_mass": 0.05,
            },
            [[0.0], [1.0]],
            ["AAA"],
            "direction",
        ),
    ],
)
def test_view_realization_rejects_malformed_inputs(
    kind, payload, panel, tickers, message
):
    with pytest.raises((TypeError, ValueError), match=message):
        view_realization(kind, payload, panel, tickers)


def test_summary_rejects_malformed_scores_and_empty_reliability():
    with pytest.raises(TypeError, match="ViewScore"):
        calibration_summary([object()])
    with pytest.raises(ValueError, match="at least one"):
        reliability_score([])
