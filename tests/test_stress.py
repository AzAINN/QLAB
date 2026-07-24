"""Deterministic portfolio stress arithmetic."""

from __future__ import annotations

import pandas as pd
import pytest

from qlab.core.stress import replay_scenarios, stress_correlation_to_one


def test_correlation_to_one_stress_is_abs_weighted_vol_sum():
    stressed = stress_correlation_to_one(
        {"AAA": 0.60, "BBB": 0.40},
        {"AAA": 0.20, "BBB": 0.10},
    )

    assert stressed == pytest.approx(0.60 * 0.20 + 0.40 * 0.10)


def test_replay_scenarios_labels_synthetic_snapshot_unavailable():
    prices = pd.DataFrame(
        {"AAA": [100.0, 90.0], "BBB": [100.0, 105.0]},
        index=pd.to_datetime(["2008-09-01", "2022-10-12"]),
    )
    prices.attrs["source"] = "synthetic"
    prices.attrs["synthetic"] = True

    result = replay_scenarios({"AAA": 0.5, "BBB": 0.5}, prices)

    assert set(result) == {"2008", "2020", "2022"}
    assert all(row["available"] is False for row in result.values())
    assert all(row["return"] is None for row in result.values())
    assert all("synthetic" in row["reason"] for row in result.values())


def test_replay_scenario_uses_weighted_asset_window_returns():
    prices = pd.DataFrame(
        {"AAA": [100.0, 80.0], "BBB": [100.0, 110.0]},
        index=pd.to_datetime(["2020-02-19", "2020-03-23"]),
    )
    prices.attrs["source"] = "historical"

    result = replay_scenarios(
        {"AAA": 0.60, "BBB": 0.40},
        prices,
        windows={"shock": ("2020-02-19", "2020-03-23")},
    )

    assert result["shock"]["available"] is True
    assert result["shock"]["return"] == pytest.approx(-0.08)
