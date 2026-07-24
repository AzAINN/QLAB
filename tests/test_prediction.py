"""Leakage and admission tests for the research-stage vol baseline."""

from __future__ import annotations

import numpy as np
import pandas as pd

from qlab.algorithms import get_algorithm
from qlab.research.prediction import (
    IC_ADMISSION_THRESHOLD,
    IC_STABILITY_THRESHOLD,
    PREDICTION_HORIZON_DAYS,
    predict_vol_ridge,
    purged_walk_forward_splits,
)


def _persistent_vol_panel(
    *,
    seed: int = 11,
    n_obs: int = 1600,
    n_assets: int = 6,
) -> pd.DataFrame:
    """Common GARCH-ish variance makes lagged risk genuinely predictive."""
    rng = np.random.default_rng(seed)
    variance = np.empty(n_obs)
    common_return = np.empty(n_obs)
    common_shock = rng.normal(size=n_obs)
    idiosyncratic_shock = rng.normal(size=(n_obs, n_assets))
    variance[0] = 0.0001
    for day in range(n_obs):
        if day:
            variance[day] = (
                0.000002
                + 0.12 * common_return[day - 1] ** 2
                + 0.86 * variance[day - 1]
            )
        common_return[day] = np.sqrt(variance[day]) * common_shock[day]
    values = (
        0.7 * common_return[:, None]
        + 0.3 * np.sqrt(variance)[:, None] * idiosyncratic_shock
    )
    return pd.DataFrame(
        values,
        index=pd.bdate_range("2018-01-01", periods=n_obs),
        columns=[f"asset_{index}" for index in range(n_assets)],
    )


def test_persistent_volatility_clears_the_strict_ic_admission_gate() -> None:
    result = predict_vol_ridge(_persistent_vol_panel())

    assert result["mean_ic"] > IC_ADMISSION_THRESHOLD
    assert result["ic_stability"] > IC_STABILITY_THRESHOLD
    assert result["usable"] is True
    assert result["target"] == "next_21d_equal_weight_realized_vol"
    assert result["chosen_alpha"] in {0.1, 1.0, 10.0}
    assert len(result["per_fold"]) == 5


def test_iid_noise_is_not_admitted_as_a_usable_prediction() -> None:
    rng = np.random.default_rng(29)
    panel = pd.DataFrame(
        rng.normal(0.0, 0.01, size=(1600, 6)),
        index=pd.bdate_range("2018-01-01", periods=1600),
        columns=[f"asset_{index}" for index in range(6)],
    )

    result = predict_vol_ridge(panel)

    assert result["usable"] is False
    assert (
        result["mean_ic"] <= IC_ADMISSION_THRESHOLD
        or result["ic_stability"] <= IC_STABILITY_THRESHOLD
    )


def test_walk_forward_folds_are_expanding_and_structurally_embargoed() -> None:
    folds = purged_walk_forward_splits(600)
    previous_train_size = 0

    for train, test in folds:
        assert train[0] == 0
        assert len(train) > previous_train_size
        assert np.all(np.diff(train) == 1)
        assert np.all(np.diff(test) == 1)
        assert len(
            np.arange(train[-1] + 1, test[0])
        ) == PREDICTION_HORIZON_DAYS
        assert train[-1] + PREDICTION_HORIZON_DAYS < test[0]
        assert not np.isin(train, np.arange(
            test[0] - PREDICTION_HORIZON_DAYS,
            test[0],
        )).any()
        previous_train_size = len(train)


def test_prediction_catalog_entry_is_research_only() -> None:
    spec = get_algorithm("vol_prediction_ridge")

    assert spec.category == "prediction"
    assert spec.stage == "research"
    assert spec.objective_forms == ("risk_forecast",)
    assert spec.agent_tool == "research.predict_vol"
    assert spec.agent_usable is False
