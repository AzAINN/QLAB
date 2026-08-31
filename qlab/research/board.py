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
# The board's headline number is a maximum over models, each tuned over its
# own grid. Positioning it needs a null of the whole selection, which costs
# one full board per trial; 24 buys a p-value resolution of ~0.04, enough to
# separate "noise reproduces this routinely" from "it does not".
_DEFAULT_NULL_TRIALS = 24
_NULL_ALPHA = 0.05


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


# What a predictor board is and is not, carried by every run row it writes.
# One tuple, because two writers persist these rows and a caveat that reaches
# only one surface is a caveat the reader of the other never sees.
PREDICTOR_BOARD_CAVEATS = (
    "risk prediction only",
    "research stage",
    "ranking is (-mean_ic, ic_std, model_id); the champion is the "
    "first admitted model, not a promoted one",
)


def validate_search(
    models,
    *,
    alphas=None,
    map_weights=None,
    n_splits=None,
    null_trials=None,
) -> dict:
    """Validate a board request's search knobs without running anything.

    Every check here is one :func:`run_predictor_board` performs on the way
    in. Running them up front is what lets a caller separate "the request was
    wrong" from "the fit failed" -- a single ``try`` around the whole run
    cannot, and would report an estimator failure as the operator's mistake.

    Returns the keyword arguments to pass on. An absent knob stays absent, so
    the board's own defaults still apply.
    """
    search: dict = {"models": _validated_models(models)}
    if alphas is not None:
        search["alphas"] = _validated_alphas(alphas)
    if map_weights is not None:
        search["map_weights"] = _validated_map_weights(map_weights)
    if n_splits is not None:
        if isinstance(n_splits, bool) or not isinstance(n_splits, int):
            raise TypeError("n_splits must be an integer")
        if n_splits < 2:
            raise ValueError("n_splits must be at least 2; one fold is not a "
                             "walk-forward evaluation")
        search["n_splits"] = n_splits
    if null_trials is not None:
        if isinstance(null_trials, bool) or not isinstance(null_trials, int):
            raise TypeError("null_trials must be an integer")
        # Zero is a real choice -- it means "do not null this selection" and
        # the board says so in the row rather than pretending it was tested.
        if null_trials < 0:
            raise ValueError("null_trials must not be negative")
        search["null_trials"] = null_trials
    return search


def predictor_run_spec(
    *,
    as_of: str,
    universe: str,
    tickers,
    lookback_days: int,
    source: str,
    snapshot_id: str,
    board: dict,
    caveats=None,
) -> dict:
    """The registry row a predictor board writes, built in exactly one place.

    Two surfaces persist this row -- the owner's ``/api/research/predictors/
    run`` and the ``research.predictor_board`` tool -- and both are read back
    by the same summarisers. Built twice, the two drifted at ``as_of`` within
    a single branch. A reader of the runs table must never have to know which
    surface wrote a row.

    ``algorithm_id`` and ``dsr_trial_counted`` are fixed here, not arguments:
    a board is research evidence, it writes no backtest row and no solution,
    and no caller gets to say otherwise.
    """
    return {
        "algorithm_id": "predictor_board",
        "as_of": str(as_of),
        "universe": universe,
        "tickers": list(tickers),
        "lookback_days": int(lookback_days),
        "source": source,
        "snapshot_id": snapshot_id,
        "board": board,
        "dsr_trial_counted": False,
        "caveats": list(caveats if caveats is not None
                        else PREDICTOR_BOARD_CAVEATS),
    }


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


def _selected_max_ic(
    model_ids: tuple[str, ...],
    feature_matrix: np.ndarray,
    target: np.ndarray,
    folds: list[tuple[np.ndarray, np.ndarray]],
    alphas: tuple[float, ...],
    map_weights: tuple[float, ...],
) -> float:
    """The number the board's ranking actually reports: the selected maximum."""
    best = -math.inf
    for model_id in model_ids:
        per_fold = _evaluate_model(
            model_id, feature_matrix, target, folds, alphas, map_weights
        )
        best = max(best, float(np.mean([f["ic"] for f in per_fold])))
    return best


