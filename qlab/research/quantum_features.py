"""Quantum-inspired feature maps, evaluated classically.

"Quantum-inspired" here has a precise meaning and a narrow claim. These are the
*encodings* used by variational quantum classifiers — the angle map from
Stoudenmire & Schwab's tensor-network work, and the second-order Pauli-ZZ map
from Havlíček et al.'s quantum kernel — written out as ordinary numpy. No
circuit is simulated and none is needed: for a product-state encoding the
feature vector is a closed form, and it is the *kernel* between two such states
that is expensive to evaluate quantumly, not the state itself.

So the honest claim is: these are structured nonlinear bases that a quantum
circuit would have produced, available at classical cost. They are not a
quantum speedup, they do not approximate one, and nothing here becomes faster
or better by running it on hardware.

Two properties make them worth trying on this desk specifically:

* **Pointwise and stateless.** Every output row is a function of that row
  alone. There is no fit, no rolling window, no cross-sectional normalisation —
  so an augmented feature cannot leak the future into a walk-forward split. The
  same cannot be said of most learned embeddings, which is why they would need
  to be fitted inside each fold.
* **Explicit interactions.** The ZZ map's whole content is the pairwise term.
  Realized volatility, turbulence, and dispersion plausibly interact — vol is
  higher when dispersion rises *and* turbulence is already elevated — and a
  linear model cannot see that without being handed the product.

Whether either actually helps is an empirical question this module does not
answer. See ``planning-docs/2026-07-30-ml-lane.md`` for the measurement.
"""

from __future__ import annotations

import numpy as np

# Feature values are scaled into angles before encoding. Half of pi keeps a
# unit-interval feature inside one monotone quarter-turn: cos and sin are then
# both injective over the range, so the map loses no information. Using a full
# turn would fold distinct inputs onto the same point.
_ANGLE_SCALE = np.pi / 2.0

AUGMENTATIONS = ("none", "angle", "zz", "angle_zz")


def _as_matrix(X) -> np.ndarray:
    arr = np.asarray(X, dtype=float)
    if arr.ndim != 2:
        raise ValueError(f"expected a 2-D feature matrix, got shape {arr.shape}")
    if not np.isfinite(arr).all():
        # Fail loud: a NaN silently propagating through cos/sin produces a
        # NaN column that ridge then fits around, and the failure appears as
        # an unexplained drop in IC rather than as an error.
        raise ValueError("feature matrix contains non-finite values")
    return arr


def scale_to_unit(X, *, lo: np.ndarray | None = None,
                  hi: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray,
                                                         np.ndarray]:
    """Map columns into [0, 1] using bounds supplied by the caller.

    Bounds are returned rather than stored, and must be computed on training
    data alone and then passed in for the test fold. A min/max taken over the
    whole sample is look-ahead — the classic way an "obviously stateless"
    preprocessing step leaks the future.
    """
    arr = _as_matrix(X)
    if lo is None or hi is None:
        lo = arr.min(axis=0)
        hi = arr.max(axis=0)
    lo = np.asarray(lo, dtype=float)
    hi = np.asarray(hi, dtype=float)
    span = hi - lo
    # A degenerate column (constant in training) maps to its midpoint rather
    # than dividing by zero. It carries no information either way.
    span = np.where(span > 0, span, 1.0)
    scaled = (arr - lo) / span
    return np.clip(scaled, 0.0, 1.0), lo, hi


def angle_map(X_unit: np.ndarray) -> np.ndarray:
    """The product-state angle encoding: each feature x -> [cos(ax), sin(ax)].

    This is the single-qubit half of any amplitude-encoded circuit, and the
    feature map behind tensor-network classifiers. It is a smooth, bounded,
    monotone-in-quarter-turn basis — the nonlinearity a linear model gets for
    free without a kernel.
    """
    arr = _as_matrix(X_unit)
    theta = arr * _ANGLE_SCALE
    # Interleaved per feature (cos_0, sin_0, cos_1, sin_1, ...) so a feature's
    # two components stay adjacent — readable when inspecting coefficients.
    out = np.empty((arr.shape[0], 2 * arr.shape[1]))
    out[:, 0::2] = np.cos(theta)
    out[:, 1::2] = np.sin(theta)
    return out


def zz_map(X_unit: np.ndarray) -> np.ndarray:
    """Second-order Pauli-ZZ interaction terms, IQP-style.

    The ZZ feature map's data-dependent phase on qubits (i, j) is
    ``(pi - x_i)(pi - x_j)``; that product *is* the entangling content of the
    circuit, and it is what a linear model cannot construct for itself. Emitted
    as cos/sin of the phase so the basis stays bounded — an unbounded product
    term would dominate the ridge penalty purely by scale.

    Returns ``n*(n-1)`` columns for n features: one cos and one sin per pair.
    """
    arr = _as_matrix(X_unit)
    n = arr.shape[1]
    if n < 2:
        return np.empty((arr.shape[0], 0))
    theta = arr * _ANGLE_SCALE
    cols = []
    for i in range(n):
        for j in range(i + 1, n):
            phase = (np.pi - theta[:, i]) * (np.pi - theta[:, j])
            cols.append(np.cos(phase))
            cols.append(np.sin(phase))
    return np.column_stack(cols)


def augment(X, kind: str = "none", *, lo=None, hi=None):
    """Apply an augmentation, returning ``(features, lo, hi)``.

    The bounds travel with the result so a caller can fit them on a training
    fold and reuse them on the matching test fold. Recomputing them per fold
    from the fold's own data would leak.
    """
    if kind not in AUGMENTATIONS:
        raise ValueError(
            f"unknown augmentation {kind!r}; available: {AUGMENTATIONS}")
    arr = _as_matrix(X)
    if kind == "none":
        return arr, None, None
    unit, lo_out, hi_out = scale_to_unit(arr, lo=lo, hi=hi)
    parts = [arr]           # always keep the raw features
    if kind in ("angle", "angle_zz"):
        parts.append(angle_map(unit))
    if kind in ("zz", "angle_zz"):
        parts.append(zz_map(unit))
    return np.column_stack(parts), lo_out, hi_out


def augmented_width(n_features: int, kind: str) -> int:
    """Column count after augmentation — the cost side of the trade.

    ZZ is quadratic in the feature count, so this is what tells a caller
    whether the design matrix is about to outgrow the sample.
    """
    if kind not in AUGMENTATIONS:
        raise ValueError(f"unknown augmentation {kind!r}")
    width = n_features
    if kind in ("angle", "angle_zz"):
        width += 2 * n_features
    if kind in ("zz", "angle_zz"):
        width += n_features * (n_features - 1)
    return width
