"""Leakage-controlled realized-volatility prediction research.

The v1 lane predicts one risk quantity: the equal-weight portfolio's next
21-session realized volatility. It never estimates an expected return. All
features are lagged, alpha selection happens inside each outer training fold,
and 21 forecast origins are purged between every expanding training window and
its test block.
"""

from __future__ import annotations

from bisect import bisect_right, insort
from collections import Counter
from collections.abc import Sequence
import math

import numpy as np
import pandas as pd

from qlab.research.quantum_features import AUGMENTATIONS, augment
from qlab.signals import hard


_TRADING_DAYS = 252
_LAG_WINDOWS = (5, 21, 63)
_TURBULENCE_LOOKBACK = 63
_DEFAULT_FOLDS = 5
PREDICTION_HORIZON_DAYS = 21
IC_ADMISSION_THRESHOLD = 0.03
IC_STABILITY_THRESHOLD = 0.5

FEATURE_COLUMNS = (
    "realized_vol_5",
    "realized_vol_21",
    "realized_vol_63",
    "mean_abs_return_5",
    "mean_abs_return_21",
    "mean_abs_return_63",
    "turbulence_percentile",
    "cross_sectional_dispersion_21",
)
TARGET_COLUMN = "target_vol_21"


def _validated_panel(panel: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(panel, pd.DataFrame):
        raise TypeError("panel must be a pandas DataFrame of asset returns")
    if panel.empty or panel.shape[1] < 2:
        raise ValueError(
            "panel must contain at least two assets for turbulence and "
            "cross-sectional dispersion"
        )
    if not panel.index.is_monotonic_increasing or not panel.index.is_unique:
        raise ValueError("panel index must be unique and increasing")
    if not panel.columns.is_unique:
        raise ValueError("panel asset columns must be unique")
    try:
        clean = panel.astype(float)
    except (TypeError, ValueError) as exc:
        raise TypeError("panel returns must be numeric") from exc
    if not np.isfinite(clean.to_numpy(dtype=float)).all():
        raise ValueError(
            "panel returns must be complete and finite; condition missing data "
            "before prediction"
        )
    return clean


def _causal_percentile(series: pd.Series) -> pd.Series:
    """Percentile of each value using only values observed through that date."""
    ordered: list[float] = []
    percentiles: dict[object, float] = {}
    for index, raw in series.items():
        value = float(raw)
        if not math.isfinite(value):
            continue
        insort(ordered, value)
        percentiles[index] = bisect_right(ordered, value) / len(ordered)
    return pd.Series(percentiles, dtype=float)


def build_vol_prediction_frame(
    panel: pd.DataFrame,
    *,
    horizon: int = PREDICTION_HORIZON_DAYS,
) -> pd.DataFrame:
    """Build point-in-time risk features and the forward realized-vol target.

    A row dated ``t`` uses returns through ``t-1`` for every feature. Its target
    is the annualized standard deviation of equal-weight portfolio returns from
    ``t+1`` through ``t+horizon``.
    """
    if isinstance(horizon, bool) or not isinstance(horizon, int):
        raise TypeError("horizon must be an integer")
    if horizon != PREDICTION_HORIZON_DAYS:
        raise ValueError(
            f"prediction v1 requires a {PREDICTION_HORIZON_DAYS}-day horizon"
        )

    returns = _validated_panel(panel)
    portfolio = returns.mean(axis=1)
    lagged_portfolio = portfolio.shift(1)
    columns: dict[str, pd.Series] = {}
    for window in _LAG_WINDOWS:
        columns[f"realized_vol_{window}"] = (
            lagged_portfolio.rolling(window, min_periods=window).std(ddof=1)
            * math.sqrt(_TRADING_DAYS)
        )
        columns[f"mean_abs_return_{window}"] = (
            lagged_portfolio.abs().rolling(window, min_periods=window).mean()
        )

    raw_turbulence = hard.turbulence(
        returns,
        lookback=_TURBULENCE_LOOKBACK,
    )
    columns["turbulence_percentile"] = (
        _causal_percentile(raw_turbulence).shift(1).reindex(returns.index)
    )
    daily_dispersion = returns.std(axis=1, ddof=0)
    columns["cross_sectional_dispersion_21"] = (
        daily_dispersion.shift(1)
        .rolling(PREDICTION_HORIZON_DAYS,
                 min_periods=PREDICTION_HORIZON_DAYS)
        .mean()
        * math.sqrt(_TRADING_DAYS)
    )

    # A rolling statistic ending at t+21 covers exactly t+1 ... t+21 after
    # shifting it back by the forecast horizon.
    columns[TARGET_COLUMN] = (
        portfolio.rolling(horizon, min_periods=horizon).std(ddof=1)
        .shift(-horizon)
        * math.sqrt(_TRADING_DAYS)
    )
    frame = (
        pd.DataFrame(columns, index=returns.index)
        .replace([np.inf, -np.inf], np.nan)
        .dropna(how="any")
    )
    if frame.empty:
        raise ValueError(
            "insufficient complete history for 63-day features and a 21-day "
            "forward target"
        )
    return frame.loc[:, [*FEATURE_COLUMNS, TARGET_COLUMN]]


def purged_walk_forward_splits(
    n_samples: int,
    *,
    n_splits: int = _DEFAULT_FOLDS,
    embargo: int = PREDICTION_HORIZON_DAYS,
    min_train_size: int | None = None,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Return expanding train/test indices with a pre-test embargo.

    For a test block beginning at position ``j``, training ends at
    ``j - embargo - 1``. Thus exactly ``embargo`` forecast origins sit between
    the two sets and no 21-day training label can overlap the test period.
    """
    for value, name in (
        (n_samples, "n_samples"),
        (n_splits, "n_splits"),
        (embargo, "embargo"),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} must be an integer")
    if n_samples <= 0:
        raise ValueError("n_samples must be positive")
    if n_splits < 2:
        raise ValueError("n_splits must be at least 2")
    if embargo < 1:
        raise ValueError("embargo must be positive")

    if min_train_size is None:
        min_train_size = max(126, n_samples // 3)
    if isinstance(min_train_size, bool) or not isinstance(min_train_size, int):
        raise TypeError("min_train_size must be an integer")
    if min_train_size < 10:
        raise ValueError("min_train_size must be at least 10")

    first_test = min_train_size + embargo
    remaining = n_samples - first_test
    if remaining < n_splits * 10:
        raise ValueError(
            "insufficient labeled observations for purged walk-forward CV: "
            f"need at least {first_test + n_splits * 10}, got {n_samples}"
        )

    boundaries = np.linspace(
        first_test,
        n_samples,
        num=n_splits + 1,
        dtype=int,
    )
    folds: list[tuple[np.ndarray, np.ndarray]] = []
    for test_start, test_stop in zip(boundaries[:-1], boundaries[1:]):
        train_stop = int(test_start) - embargo
        train = np.arange(0, train_stop, dtype=int)
        test = np.arange(int(test_start), int(test_stop), dtype=int)
        if len(train) < min_train_size or len(test) < 10:
            raise ValueError("purged walk-forward split is too small")
        folds.append((train, test))
    return folds


def _spearman_ic(predicted: np.ndarray, realized: np.ndarray) -> float:
    if len(predicted) != len(realized) or len(predicted) < 3:
        raise ValueError("Spearman IC requires matching vectors of length >= 3")
    predicted_rank = pd.Series(predicted).rank(method="average").to_numpy()
    realized_rank = pd.Series(realized).rank(method="average").to_numpy()
    if (
        float(np.std(predicted_rank)) <= 1e-12
        or float(np.std(realized_rank)) <= 1e-12
    ):
        return 0.0
    correlation = float(np.corrcoef(predicted_rank, realized_rank)[0, 1])
    return correlation if math.isfinite(correlation) else 0.0


def _ridge_predict(
    train_x: np.ndarray,
    train_y: np.ndarray,
    test_x: np.ndarray,
    alpha: float,
) -> np.ndarray:
    mean_x = train_x.mean(axis=0)
    scale_x = train_x.std(axis=0)
    scale_x = np.where(scale_x > 1e-12, scale_x, 1.0)
    standardized_train = (train_x - mean_x) / scale_x
    standardized_test = (test_x - mean_x) / scale_x
    mean_y = float(train_y.mean())
    centered_y = train_y - mean_y
    penalty = alpha * np.eye(standardized_train.shape[1], dtype=float)
    gram = standardized_train.T @ standardized_train + penalty
    rhs = standardized_train.T @ centered_y
    try:
        coefficients = np.linalg.solve(gram, rhs)
    except np.linalg.LinAlgError:
        coefficients = np.linalg.pinv(gram) @ rhs
    return mean_y + standardized_test @ coefficients


def _validated_alphas(alphas: Sequence[float]) -> tuple[float, ...]:
    if isinstance(alphas, (str, bytes)):
        raise TypeError("alphas must be a sequence of positive numbers")
    try:
        values = tuple(float(alpha) for alpha in alphas)
    except (TypeError, ValueError) as exc:
        raise TypeError("alphas must be a sequence of positive numbers") from exc
    if not values:
        raise ValueError("alphas must not be empty")
    if any(not math.isfinite(alpha) or alpha <= 0.0 for alpha in values):
        raise ValueError("alphas must be finite and positive")
    if len(set(values)) != len(values):
        raise ValueError("alphas must be unique")
    return values


def _choose_alpha(
    train_x: np.ndarray,
    train_y: np.ndarray,
    alphas: tuple[float, ...],
) -> float:
    """Tune alpha only within the outer fold's already-purged history."""
    inner_min_train = max(42, len(train_y) // 2)
    try:
        inner_folds = purged_walk_forward_splits(
            len(train_y),
            n_splits=3,
            embargo=PREDICTION_HORIZON_DAYS,
            min_train_size=inner_min_train,
        )
    except ValueError:
        return alphas[len(alphas) // 2]

    scores: dict[float, float] = {}
    for alpha in alphas:
        inner_ics = []
        for inner_train, inner_test in inner_folds:
            predicted = _ridge_predict(
                train_x[inner_train],
                train_y[inner_train],
                train_x[inner_test],
                alpha,
            )
            inner_ics.append(
                _spearman_ic(predicted, train_y[inner_test])
            )
        scores[alpha] = float(np.mean(inner_ics))
    # Preserve caller order as the deterministic tie-breaker.
    return max(alphas, key=lambda alpha: scores[alpha])


def _groupwise_ridge_predict(
    train_x: np.ndarray,
    train_y: np.ndarray,
    test_x: np.ndarray,
    alpha_raw: float,
    alpha_map: float,
    n_raw: int,
) -> np.ndarray:
    """Ridge with separate penalties for the raw and mapped feature groups.

    The measured failure of the explicit augmentation was a single global
    alpha over-shrinking the raw columns along with the near-collinear mapped
    ones (``planning-docs/2026-07-30-ml-lane.md``). A per-group diagonal
    penalty is rescue path #1 from that document. With equal alphas this is
    exactly :func:`_ridge_predict` — the identity the tests pin.
    """
    width = train_x.shape[1]
    if isinstance(n_raw, bool) or not isinstance(n_raw, int):
        raise TypeError("n_raw must be an integer")
    if not 0 < n_raw <= width:
        raise ValueError(
            f"n_raw must lie in [1, {width}] for a {width}-column matrix"
        )
    for name, alpha in (("alpha_raw", alpha_raw), ("alpha_map", alpha_map)):
        if not math.isfinite(float(alpha)) or float(alpha) <= 0.0:
            raise ValueError(f"{name} must be finite and positive")
    mean_x = train_x.mean(axis=0)
    scale_x = train_x.std(axis=0)
    scale_x = np.where(scale_x > 1e-12, scale_x, 1.0)
    standardized_train = (train_x - mean_x) / scale_x
    standardized_test = (test_x - mean_x) / scale_x
    mean_y = float(train_y.mean())
    centered_y = train_y - mean_y
    penalty = np.diag(np.concatenate([
        np.full(n_raw, float(alpha_raw)),
        np.full(width - n_raw, float(alpha_map)),
    ]))
    gram = standardized_train.T @ standardized_train + penalty
    rhs = standardized_train.T @ centered_y
    try:
        coefficients = np.linalg.solve(gram, rhs)
    except np.linalg.LinAlgError:
        coefficients = np.linalg.pinv(gram) @ rhs
    return mean_y + standardized_test @ coefficients


def _choose_groupwise_alphas(
    train_x: np.ndarray,
    train_y: np.ndarray,
    alphas: tuple[float, ...],
    n_raw: int,
) -> tuple[float, float]:
    """Tune both group alphas inside the outer fold's already-purged history.

    Grid order is the deterministic tie-breaker, mirroring
    :func:`_choose_alpha`; when the training block is too small for inner
    splits the middle grid pair is the same conservative fallback.
    """
    grid = [(a_raw, a_map) for a_raw in alphas for a_map in alphas]
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
    for pair in grid:
        inner_ics = []
        for inner_train, inner_test in inner_folds:
            predicted = _groupwise_ridge_predict(
                train_x[inner_train],
                train_y[inner_train],
                train_x[inner_test],
                pair[0],
                pair[1],
                n_raw,
            )
            inner_ics.append(_spearman_ic(predicted, train_y[inner_test]))
        scores[pair] = float(np.mean(inner_ics))
    return max(grid, key=lambda pair: scores[pair])


def _index_text(value: object) -> str:
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def predict_vol_ridge(
    panel: pd.DataFrame,
    alphas: Sequence[float] = (0.1, 1.0, 10.0),
    *,
    n_splits: int = _DEFAULT_FOLDS,
    augmentation: str = "none",
) -> dict:
    """Evaluate a portfolio-volatility ridge baseline with honest admission.

    The reported ICs are outer-fold Spearman correlations. Each outer fold's
    alpha comes from a separate purged inner walk-forward search, so the
    admission metrics never reuse their test block for hyperparameter tuning.

    ``augmentation`` selects a quantum-inspired feature map (see
    :mod:`qlab.research.quantum_features`). The maps are pointwise, but their
    input scaling is not: the [0,1] bounds are fitted on each fold's training
    block alone and then applied to its test block. Fitting them on the whole
    sample is look-ahead, and it is the exact mistake that makes a "stateless"
    preprocessing step leak.
    """
    if augmentation not in AUGMENTATIONS:
        raise ValueError(
            f"unknown augmentation {augmentation!r}; available: {AUGMENTATIONS}")
    candidates = _validated_alphas(alphas)
    frame = build_vol_prediction_frame(panel)
    feature_matrix = frame.loc[:, FEATURE_COLUMNS].to_numpy(dtype=float)
    target = frame[TARGET_COLUMN].to_numpy(dtype=float)
    folds = purged_walk_forward_splits(
        len(frame),
        n_splits=n_splits,
        embargo=PREDICTION_HORIZON_DAYS,
    )

    per_fold: list[dict] = []
    alpha_choices: list[float] = []
    fold_ics: list[float] = []
    for fold_number, (train, test) in enumerate(folds, start=1):
        # Bounds from the training block only, then reused verbatim on the
        # test block. This is the whole leak surface of the augmentation.
        train_x, lo, hi = augment(feature_matrix[train], augmentation)
        test_x, _, _ = augment(feature_matrix[test], augmentation, lo=lo, hi=hi)
        alpha = _choose_alpha(
            train_x,
            target[train],
            candidates,
        )
        predicted = _ridge_predict(
            train_x,
            target[train],
            test_x,
            alpha,
        )
        ic = _spearman_ic(predicted, target[test])
        alpha_choices.append(alpha)
        fold_ics.append(ic)
        train_end = int(train[-1])
        test_start = int(test[0])
        per_fold.append({
            "fold": fold_number,
            "ic": ic,
            "alpha": alpha,
            "n_train": int(len(train)),
            "n_test": int(len(test)),
            "train_start_index": int(train[0]),
            "train_end_index": train_end,
            "test_start_index": test_start,
            "test_end_index": int(test[-1]),
            "train_start": _index_text(frame.index[train[0]]),
            "train_end": _index_text(frame.index[train_end]),
            "test_start": _index_text(frame.index[test_start]),
            "test_end": _index_text(frame.index[test[-1]]),
            "embargo_days": PREDICTION_HORIZON_DAYS,
        })

    mean_ic = float(np.mean(fold_ics))
    ic_std = float(np.std(fold_ics, ddof=0))
    ic_stability = mean_ic / max(ic_std, 1e-12)
    usable = bool(
        mean_ic > IC_ADMISSION_THRESHOLD
        and ic_stability > IC_STABILITY_THRESHOLD
    )
    counts = Counter(alpha_choices)
    # A tie goes to the most recent outer fold's inner-CV selection.
    chosen_alpha = max(
        candidates,
        key=lambda alpha: (
            counts[alpha],
            max(
                index
                for index, selected in enumerate(alpha_choices)
                if selected == alpha
            ) if counts[alpha] else -1,
        ),
    )
    return {
        "mean_ic": mean_ic,
        "ic_stability": ic_stability,
        "ic_std": ic_std,
        "usable": usable,
        "chosen_alpha": chosen_alpha,
        "per_fold": per_fold,
        "n_obs": int(len(frame)),
        "features": list(FEATURE_COLUMNS),
        "target": "next_21d_equal_weight_realized_vol",
        "horizon_days": PREDICTION_HORIZON_DAYS,
        "embargo_days": PREDICTION_HORIZON_DAYS,
        "admission": {
            "mean_ic_strictly_above": IC_ADMISSION_THRESHOLD,
            "ic_stability_strictly_above": IC_STABILITY_THRESHOLD,
        },
    }
