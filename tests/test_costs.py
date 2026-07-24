"""Pure transaction-cost model unit tests."""

from __future__ import annotations

import numpy as np
import pytest

from qlab.core.costs import cost_model


def test_cost_model_matches_hand_computed_components():
    result = cost_model(
        trade_notional=1_000_000.0,
        price=100.0,
        adv_notional=100_000_000.0,
        daily_vol=0.02,
        spread_bps=2.0,
        commission_bps=1.0,
        impact_k=1.0,
    )

    assert result["commission"] == pytest.approx(100.0)
    assert result["half_spread"] == pytest.approx(100.0)
    assert result["impact"] == pytest.approx(2_000.0)
    assert result["minimum_adjustment"] == 0.0
    assert result["total"] == pytest.approx(2_200.0)


def test_cost_model_applies_one_basis_point_minimum():
    result = cost_model(
        trade_notional=-250_000.0,
        price=50.0,
        adv_notional=50_000_000.0,
        daily_vol=0.0,
        spread_bps=0.0,
        commission_bps=0.0,
        impact_k=0.0,
    )

    assert result["minimum_adjustment"] == pytest.approx(25.0)
    assert result["total"] == pytest.approx(25.0)


def test_cost_model_is_vectorized():
    result = cost_model(
        trade_notional=np.array([100_000.0, 200_000.0]),
        price=np.array([100.0, 50.0]),
        adv_notional=50_000_000.0,
        daily_vol=np.array([0.01, 0.02]),
    )

    assert isinstance(result["total"], np.ndarray)
    assert result["total"].shape == (2,)
    np.testing.assert_allclose(
        result["total"],
        result["commission"]
        + result["half_spread"]
        + result["impact"]
        + result["minimum_adjustment"],
    )


def test_square_root_impact_scales_tenfold_for_one_hundredth_adv():
    mega = cost_model(
        1_000_000.0,
        100.0,
        10_000_000_000.0,
        0.02,
        spread_bps=0.0,
        commission_bps=0.0,
        impact_k=1.0,
    )
    small = cost_model(
        1_000_000.0,
        100.0,
        100_000_000.0,
        0.02,
        spread_bps=0.0,
        commission_bps=0.0,
        impact_k=1.0,
    )

    assert small["impact"] / mega["impact"] == pytest.approx(10.0)
    assert small["total"] / mega["total"] == pytest.approx(10.0)


@pytest.mark.parametrize(
    ("field", "kwargs"),
    [
        ("price", {"price": 0.0}),
        ("adv_notional", {"adv_notional": 0.0}),
        ("daily_vol", {"daily_vol": -0.01}),
        ("spread_bps", {"spread_bps": -1.0}),
    ],
)
def test_cost_model_rejects_invalid_inputs(field, kwargs):
    inputs = {
        "trade_notional": 100_000.0,
        "price": 100.0,
        "adv_notional": 50_000_000.0,
        "daily_vol": 0.02,
    }
    inputs.update(kwargs)

    with pytest.raises(ValueError, match=field):
        cost_model(**inputs)
