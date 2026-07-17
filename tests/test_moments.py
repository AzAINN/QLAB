"""Moment estimation: shrinkage, denoising, co-moment structure, regimes."""

from __future__ import annotations

import numpy as np
import pandas as pd

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


def test_auto_comoment_shrinkage_decreases_with_sample_size():
    rng = np.random.default_rng(4)
    n_assets = 5
    delta4 = []
    for observations in (150, 3000):
        returns = rng.standard_t(5, (observations, n_assets)) * 0.01
        covariance, _ = ledoit_wolf(returns)
        co_moments(returns, covariance, comoment_shrinkage="auto")
        delta4.append(co_moments.last_deltas["delta4"])

    assert delta4[1] < delta4[0]
    assert 0.2 <= delta4[0] <= 0.9


def test_one_factor_target_tracks_factor_skew():
    rng = np.random.default_rng(9)
    observations, n_assets = 2000, 4
    factor = rng.gamma(2.0, 1.0, observations) - 2.0
    returns = (
        np.outer(factor, np.ones(n_assets)) * 0.01
        + rng.normal(0, 0.001, (observations, n_assets))
    )
    covariance, _ = ledoit_wolf(returns)

    factor_coskew, _ = co_moments(
        returns,
        covariance,
        comoment_shrinkage=1.0,
        target="one_factor",
    )
    gaussian_coskew, _ = co_moments(
        returns,
        covariance,
        comoment_shrinkage=1.0,
        target="isserlis",
    )
    assert np.abs(factor_coskew).max() > 0
    assert np.abs(gaussian_coskew).max() == 0


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


# ---------------------------------------------------------------------------
# Nonlinear shrinkage (Ledoit–Wolf 2020 analytical kernel estimator)
# ---------------------------------------------------------------------------
def test_nonlinear_shrinkage_beats_sample_in_frobenius():
    rng = np.random.default_rng(11)
    n, T = 30, 120
    truth = np.eye(n)
    X = rng.multivariate_normal(np.zeros(n), truth, size=T)
    from qlab.core.moments import nonlinear_shrinkage
    S = np.cov(X, rowvar=False, bias=True)
    NL, _ = nonlinear_shrinkage(X)
    assert np.linalg.norm(NL - truth) < np.linalg.norm(S - truth)
    assert np.all(np.linalg.eigvalsh(NL) > 0)


def test_estimate_moments_accepts_nonlinear():
    from datetime import date
    from qlab.core.moments import estimate_moments
    from qlab.core.types import DataSnapshot
    rng = np.random.default_rng(1)
    idx = pd.bdate_range("2018-01-01", periods=800)
    px = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, 0.01, (800, 5)), axis=0)),
                      index=idx, columns=list("ABCDE"))
    snap = DataSnapshot(list(px.columns), px, idx[-1].date())
    ms = estimate_moments(snap, shrinkage="nonlinear", denoise=None,
                          higher_moments=False)
    assert ms.cov.shape == (5, 5)


def test_nonlinear_shrinkage_approaches_sample_and_is_psd():
    """Beyond the brief: with T >> n the estimator should recover truth far
    better than the small-sample case, and the output must be symmetric + PSD
    for every shape (including the degenerate n > T branch)."""
    from qlab.core.moments import nonlinear_shrinkage

    # (a) small sample n=30, T=120
    rng1 = np.random.default_rng(11)
    n1, T1 = 30, 120
    truth1 = np.eye(n1)
    X1 = rng1.multivariate_normal(np.zeros(n1), truth1, size=T1)
    NL1, _ = nonlinear_shrinkage(X1)
    rel1 = np.linalg.norm(NL1 - truth1) / np.linalg.norm(truth1)

    # (b) T >> n with a non-trivial diagonal truth
    rng2 = np.random.default_rng(123)
    n2, T2 = 5, 5000
    truth2 = np.diag(np.arange(1, n2 + 1).astype(float))
    X2 = rng2.multivariate_normal(np.zeros(n2), truth2, size=T2)
    NL2, shift2 = nonlinear_shrinkage(X2)
    rel2 = np.linalg.norm(NL2 - truth2) / np.linalg.norm(truth2)

    assert rel2 < rel1                       # more data -> tighter recovery
    assert rel2 < 0.05                       # and lands close to the truth
    assert shift2 >= 0.0

    # (c) symmetric + PSD everywhere, including n > T (rank-deficient)
    rng3 = np.random.default_rng(7)
    NL3, _ = nonlinear_shrinkage(rng3.standard_normal((40, 50)))
    for A in (NL1, NL2, NL3):
        assert np.allclose(A, A.T, atol=1e-10)
        assert np.linalg.eigvalsh((A + A.T) / 2.0).min() > -1e-8
