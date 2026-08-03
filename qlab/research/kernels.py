"""Closed-form kernels for the quantum-inspired feature maps.

The 2026-07-30 measurement (``planning-docs/2026-07-30-ml-lane.md``) found the
explicit ZZ augmentation *hurts* under a single-alpha ridge, and named the
kernel formulation as a rescue path: the ZZ map's natural home is a kernel,
where the quadratic column count never has to sit in a design matrix against a
few hundred rows. These functions are those kernels, in closed form.

The narrow claim is unchanged from :mod:`qlab.research.quantum_features`:
these are the Gram matrices a product-state quantum encoding would induce,
available at classical cost. No circuit, no speedup, no hardware story. Each
kernel is *exactly* the inner product of the corresponding explicit map — an
agreement the tests pin at 1e-10 — so nothing here can quietly diverge from
the measured lane.

Scaling discipline is the caller's: unit-scaling bounds and standardisation
moments must be fitted on a training fold and reused on its test fold, exactly
as with :func:`qlab.research.quantum_features.augment`.
"""

from __future__ import annotations

import math

import numpy as np

from qlab.research.quantum_features import _ANGLE_SCALE, _as_matrix

KERNELS = ("linear", "angle", "zz")


def _validated_pair(a, b) -> tuple[np.ndarray, np.ndarray]:
    left = _as_matrix(a)
    right = _as_matrix(b)
    if left.shape[1] != right.shape[1]:
        raise ValueError(
            f"feature-count mismatch: {left.shape[1]} vs {right.shape[1]}"
        )
    return left, right


def angle_kernel(a_unit, b_unit) -> np.ndarray:
    """Gram of :func:`angle_map` features without materialising them.

    ``cos(ta - tb)`` summed over features, via the product identity — one
    matrix product per trig component.
    """
    left, right = _validated_pair(a_unit, b_unit)
    theta_a = left * _ANGLE_SCALE
    theta_b = right * _ANGLE_SCALE
    return (
        np.cos(theta_a) @ np.cos(theta_b).T
        + np.sin(theta_a) @ np.sin(theta_b).T
    )


def _pair_phases(unit: np.ndarray) -> np.ndarray:
    """Rows of ``(pi - theta_i)(pi - theta_j)`` for i < j — zz_map's phases."""
    theta = unit * _ANGLE_SCALE
    n = theta.shape[1]
    if n < 2:
        return np.empty((theta.shape[0], 0))
    cols = [
        (np.pi - theta[:, i]) * (np.pi - theta[:, j])
        for i in range(n)
        for j in range(i + 1, n)
    ]
    return np.column_stack(cols)


def zz_kernel(a_unit, b_unit) -> np.ndarray:
    """Gram of :func:`zz_map` features; zero for fewer than two features."""
    left, right = _validated_pair(a_unit, b_unit)
    phases_a = _pair_phases(left)
    phases_b = _pair_phases(right)
    if phases_a.shape[1] == 0:
        return np.zeros((left.shape[0], right.shape[0]))
    return (
        np.cos(phases_a) @ np.cos(phases_b).T
        + np.sin(phases_a) @ np.sin(phases_b).T
    )


def quantum_gram(
    a_std,
    b_std,
    a_unit,
    b_unit,
    kind: str,
    *,
    w_raw: float = 1.0,
    w_map: float = 1.0,
) -> np.ndarray:
    """Weighted raw + map Gram: ``w_raw * (a_std @ b_std.T) + w_map * k_map``.

    The two weights are the kernel answer to the measured failure of the
    explicit augmentation: one global ridge alpha over-shrank the raw columns,
    while here the raw and mapped parts shrink separately. ``linear`` ignores
    the map term entirely and is the dual of the plain ridge baseline.

    ``a_std``/``b_std`` are standardised raw features; ``a_unit``/``b_unit``
    are the same rows unit-scaled for the maps. Both fits belong to the
    training fold.
    """
    if kind not in KERNELS:
        raise ValueError(f"unknown kernel {kind!r}; available: {KERNELS}")
    for name, weight in (("w_raw", w_raw), ("w_map", w_map)):
        if not math.isfinite(float(weight)) or float(weight) < 0.0:
            raise ValueError(f"{name} must be finite and non-negative")
    left_std, right_std = _validated_pair(a_std, b_std)
    gram = float(w_raw) * (left_std @ right_std.T)
    if kind == "linear":
        return gram
    left_unit, right_unit = _validated_pair(a_unit, b_unit)
    if (
        left_unit.shape[0] != left_std.shape[0]
        or right_unit.shape[0] != right_std.shape[0]
    ):
        raise ValueError(
            "row-count mismatch between standardised and unit-scaled features"
        )
    kernel = angle_kernel if kind == "angle" else zz_kernel
    return gram + float(w_map) * kernel(left_unit, right_unit)


def kernel_ridge_predict(
    k_train,
    train_y,
    k_cross,
    alpha: float,
) -> np.ndarray:
    """Solve ``(K + alpha I) c = y - mean(y)`` and predict ``mean + Kc @ c``.

    For a linear kernel over standardised features this is exactly the primal
    ridge in :mod:`qlab.research.prediction` — the identity the tests assert —
    which makes the kernel lane's baseline auditable against the measured one.
    """
    gram = _as_matrix(k_train)
    cross = _as_matrix(k_cross)
    target = np.asarray(train_y, dtype=float)
    if target.ndim != 1 or not np.isfinite(target).all():
        raise ValueError("train_y must be a finite 1-D vector")
    if gram.shape[0] != gram.shape[1]:
        raise ValueError("k_train must be square")
    if len(target) != gram.shape[0]:
        raise ValueError("train_y length must match k_train")
    if cross.shape[1] != gram.shape[0]:
        raise ValueError("k_cross columns must match k_train rows")
    if not math.isfinite(float(alpha)) or float(alpha) <= 0.0:
        raise ValueError("alpha must be finite and positive")
    mean_y = float(target.mean())
    centered = target - mean_y
    system = gram + float(alpha) * np.eye(gram.shape[0])
    try:
        coefficients = np.linalg.solve(system, centered)
    except np.linalg.LinAlgError:
        coefficients = np.linalg.pinv(system) @ centered
    return mean_y + cross @ coefficients
