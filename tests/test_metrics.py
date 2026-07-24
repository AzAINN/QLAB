from __future__ import annotations

import json

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


def test_omega_ratio_is_gain_over_shortfall_and_none_without_losses():
    returns = pd.Series([-0.10, -0.05, 0.0, 0.05, 0.20])
    assert omega_ratio(returns) == pytest.approx(0.25 / 0.15)
    assert omega_ratio(pd.Series([0.0, 0.01, 0.02])) is None
    assert omega_ratio(pd.Series([0.0, 0.0, 0.0])) is None


def test_capture_ratios_use_benchmark_up_and_down_periods():
    returns = pd.Series([0.03, 0.01, -0.01, -0.02])
    benchmark = pd.Series([0.02, 0.04, -0.02, -0.01])

    assert upside_capture(returns, benchmark) == pytest.approx(2.0 / 3.0)
    assert downside_capture(returns, benchmark) == pytest.approx(1.0)


def test_capture_ratios_are_none_without_required_benchmark_regime():
    assert upside_capture([0.01, -0.01], [-0.02, -0.01]) is None
    assert downside_capture([0.01, 0.02], [0.02, 0.01]) is None


def test_standard_metric_bundle_adds_pmpt_metrics():
    returns = pd.Series([-0.02, 0.01, 0.03, -0.01])
    metrics = compute_metrics(returns)

    assert metrics["downside_deviation"] == pytest.approx(
        downside_deviation(returns)
    )
    assert metrics["omega_ratio"] == pytest.approx(omega_ratio(returns))


def test_metric_bundle_is_strict_json_when_omega_is_undefined():
    metrics = compute_metrics(pd.Series([0.005, 0.01, 0.015, 0.02]))

    assert metrics["omega_ratio"] is None
    assert metrics["sortino"] == 0.0
    json.dumps(metrics, allow_nan=False)
