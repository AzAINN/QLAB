"""The predictor board: paired champion/challenger evaluation.

One panel, one set of purged walk-forward folds, every model. Pairing is the
board's structural promise — a model's edge over the baseline is only a claim
when both saw exactly the same test blocks — and it is what makes the per-fold
IC differences a valid input to a paired t-statistic.

The model set is the 2026-07-30 ml-lane document's ranked rescue paths for the
quantum-inspired augmentation, plus the measured baseline they must beat:

* ``ridge:none`` — the admitted v1 baseline (:func:`predict_vol_ridge`'s
  estimator, unchanged).
* ``groupwise:*`` — rescue path #1: separate ridge penalties for the raw and
  mapped column groups.
* ``kernel:*`` — rescue path #2: the closed-form quantum kernels, where the
  ZZ column explosion never materialises. ``kernel:linear`` is the dual of
  the baseline and exists as a live identity check, not a contender.

Board results are research evidence. They are logged with
``dsr_trial_counted: False`` and must never write a backtest row — the
window-evidence precedent.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from qlab.research.kernels import KERNELS, kernel_ridge_predict, quantum_gram
from qlab.research.prediction import (
    _DEFAULT_FOLDS,
    FEATURE_COLUMNS,
    IC_ADMISSION_THRESHOLD,
    IC_STABILITY_THRESHOLD,
    PREDICTION_HORIZON_DAYS,
    TARGET_COLUMN,
    _choose_alpha,
    _choose_groupwise_alphas,
    _groupwise_ridge_predict,
    _ridge_predict,
    _spearman_ic,
    _validated_alphas,
    build_vol_prediction_frame,
    purged_walk_forward_splits,
)
from qlab.research.quantum_features import augment, scale_to_unit

MODEL_IDS = (
    "ridge:none",
    "groupwise:angle",
    "groupwise:zz",
    "groupwise:angle_zz",
    "kernel:linear",
    "kernel:angle",
    "kernel:zz",
)
BASELINE_MODEL_ID = "ridge:none"
DEFAULT_ALPHAS = (0.1, 1.0, 10.0)
DEFAULT_MAP_WEIGHTS = (0.25, 1.0, 4.0)


def _validated_models(models) -> tuple[str, ...]:
    if isinstance(models, (str, bytes)):
        raise ValueError("models must be a sequence of model ids")
    ids = tuple(models)
    if not ids:
        raise ValueError("models must not be empty")
    for model_id in ids:
        if model_id not in MODEL_IDS:
            raise ValueError(
                f"unknown model {model_id!r}; available: {MODEL_IDS}"
            )
    if len(set(ids)) != len(ids):
        raise ValueError("models must be unique")
    if BASELINE_MODEL_ID not in ids:
        raise ValueError(
            f"the board requires the baseline {BASELINE_MODEL_ID!r}; "
            "an unpaired challenger is not evidence"
        )
    return ids


def _validated_map_weights(map_weights) -> tuple[float, ...]:
    if isinstance(map_weights, (str, bytes)):
        raise TypeError("map_weights must be a sequence of positive numbers")
    try:
        values = tuple(float(weight) for weight in map_weights)
    except (TypeError, ValueError) as exc:
        raise TypeError(
            "map_weights must be a sequence of positive numbers"
        ) from exc
    if not values:
        raise ValueError("map_weights must not be empty")
    if any(not math.isfinite(w) or w <= 0.0 for w in values):
        raise ValueError("map_weights must be finite and positive")
    if len(set(values)) != len(values):
        raise ValueError("map_weights must be unique")
    return values


def _kernel_fold_predict(
    train_raw: np.ndarray,
    train_y: np.ndarray,
    test_raw: np.ndarray,
    kind: str,
    alpha: float,
    w_map: float,
) -> np.ndarray:
    """One kernel-ridge fit with every statistic fitted on the train block."""
    mean_x = train_raw.mean(axis=0)
    scale_x = train_raw.std(axis=0)
    scale_x = np.where(scale_x > 1e-12, scale_x, 1.0)
    std_train = (train_raw - mean_x) / scale_x
    std_test = (test_raw - mean_x) / scale_x
    unit_train, lo, hi = scale_to_unit(train_raw)
    unit_test, _, _ = scale_to_unit(test_raw, lo=lo, hi=hi)
    k_train = quantum_gram(
        std_train, std_train, unit_train, unit_train, kind, w_map=w_map
    )
    k_cross = quantum_gram(
        std_test, std_train, unit_test, unit_train, kind, w_map=w_map
    )
    return kernel_ridge_predict(k_train, train_y, k_cross, alpha)


def _choose_kernel_params(
    train_raw: np.ndarray,
    train_y: np.ndarray,
    kind: str,
    alphas: tuple[float, ...],
    map_weights: tuple[float, ...],
) -> tuple[float, float]:
    """Tune (alpha, w_map) inside the outer fold's already-purged history.

    ``linear`` pins ``w_map`` to zero so its grid — and therefore its inner-CV
    scores and tie-breaks — collapses to exactly :func:`_choose_alpha`'s,
    preserving the dual/primal identity with the baseline.
    """
    weight_grid = (0.0,) if kind == "linear" else map_weights
    grid = [(alpha, w) for alpha in alphas for w in weight_grid]
    inner_min_train = max(42, len(train_y) // 2)
    try:
        inner_folds = purged_walk_forward_splits(
            len(train_y),
            n_splits=3,
            embargo=PREDICTION_HORIZON_DAYS,
            min_train_size=inner_min_train,
        )
    except ValueError:
        return grid[len(grid) // 2]

    scores: dict[tuple[float, float], float] = {}
    for alpha, w_map in grid:
        inner_ics = []
        for inner_train, inner_test in inner_folds:
            predicted = _kernel_fold_predict(
                train_raw[inner_train],
                train_y[inner_train],
                train_raw[inner_test],
                kind,
                alpha,
                w_map,
            )
            inner_ics.append(_spearman_ic(predicted, train_y[inner_test]))
        scores[(alpha, w_map)] = float(np.mean(inner_ics))
    return max(grid, key=lambda pair: scores[pair])


def _evaluate_model(
    model_id: str,
    feature_matrix: np.ndarray,
    target: np.ndarray,
    folds: list[tuple[np.ndarray, np.ndarray]],
    alphas: tuple[float, ...],
    map_weights: tuple[float, ...],
) -> list[dict]:
    family, variant = model_id.split(":", 1)
    n_raw = feature_matrix.shape[1]
    per_fold: list[dict] = []
    for fold_number, (train, test) in enumerate(folds, start=1):
        if family == "ridge":
            alpha = _choose_alpha(
                feature_matrix[train], target[train], alphas
            )
            predicted = _ridge_predict(
                feature_matrix[train],
                target[train],
                feature_matrix[test],
                alpha,
            )
            hyperparams: dict = {"alpha": alpha}
        elif family == "groupwise":
            train_x, lo, hi = augment(feature_matrix[train], variant)
            test_x, _, _ = augment(
                feature_matrix[test], variant, lo=lo, hi=hi
            )
            alpha_raw, alpha_map = _choose_groupwise_alphas(
                train_x, target[train], alphas, n_raw
            )
            predicted = _groupwise_ridge_predict(
                train_x, target[train], test_x, alpha_raw, alpha_map, n_raw
            )
            hyperparams = {"alpha_raw": alpha_raw, "alpha_map": alpha_map}
        else:
            alpha, w_map = _choose_kernel_params(
                feature_matrix[train],
                target[train],
                variant,
                alphas,
                map_weights,
            )
            predicted = _kernel_fold_predict(
                feature_matrix[train],
                target[train],
                feature_matrix[test],
                variant,
                alpha,
                w_map,
            )
            hyperparams = {"alpha": alpha, "w_map": w_map}
        per_fold.append({
            "fold": fold_number,
            "ic": _spearman_ic(predicted, target[test]),
            **hyperparams,
        })
    return per_fold


def _paired_t(diffs: np.ndarray) -> float:
    spread = float(np.std(diffs, ddof=1))
    if spread <= 1e-12:
        return 0.0
    return float(np.mean(diffs) / (spread / math.sqrt(len(diffs))))


def run_predictor_board(
    panel: pd.DataFrame,
    *,
    models=MODEL_IDS,
    alphas=DEFAULT_ALPHAS,
    map_weights=DEFAULT_MAP_WEIGHTS,
    n_splits: int = _DEFAULT_FOLDS,
) -> dict:
    """Evaluate every requested model on one shared set of purged folds.

    Returns a JSON-shaped board: per-model admission metrics, paired
    comparisons against the fixed baseline, a deterministic ranking
    ``(-mean_ic, ic_std, model_id)``, and the first admitted model as
    ``champion`` (or ``None`` — an honest empty answer, never a default).
    """
    model_ids = _validated_models(models)
    candidates = _validated_alphas(alphas)
    weights = _validated_map_weights(map_weights)
    frame = build_vol_prediction_frame(panel)
    feature_matrix = frame.loc[:, FEATURE_COLUMNS].to_numpy(dtype=float)
    target = frame[TARGET_COLUMN].to_numpy(dtype=float)
    folds = purged_walk_forward_splits(
        len(frame),
        n_splits=n_splits,
        embargo=PREDICTION_HORIZON_DAYS,
    )

    fold_results = {
        model_id: _evaluate_model(
            model_id, feature_matrix, target, folds, candidates, weights
        )
        for model_id in model_ids
    }
    baseline_ics = np.array(
        [fold["ic"] for fold in fold_results[BASELINE_MODEL_ID]],
        dtype=float,
    )

    entries: list[dict] = []
    for model_id in model_ids:
        per_fold = fold_results[model_id]
        ics = np.array([fold["ic"] for fold in per_fold], dtype=float)
        mean_ic = float(np.mean(ics))
        ic_std = float(np.std(ics, ddof=0))
        ic_stability = mean_ic / max(ic_std, 1e-12)
        family, variant = model_id.split(":", 1)
        diffs = ics - baseline_ics
        is_baseline = model_id == BASELINE_MODEL_ID
        entries.append({
            "model_id": model_id,
            "family": family,
            "variant": variant,
            "mean_ic": mean_ic,
            "ic_std": ic_std,
            "ic_stability": ic_stability,
            "usable": bool(
                mean_ic > IC_ADMISSION_THRESHOLD
                and ic_stability > IC_STABILITY_THRESHOLD
            ),
            "per_fold": per_fold,
            "delta_mean_ic_vs_baseline": (
                0.0 if is_baseline else float(np.mean(diffs))
            ),
            "wins_vs_baseline": (
                0 if is_baseline else int(np.sum(diffs > 0))
            ),
            "paired_t_vs_baseline": (
                None if is_baseline else _paired_t(diffs)
            ),
        })

    entries.sort(
        key=lambda entry: (
            -entry["mean_ic"], entry["ic_std"], entry["model_id"]
        )
    )
    champion = next(
        (entry["model_id"] for entry in entries if entry["usable"]), None
    )
    return {
        "n_obs": int(len(frame)),
        "n_folds": int(n_splits),
        "target": "next_21d_equal_weight_realized_vol",
        "horizon_days": PREDICTION_HORIZON_DAYS,
        "embargo_days": PREDICTION_HORIZON_DAYS,
        "features": list(FEATURE_COLUMNS),
        "baseline": BASELINE_MODEL_ID,
        "models": entries,
        "ranking": [entry["model_id"] for entry in entries],
        "champion": champion,
        "admitted_any": champion is not None,
        "admission": {
            "mean_ic_strictly_above": IC_ADMISSION_THRESHOLD,
            "ic_stability_strictly_above": IC_STABILITY_THRESHOLD,
        },
        "dsr_note": "not counted toward the deflated-Sharpe trials",
        "kernels": list(KERNELS),
        # A tuned run must be self-documenting: the grids that produced a
        # number travel with the number, or two boards are incomparable.
        "search": {
            "models": list(model_ids),
            "alphas": list(candidates),
            "map_weights": list(weights),
            "n_splits": int(n_splits),
        },
    }
