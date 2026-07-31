"""Closed-form kernel agreement with the explicit feature maps.

The kernels exist so the ZZ column explosion never has to materialise inside a
model. That is only sound if the closed form and the explicit map are the same
mathematical object — so the governing tests here are agreement properties in
the compiler-agreement style, at 1e-10.
"""

from __future__ import annotations

import numpy as np
import pytest

from qlab.research.kernels import (
    KERNELS,
    angle_kernel,
    kernel_ridge_predict,
    quantum_gram,
    zz_kernel,
)
from qlab.research.prediction import _ridge_predict
from qlab.research.quantum_features import angle_map, scale_to_unit, zz_map


def _unit_grid(rows: int, cols: int = 4, seed: int = 3) -> np.ndarray:
    rng = np.random.default_rng(seed)
    scaled, _, _ = scale_to_unit(rng.normal(0.0, 1.0, (rows, cols)))
    return scaled


def _std_grid(rows: int, cols: int = 4, seed: int = 5) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.normal(0.0, 1.0, (rows, cols))


# --- agreement with the explicit maps ----------------------------------------


def test_angle_kernel_agrees_with_the_explicit_feature_map():
    a = _unit_grid(30, seed=3)
    b = _unit_grid(20, seed=4)
    explicit = angle_map(a) @ angle_map(b).T
    np.testing.assert_allclose(angle_kernel(a, b), explicit, atol=1e-10)


def test_zz_kernel_agrees_with_the_explicit_feature_map():
    a = _unit_grid(30, cols=5, seed=6)
    b = _unit_grid(20, cols=5, seed=7)
    explicit = zz_map(a) @ zz_map(b).T
    np.testing.assert_allclose(zz_kernel(a, b), explicit, atol=1e-10)


def test_zz_kernel_is_zero_for_a_single_feature():
    a = _unit_grid(10, cols=1, seed=8)
    b = _unit_grid(7, cols=1, seed=9)
    np.testing.assert_array_equal(zz_kernel(a, b), np.zeros((10, 7)))


def test_quantum_gram_with_unit_weights_agrees_with_stacked_features():
    a_std = _std_grid(25, seed=10)
    b_std = _std_grid(15, seed=11)
    a_unit = _unit_grid(25, seed=12)
    b_unit = _unit_grid(15, seed=13)
    for kind, mapper in (("angle", angle_map), ("zz", zz_map)):
        explicit = (
            a_std @ b_std.T + mapper(a_unit) @ mapper(b_unit).T
        )
        np.testing.assert_allclose(
            quantum_gram(a_std, b_std, a_unit, b_unit, kind),
            explicit,
            atol=1e-10,
        )


def test_the_linear_kernel_ignores_the_map_term():
    a_std = _std_grid(12, seed=14)
    b_std = _std_grid(9, seed=15)
    a_unit = _unit_grid(12, seed=16)
    b_unit = _unit_grid(9, seed=17)
    np.testing.assert_allclose(
        quantum_gram(a_std, b_std, a_unit, b_unit, "linear", w_map=7.0),
        a_std @ b_std.T,
        atol=1e-10,
    )


def test_the_gram_is_symmetric_and_psd_on_itself():
    a_std = _std_grid(40, seed=18)
    a_unit = _unit_grid(40, seed=19)
    for kind in KERNELS:
        gram = quantum_gram(a_std, a_std, a_unit, a_unit, kind)
        np.testing.assert_allclose(gram, gram.T, atol=1e-10)
        assert float(np.linalg.eigvalsh(gram).min()) >= -1e-8


# --- the solver ---------------------------------------------------------------


def test_kernel_ridge_equals_primal_ridge_for_the_linear_kernel():
    rng = np.random.default_rng(20)
    train_x = rng.normal(0.0, 1.0, (60, 4))
    test_x = rng.normal(0.0, 1.0, (15, 4))
    train_y = rng.normal(0.0, 1.0, 60)
    alpha = 1.7

    mean_x = train_x.mean(axis=0)
    scale_x = train_x.std(axis=0)
    scale_x = np.where(scale_x > 1e-12, scale_x, 1.0)
    std_train = (train_x - mean_x) / scale_x
    std_test = (test_x - mean_x) / scale_x

    dual = kernel_ridge_predict(
        std_train @ std_train.T,
        train_y,
        std_test @ std_train.T,
        alpha,
    )
    primal = _ridge_predict(train_x, train_y, test_x, alpha)
    np.testing.assert_allclose(dual, primal, atol=1e-8)


# --- refusals -----------------------------------------------------------------


def test_non_finite_input_fails_loud():
    bad = np.array([[0.1, np.nan], [0.2, 0.3]])
    good = np.array([[0.1, 0.2], [0.2, 0.3]])
    with pytest.raises(ValueError, match="non-finite"):
        angle_kernel(bad, good)
    with pytest.raises(ValueError, match="non-finite"):
        kernel_ridge_predict(
            np.array([[1.0, np.nan], [np.nan, 1.0]]),
            np.array([1.0, 2.0]),
            np.array([[1.0, 1.0]]),
            1.0,
        )


def test_a_feature_count_mismatch_is_refused():
    with pytest.raises(ValueError, match="feature-count mismatch"):
        angle_kernel(_unit_grid(5, cols=3), _unit_grid(5, cols=4))


def test_an_unknown_kernel_is_refused():
    a_std = _std_grid(5)
    a_unit = _unit_grid(5)
    with pytest.raises(ValueError, match="unknown kernel"):
        quantum_gram(a_std, a_std, a_unit, a_unit, "rbf")


def test_a_row_count_mismatch_between_std_and_unit_is_refused():
    with pytest.raises(ValueError, match="row-count mismatch"):
        quantum_gram(
            _std_grid(6),
            _std_grid(5),
            _unit_grid(5),
            _unit_grid(5),
            "angle",
        )


def test_kernel_ridge_refuses_a_nonpositive_alpha():
    k = np.eye(4)
    y = np.arange(4.0)
    with pytest.raises(ValueError, match="alpha"):
        kernel_ridge_predict(k, y, k[:2], 0.0)
