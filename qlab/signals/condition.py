"""Regime-conditional moment estimation (signals v1: lambda-mixing).

Sigma(lam) = (1 - lam) * Sigma_calm + lam * Sigma_stress, with lam supplied by
the composite hard-signal regime (later: clamped LLM views, roadmap §3). The
solver stack is untouched - conditioning only changes the coefficients.

Conditioning currently lambda-mixes ONLY Sigma. The coskew/cokurt tensors
(whose Isserlis/one-factor targets embed the UNCONDITIONED covariance) are not
conditioned, so mixing them in unmodified alongside a conditioned Sigma would
silently blend two different regime covariances in one MVSK objective. Until
the higher-moment tensors are conditioned consistently, ``qlab.arms.estimate``
rejects ``regime_conditional=True`` for any objective that needs them (MVSK) -
see the ``ValueError`` raised there. Separately, note that ``condition_covariance``
returns an LW-only mix (no Marchenko-Pastur denoise applied to the blended
result), so a conditioned arm differs from its unconditioned twin on both the
regime-mixing axis and the denoising axis.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from qlab.core.moments import ledoit_wolf

_TRADING_DAYS = 252


def regime_labels(returns: pd.DataFrame, window: int = 63,
                  quantile: float = 0.8) -> np.ndarray:
    port = returns.mean(axis=1)
    vol = port.rolling(window).std() * np.sqrt(_TRADING_DAYS)
    thresh = vol.quantile(quantile)
    return (vol > thresh).fillna(False).to_numpy()


def _psd_floor(cov: np.ndarray) -> np.ndarray:
    cov = (cov + cov.T) / 2.0
    vals, vecs = np.linalg.eigh(cov)
    return (vecs * np.clip(vals, 1e-12, None)) @ vecs.T


def condition_covariance(X: np.ndarray, labels: np.ndarray, lam: float) -> np.ndarray:
    n = X.shape[1]
    lam = float(np.clip(lam, 0.0, 1.0))
    min_obs = max(3 * n, 60)
    calm, stress = X[~labels], X[labels]
    cov_all, _ = ledoit_wolf(X)
    cov_calm, _ = ledoit_wolf(calm) if len(calm) >= min_obs else (cov_all, 0.0)
    cov_stress, _ = ledoit_wolf(stress) if len(stress) >= min_obs else (cov_all, 0.0)
    return _psd_floor((1.0 - lam) * cov_calm + lam * cov_stress)