def _selection_null(
    model_ids: tuple[str, ...],
    feature_matrix: np.ndarray,
    target: np.ndarray,
    folds: list[tuple[np.ndarray, np.ndarray]],
    alphas: tuple[float, ...],
    map_weights: tuple[float, ...],
    observed_max_ic: float,
    trials: int,
) -> dict:
    """What this selection procedure scores when there is nothing to find.

    The board reports a maximum over models, each tuned over its own grid,
    and then judges it against a per-model bar. That bar cannot hold: a
    threshold calibrated for one estimator is being applied to the best of
    seven, and the top-ranked mean IC is a selected extremum. Measured on
    100 panels of pure noise, the unmodified procedure admitted a champion
    66 times, 39 of them quantum-mapped, and 84 of 100 cleared the 0.03
    mean_ic bar. The live desk's admitted champion scored 0.178; the noise
    median was 0.21. Nothing in the board said so.

    The null is built by circularly shifting the target against the features.
    A shift preserves the target's autocorrelation exactly -- it is the same
    series, rotated -- which matters because the 21-day overlapping horizon
    is the reason the effective sample is so much smaller than the row count.
    Shuffling instead would destroy that dependence and produce a flattering
    null, understating the very problem being measured.

    Deterministic: offsets are fixed spans of the sample, so a board is
    reproducible and two boards over the same panel are comparable.
    """
    n = len(target)
    # Keep every shift well away from 0 and n, where the rotated target still
    # lines up with the features over most of the sample.
    usable = [
        int(round(n * (i + 1) / (trials + 1)))
        for i in range(trials)
    ]
    offsets = sorted({o for o in usable if PREDICTION_HORIZON_DAYS < o < n - PREDICTION_HORIZON_DAYS})
    scores = [
        _selected_max_ic(
            model_ids, feature_matrix, np.roll(target, offset), folds,
            alphas, map_weights,
        )
        for offset in offsets
    ]
    if not scores:
        return {
            "trials": 0,
            "p_value": None,
            "reason": (
                f"a {n}-row sample admits no circular shift clear of the "
                f"{PREDICTION_HORIZON_DAYS}-day horizon, so the selected "
                "maximum could not be nulled and is unpositioned"
            ),
        }
    arr = np.array(scores, dtype=float)
    # +1 in numerator and denominator: the observed run is itself one draw,
    # so a p-value of exactly zero is not available from a finite null.
    exceedances = int(np.sum(arr >= observed_max_ic))
    p_value = float((exceedances + 1) / (len(arr) + 1))
    # A p-value from T resamples lives on a grid of width 1/(T+1). On a
    # deterministic 250-day vol cycle -- IC +0.86 against a null median of
    # +0.21 -- this came back as exactly 0.080 six runs running: one single
    # exceedance, the shift that realigns the cycle. `established` therefore
    # turns on one null draw, and a bare p hides that completely.
    resolution = 1.0 / (len(arr) + 1)
    # Below 1/alpha - 1 trials, `p <= alpha` is arithmetically unreachable: the
    # smallest p a T-trial null can produce is 1/(T+1), which is 0.100 at T=9.
    # A verdict that cannot come out True is not a strict test, it is a broken
    # instrument, and reporting it as False would read as "tested and refuted".
    underpowered = bool(resolution > _NULL_ALPHA)
    return {
        "trials": int(len(arr)),
        "method": "circular_shift_of_target",
        "p_value": p_value,
        "exceedances": exceedances,
        "p_value_resolution": float(resolution),
        "underpowered_for_alpha": underpowered,
        "observed_max_mean_ic": float(observed_max_ic),
        "null_median_max_mean_ic": float(np.median(arr)),
        "null_p90_max_mean_ic": float(np.percentile(arr, 90)),
        "null_max_mean_ic": float(arr.max()),
        "reason": (
            f"the best of {len(model_ids)} tuned models scored "
            f"{observed_max_ic:+.4f}; the same selection over {len(arr)} "
            f"null shifts had median {float(np.median(arr)):+.4f} and "
            f"reached {float(arr.max()):+.4f}. {exceedances} of {len(arr)} "
            f"null runs matched or beat it, so p={p_value:.3f} "
            f"(resolution {resolution:.3f} — no finer p is available from "
            f"{len(arr)} trials)"
            + (
                f". {len(arr)} trials cannot establish anything at "
                f"alpha={_NULL_ALPHA}: the smallest p reachable is "
                f"{resolution:.3f}, so no result could clear the bar and the "
                f"verdict is withheld rather than reported as a refutation"
                if underpowered else ""
            )
        ),
    }


def run_predictor_board(
    panel: pd.DataFrame,
    *,
    models=MODEL_IDS,
    alphas=DEFAULT_ALPHAS,
    map_weights=DEFAULT_MAP_WEIGHTS,
    n_splits: int = _DEFAULT_FOLDS,
    null_trials: int = _DEFAULT_NULL_TRIALS,
) -> dict:
    """Evaluate every requested model on one shared set of purged folds.

    Returns a JSON-shaped board: per-model admission metrics, paired
    comparisons against the fixed baseline, a deterministic ranking
    ``(-mean_ic, ic_std, model_id)``, and the first admitted model as
    ``champion`` (or ``None`` — an honest empty answer, never a default).

    ``usable`` and ``champion`` remain what they always were: the documented
    fixed bar. ``champion_established`` is the separate, harder question of
    whether the selection beats its own null — see :func:`_selection_null`.
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
    # Position the selected maximum against its own null. This is the number
    # the ranking reports, so it is the number that has to be nulled --
    # nulling a fixed model would answer a question nobody asked.
    null = _selection_null(
        model_ids, feature_matrix, target, folds, candidates, weights,
        observed_max_ic=float(entries[0]["mean_ic"]) if entries else 0.0,
        trials=int(null_trials),
    )
    p_value = null.get("p_value")
    if champion is None:
        established = False
    elif p_value is None:
        # The null could not be built. Unknown must not read as established.
        established = None
    elif null.get("underpowered_for_alpha"):
        # The null was built but cannot reach alpha at this trial count, so
        # False here would mean "no null could ever pass", not "this champion
        # failed". Withhold the claim rather than manufacture a refutation.
        established = None
    else:
        established = bool(p_value <= _NULL_ALPHA)
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
        # Admission is the fixed bar. Establishment is whether the selection
        # beat its own null. They are different claims and are reported
        # separately: None means the null could not be built, which is not
        # the same as failing it.
        "champion_established": established,
        "selection_null": null,
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
