"""Moment estimation: shrinkage, denoising, co-moment structure, regimes."""

from __future__ import annotations

import numpy as np

from qlab.core.moments import (
    co_moments,
    ledoit_wolf,
    marchenko_pastur_denoise,
    portfolio_moments,
)


def test_covariance_is_psd_and_symmetric(moment_set):
    cov = moment_set.cov
    assert np.allclose(cov, cov.T, atol=1e-10)
    eig = np.linalg.eigvalsh(cov)
    assert eig.min() > -1e-8            # PSD (numerical tolerance)


def test_ledoit_wolf_intensity_in_unit_interval():
    rng = np.random.default_rng(0)
    X = rng.standard_normal((200, 8)) * 0.01
    cov, intensity = ledoit_wolf(X)
    assert 0.0 <= intensity <= 1.0
    assert np.allclose(cov, cov.T)


def test_denoise_preserves_shape_and_diagonal():
    rng = np.random.default_rng(1)
    X = rng.standard_normal((300, 6)) * 0.01
    cov = np.cov(X, rowvar=False)
    clean = marchenko_pastur_denoise(cov, T=300)
    assert clean.shape == cov.shape
    # variances roughly preserved
    assert np.allclose(np.diag(clean), np.diag(cov), rtol=0.2)


def test_comoment_tensor_shapes_and_gaussian_shrink():
    rng = np.random.default_rng(2)
    X = rng.standard_normal((400, 5)) * 0.01
    cov = np.cov(X, rowvar=False)
    # full shrink toward Gaussian → coskew ~ 0
    coskew, cokurt = co_moments(X, cov, comoment_shrinkage=1.0)
    assert coskew.shape == (5, 5, 5)
    assert cokurt.shape == (5, 5, 5, 5)
    assert np.allclose(coskew, 0.0)
    # cokurt target is the Isserlis combination
    isserlis = (np.einsum("ij,kl->ijkl", cov, cov)
                + np.einsum("ik,jl->ijkl", cov, cov)
                + np.einsum("il,jk->ijkl", cov, cov))
    assert np.allclose(cokurt, isserlis)


def test_portfolio_moments_match_direct(moment_set):
    n = moment_set.n
    w = np.full(n, 1.0 / n)
    pm = portfolio_moments(w, moment_set)
    assert abs(pm["variance"] - w @ moment_set.cov @ w) < 1e-12
    assert "skew" in pm and "kurt" in pm


def test_regime_returns_valid_label(snap):
    from qlab.core.moments import detect_regime

    r = detect_regime(snap)
    assert r["regime"] in ("calm", "stress")
