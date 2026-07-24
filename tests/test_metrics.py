from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from qlab.core.metrics import (
    compute_metrics,
    downside_capture,
    downside_deviation,
    omega_ratio,
    upside_capture,
)


def test_downside_deviation_is_root_mean_squared_shortfall():
    returns = pd.Series([-0.03, 0.02, -0.04, 0.01])
    assert downside_deviation(returns) == pytest.approx(0.025)

    targeted = pd.Series([0.01, 0.03, -0.01])
    assert downside_deviation(targeted, target=0.01) == pytest.approx(
        np.sqrt(0.0004 / 3.0)
    )


def test_omega_ratio_is_gain_over_shortfall_and_infinite_without_losses():
    returns = pd.Series([-0.10, -0.05, 0.0, 0.05, 0.20])
    assert omega_ratio(returns) == pytest.approx(0.25 / 0.15)
    assert np.isinf(omega_ratio(pd.Series([0.0, 0.01, 0.02])))


def test_capture_ratios_use_benchmark_up_and_down_periods():
    returns = pd.Series([0.03, 0.01, -0.01, -0.02])
    benchmark = pd.Series([0.02, 0.04, -0.02, -0.01])

    assert upside_capture(returns, benchmark) == pytest.approx(2.0 / 3.0)
    assert downside_capture(returns, benchmark) == pytest.approx(1.0)


def test_capture_ratios_fail_loud_without_required_benchmark_regime():
    with pytest.raises(ValueError, match="positive benchmark"):
        upside_capture([0.01, -0.01], [-0.02, -0.01])
    with pytest.raises(ValueError, match="negative benchmark"):
        downside_capture([0.01, 0.02], [0.02, 0.01])


def test_standard_metric_bundle_adds_pmpt_metrics():
    returns = pd.Series([-0.02, 0.01, 0.03, -0.01])
    metrics = compute_metrics(returns)

    assert metrics["downside_deviation"] == pytest.approx(
        downside_deviation(returns)
    )
    assert metrics["omega_ratio"] == pytest.approx(omega_ratio(returns))
