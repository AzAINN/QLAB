"""The predictor board: paired evaluation, honest admission, refusals.

The board's one structural promise is pairing — every model sees the same
purged folds — so its governing test is the dual/primal identity: a linear
kernel ridge must reproduce the primal baseline fold for fold.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from qlab.research.board import (
    BASELINE_MODEL_ID,
    MODEL_IDS,
    run_predictor_board,
)


def _persistent_vol_panel(
    *,
    seed: int = 11,
    n_obs: int = 700,
    n_assets: int = 5,
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
    index = pd.bdate_range("2018-01-02", periods=n_obs)
    return pd.DataFrame(
        values,
        index=index,
        columns=[f"A{i}" for i in range(n_assets)],
    )


def _noise_panel(*, seed: int = 42, n_obs: int = 700) -> pd.DataFrame:
    # Seed chosen so that luck does not admit a model: with a 21-day rolling
    # target a 78-row test block has ~4 effective observations, so noise ICs
    # swing to +/-0.5 across seeds. That swing is the point of admission.
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2018-01-02", periods=n_obs)
    return pd.DataFrame(
        rng.normal(0.0, 0.01, (n_obs, 5)),
        index=index,
        columns=[f"N{i}" for i in range(5)],
    )


# --- refusals -----------------------------------------------------------------


def test_the_board_requires_the_baseline():
    with pytest.raises(ValueError, match="baseline"):
        run_predictor_board(
            _persistent_vol_panel(), models=("kernel:linear",)
        )


def test_an_unknown_model_is_refused():
    with pytest.raises(ValueError, match="unknown model"):
        run_predictor_board(
            _persistent_vol_panel(),
            models=("ridge:none", "forest:deep"),
        )


def test_duplicate_models_are_refused():
    with pytest.raises(ValueError, match="unique"):
        run_predictor_board(
            _persistent_vol_panel(),
            models=("ridge:none", "ridge:none"),
        )


def test_an_empty_model_list_is_refused():
    with pytest.raises(ValueError, match="empty"):
        run_predictor_board(_persistent_vol_panel(), models=())


# --- pairing ------------------------------------------------------------------


def test_kernel_linear_matches_the_primal_baseline_per_fold():
    board = run_predictor_board(
        _persistent_vol_panel(),
        models=("ridge:none", "kernel:linear"),
    )
    by_id = {entry["model_id"]: entry for entry in board["models"]}
    baseline = by_id["ridge:none"]
    linear = by_id["kernel:linear"]
    baseline_ics = [fold["ic"] for fold in baseline["per_fold"]]
    linear_ics = [fold["ic"] for fold in linear["per_fold"]]
    np.testing.assert_allclose(linear_ics, baseline_ics, atol=1e-8)
    assert linear["paired_t_vs_baseline"] == 0.0
    assert linear["wins_vs_baseline"] == 0
    assert baseline["paired_t_vs_baseline"] is None


def test_the_board_is_deterministic_across_runs():
    panel = _persistent_vol_panel()
    models = ("ridge:none", "groupwise:zz", "kernel:angle")
    first = run_predictor_board(panel, models=models)
    second = run_predictor_board(panel, models=models)
    assert first == second


# --- the full board -----------------------------------------------------------


def test_the_board_runs_every_registered_model():
    board = run_predictor_board(_persistent_vol_panel())
    assert [entry["model_id"] for entry in board["models"]] == board["ranking"]
    assert sorted(board["ranking"]) == sorted(MODEL_IDS)
    for entry in board["models"]:
        assert len(entry["per_fold"]) == board["n_folds"]
        assert isinstance(entry["usable"], bool)
    assert board["baseline"] == BASELINE_MODEL_ID
    assert board["target"] == "next_21d_equal_weight_realized_vol"
    assert board["dsr_note"] == "not counted toward the deflated-Sharpe trials"


def test_admission_and_ranking_are_propagated():
    board = run_predictor_board(
        _persistent_vol_panel(),
        models=("ridge:none", "kernel:zz"),
    )
    ranking_keys = [
        (-entry["mean_ic"], entry["ic_std"], entry["model_id"])
        for entry in board["models"]
    ]
    assert ranking_keys == sorted(ranking_keys)
    admitted = [entry for entry in board["models"] if entry["usable"]]
    assert board["admitted_any"] == bool(admitted)
    if admitted:
        assert board["champion"] == next(
            entry["model_id"]
            for entry in board["models"]
            if entry["usable"]
        )
    else:
        assert board["champion"] is None
    assert board["admission"] == {
        "mean_ic_strictly_above": 0.03,
        "ic_stability_strictly_above": 0.5,
    }


def test_the_board_records_its_own_search():
    # A tuned run must be self-documenting: the grids that produced a number
    # travel with the number, or two boards are silently incomparable.
    board = run_predictor_board(
        _persistent_vol_panel(),
        models=("ridge:none", "kernel:zz"),
        alphas=(0.5, 2.0),
        map_weights=(0.5, 1.5),
        n_splits=4,
    )
    assert board["search"] == {
        "models": ["ridge:none", "kernel:zz"],
        "alphas": [0.5, 2.0],
        "map_weights": [0.5, 1.5],
        "n_splits": 4,
    }
    for entry in board["models"]:
        for fold in entry["per_fold"]:
            if "alpha" in fold:
                assert fold["alpha"] in (0.5, 2.0)


def test_champion_is_none_when_nothing_admits():
    board = run_predictor_board(
        _noise_panel(),
        models=("ridge:none", "kernel:angle"),
    )
    assert board["champion"] is None
    assert board["admitted_any"] is False
