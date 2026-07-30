"""Quantum-inspired feature maps: correctness, and the leak they could cause.

The maps themselves are pointwise and cannot leak. Their *input scaling* can,
and that is the only interesting property to guard: bounds fitted on the whole
sample are look-ahead, which is exactly how an "obviously stateless"
preprocessing step smuggles the future into a walk-forward split.
"""

from __future__ import annotations

import numpy as np
import pytest

from qlab.research.quantum_features import (
    AUGMENTATIONS,
    angle_map,
    augment,
    augmented_width,
    scale_to_unit,
    zz_map,
)


def _grid(rows: int = 40, cols: int = 4, seed: int = 3) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.normal(0.0, 1.0, (rows, cols))


# --- the maps -----------------------------------------------------------------


def test_angle_map_emits_a_unit_state_per_feature():
    """cos^2 + sin^2 == 1 is what makes this an encoding rather than a hack.

    A bounded, unit-norm pair per feature is why ridge's per-column penalty
    stays meaningful: no column can dominate purely by scale.
    """
    unit, _, _ = scale_to_unit(_grid())
    out = angle_map(unit)
    assert out.shape == (unit.shape[0], 2 * unit.shape[1])
    norms = out[:, 0::2] ** 2 + out[:, 1::2] ** 2
    assert np.allclose(norms, 1.0)


def test_angle_map_is_injective_over_the_encoded_range():
    # A full turn would fold distinct inputs onto the same point. Half of pi
    # keeps cos and sin both monotone, so the map loses no information.
    unit = np.linspace(0.0, 1.0, 50).reshape(-1, 1)
    out = angle_map(unit)
    assert np.unique(np.round(out, 12), axis=0).shape[0] == 50


def test_zz_map_emits_one_bounded_pair_per_feature_pair():
    unit, _, _ = scale_to_unit(_grid(cols=5))
    out = zz_map(unit)
    assert out.shape == (unit.shape[0], 5 * 4)     # n*(n-1)
    # Bounded: an unbounded product term would dominate the ridge penalty by
    # scale alone rather than by information.
    assert np.abs(out).max() <= 1.0


def test_zz_map_is_empty_for_a_single_feature():
    # There is no pair to interact. Returning a degenerate column would give
    # ridge a constant to fit.
    assert zz_map(np.linspace(0, 1, 10).reshape(-1, 1)).shape == (10, 0)


def test_zz_actually_encodes_an_interaction():
    """Guards the map from being a relabelled copy of its inputs.

    If the pair column were a function of one feature alone, the map would add
    columns without adding information — which a linear model cannot use.
    """
    a = np.array([[0.2, 0.9]])
    b = np.array([[0.2, 0.1]])       # same first feature, different second
    assert not np.allclose(zz_map(a), zz_map(b))
    c = np.array([[0.9, 0.2]])       # swapped: the product is symmetric
    assert np.allclose(zz_map(a), zz_map(c))


# --- widths and dispatch -------------------------------------------------------


def test_augmented_width_matches_what_augment_produces():
    # The width is the cost side of the trade and callers size their sample
    # against it, so a wrong prediction is worse than no prediction.
    X = _grid(cols=6)
    for kind in AUGMENTATIONS:
        out, _, _ = augment(X, kind)
        assert out.shape[1] == augmented_width(6, kind), kind


def test_zz_width_is_quadratic_and_that_is_the_warning():
    assert augmented_width(6, "none") == 6
    assert augmented_width(6, "angle") == 18
    assert augmented_width(6, "zz") == 36
    assert augmented_width(6, "angle_zz") == 48


def test_every_augmentation_keeps_the_raw_features():
    # The map is additive: a model must never be made *worse off* than the
    # baseline by losing access to the original columns.
    X = _grid(cols=3)
    for kind in AUGMENTATIONS:
        out, _, _ = augment(X, kind)
        assert np.allclose(out[:, :3], X), kind


def test_an_unknown_augmentation_is_refused():
    with pytest.raises(ValueError, match="unknown augmentation"):
        augment(_grid(), "qaoa_magic")
    with pytest.raises(ValueError, match="unknown augmentation"):
        augmented_width(4, "qaoa_magic")


def test_non_finite_input_fails_loud():
    # A NaN through cos/sin produces a NaN column that ridge then fits around;
    # the failure would surface as an unexplained IC drop, not as an error.
    bad = _grid()
    bad[3, 1] = np.nan
    with pytest.raises(ValueError, match="non-finite"):
        augment(bad, "angle")


# --- the leak surface ----------------------------------------------------------


def test_bounds_are_returned_so_a_caller_can_avoid_look_ahead():
    """The whole point: bounds fit on train, applied to test.

    Recomputing them on the test block leaks its range backwards, which is a
    real and easy-to-miss look-ahead in a walk-forward split.
    """
    X = _grid(rows=60)
    train, test = X[:40], X[40:]
    _, lo, hi = augment(train, "angle")
    honest, _, _ = augment(test, "angle", lo=lo, hi=hi)
    leaky, _, _ = augment(test, "angle")
    # The test block's own range differs from the training range, so the two
    # encodings must differ — if they did not, the guard would be untestable.
    assert not np.allclose(honest, leaky)


def test_test_rows_outside_the_training_range_are_clipped_not_wrapped():
    # An unseen extreme must saturate at the boundary. Wrapping would map a new
    # high onto the encoding of a low — a silent, systematic mislabel.
    train = np.array([[0.0], [1.0]])
    _, lo, hi = scale_to_unit(train)
    scaled, _, _ = scale_to_unit(np.array([[5.0], [-5.0]]), lo=lo, hi=hi)
    assert scaled.max() == 1.0 and scaled.min() == 0.0


def test_a_constant_training_column_does_not_divide_by_zero():
    const = np.zeros((10, 2))
    const[:, 1] = np.arange(10)
    out, _, _ = augment(const, "angle_zz")
    assert np.isfinite(out).all()


def test_the_maps_are_pointwise():
    """Row i's output depends on row i alone, given fixed bounds.

    This is what makes the maps safe inside a walk-forward split at all: no
    rolling window, no cross-sectional statistic, nothing that could see a
    later row.
    """
    X = _grid(rows=30)
    _, lo, hi = augment(X, "angle_zz")
    full, _, _ = augment(X, "angle_zz", lo=lo, hi=hi)
    for i in (0, 7, 29):
        single, _, _ = augment(X[i:i + 1], "angle_zz", lo=lo, hi=hi)
        assert np.allclose(single[0], full[i])
