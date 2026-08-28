"""Moment estimation: covariance, shrinkage, denoising, co-moments, regimes.

This is the layer where ML genuinely helps *without* touching return prediction
(research-plan §7.2). The return-prediction ban is deliberate: no LSTM, no
sentiment, no news. What remains is estimator quality:

* **Ledoit–Wolf shrinkage** of the covariance (well-conditioned, robust to
  estimation noise);
* **Marchenko–Pastur / RMT denoising** of the eigenspectrum;
* **structured co-moment shrinkage** — sample cokurtosis is horrifically noisy,
  so we shrink coskew toward 0 and cokurt toward the Gaussian (Isserlis) tensor
  implied by the covariance (Martellini–Ziemann style, research-plan §7.2);
* **regime detection** — a realized-vol threshold in v1 (an honest, no-return-
  prediction signal), with an HMM as the documented v2.

All co-moments are *central* (not standardized) so that portfolio moments are
direct tensor contractions with the weight vector — the property the one-true
objective relies on.
"""

from __future__ import annotations

from dataclasses import replace
from math import comb

import numpy as np
import pandas as pd

from qlab.core.types import DataSnapshot, MomentSet

_TRADING_DAYS = 252


# ---------------------------------------------------------------------------
# Top-level estimator
# ---------------------------------------------------------------------------
def estimate_moments(
    snapshot: DataSnapshot,
    *,
    lookback_days: int = 756,
    shrinkage: str = "ledoit_wolf",
    denoise: str | None = "marchenko_pastur",
    comoment_shrinkage: float | str = 0.5,
    comoment_target: str = "isserlis",
    include_mu: bool = False,
    higher_moments: bool = True,
) -> MomentSet:
    """Estimate a :class:`MomentSet` from a point-in-time snapshot.

    Parameters mirror the agent-tunable judgment slots (window length,
    shrinkage intensity) that the moments-analyst is responsible for choosing.
    """
    rets = snapshot.log_returns(lookback_days=lookback_days)
    rets = rets.dropna(how="any")
    X = rets.to_numpy(dtype=float)
    T, n = X.shape
    tickers = list(rets.columns)

    # --- covariance + shrinkage + denoise -----------------------------------
    if shrinkage == "ledoit_wolf":
        cov, delta = ledoit_wolf(X)
    elif shrinkage == "nonlinear":
        cov, delta = nonlinear_shrinkage(X)
    elif shrinkage in (None, "none", "sample"):
        cov, delta = np.cov(X, rowvar=False, bias=True), 0.0
    else:
        raise ValueError(f"unknown shrinkage: {shrinkage!r}")

    if denoise == "marchenko_pastur":
        cov = marchenko_pastur_denoise(cov, T=T)
    elif denoise not in (None, "none"):
        raise ValueError(f"unknown denoise: {denoise!r}")

    diagnostics = {
        "T": int(T),
        "lookback_days": int(lookback_days),
        "shrinkage_intensity": float(delta),
        "denoise": denoise or "none",
        "condition_number": float(np.linalg.cond(cov)),
    }

    coskew = cokurt = None
    if higher_moments:
        coskew, cokurt = co_moments(
            X,
            cov,
            comoment_shrinkage=comoment_shrinkage,
            target=comoment_target,
        )
        diagnostics.update({
            "comoment_shrinkage": comoment_shrinkage,
            "comoment_target": comoment_target,
            "comoment_delta3": co_moments.last_deltas["delta3"],
            "comoment_delta4": co_moments.last_deltas["delta4"],
        })

    mu = shrunk_mean(X) if include_mu else None

    return MomentSet(
        tickers=tickers,
        as_of=snapshot.as_of,
        cov=cov,
        mu=mu,
        coskew=coskew,
        cokurt=cokurt,
        diagnostics=diagnostics,
    )


# ---------------------------------------------------------------------------
# Views conditioning (research stage — see qlab/algorithms/catalog.py)
# ---------------------------------------------------------------------------
def condition(ms: MomentSet, probabilities, *, panel,
              views_run_id: str) -> MomentSet:
    """A moment set whose covariance is views-tilted and whose mean is ``ms``'s.

    The mean is copied, not recomputed: ``conditioned_moments`` returns a
    tilted mean too, and the entire safety of the qualitative lane is that no
    view can move one. That is re-established here as arithmetic — the tilted
    mean is computed, measured against the parent's, then discarded — rather
    than trusted from the pooling code. The measurement is kept in
    ``provenance["mean_pinning_max_abs"]`` so a tilt that would have moved a
    mean is visible in the audit instead of merely absent from the tensor.

    ``panel`` must be the scenario panel the probabilities were solved on, in
    this moment set's column order. The higher-moment tensors are carried
    through unconditioned, which is why the catalog entry that consumes this is
    covariance-only: mixing a tilted Sigma with an untilted cokurt would blend
    two different distributions in one objective (the same reason
    ``qlab.signals.condition`` refuses MVSK).
    """
    from qlab.core.views import conditioned_moments

    panel = np.asarray(panel, dtype=float)
    if panel.ndim != 2 or panel.shape[1] != ms.n:
        width = panel.shape[1] if panel.ndim == 2 else 0
        raise ValueError(
            f"panel has {width} columns but this moment set has {ms.n} "
            "tickers; conditioning requires the panel the views were solved "
            "on, in the same column order")
    tilted_mean, cov = conditioned_moments(panel, np.asarray(probabilities))
    reference = ms.mu if ms.mu is not None else panel.mean(axis=0)
    drift = float(np.max(np.abs(tilted_mean - np.asarray(reference, dtype=float))))
    return replace(
        ms,
        cov=cov,
        provenance={
            **(ms.provenance or {}),
            "parent": ms.content_hash(),
            "views_run_id": str(views_run_id),
            "mean_pinning_max_abs": drift,
        },
    )


# ---------------------------------------------------------------------------
# Ledoit–Wolf shrinkage (scaled-identity target — the well-conditioned form)
# ---------------------------------------------------------------------------
def ledoit_wolf(X: np.ndarray) -> tuple[np.ndarray, float]:
    """Ledoit–Wolf (2004) shrinkage of the sample covariance to a scaled identity.

    Returns ``(shrunk_cov, intensity)``. The intensity is the optimal convex
    weight on the structured target; it is exactly the kind of number the agent
    layer must *not* invent — it is computed here and logged.

    A constant-correlation target is a documented drop-in alternative; the
    scaled-identity form is used for its clean, verifiable closed form.
    """
    T, n = X.shape
    Xc = X - X.mean(axis=0, keepdims=True)
    S = (Xc.T @ Xc) / T

    # inner product <A,B> = trace(A B') / n
    m = np.trace(S) / n                          # <S, I>
    d2 = np.sum((S - m * np.eye(n)) ** 2) / n    # ||S - m I||_F^2 / n
    # b̄^2 = mean over t of ||x_t x_t' - S||_F^2 / n, scaled by 1/T
    b2_bar = 0.0
    for t in range(T):
        xt = Xc[t : t + 1].T @ Xc[t : t + 1]     # outer product x_t x_t'
        b2_bar += np.sum((xt - S) ** 2) / n
    b2_bar /= T * T
    b2 = min(b2_bar, d2)
    a2 = d2 - b2
    if d2 <= 0:
        return S, 0.0
    intensity = b2 / d2
    shrunk = intensity * m * np.eye(n) + (a2 / d2) * S
    return shrunk, float(intensity)


# ---------------------------------------------------------------------------
# Ledoit–Wolf (2020) analytical *nonlinear* shrinkage
# ---------------------------------------------------------------------------
def nonlinear_shrinkage(X: np.ndarray) -> tuple[np.ndarray, float]:
    """Ledoit–Wolf (2020) *analytical* nonlinear shrinkage of the covariance.

    An Epanechnikov-kernel estimate of the sample spectral density and its
    Hilbert transform give a *per-eigenvalue* shrinkage that strictly dominates
    linear Ledoit–Wolf as ``n`` grows with ``T`` (Ledoit & Wolf 2020,
    *Analytical Nonlinear Shrinkage of Large-Dimensional Covariance Matrices*).

    ``X`` is ``(T, n)`` (observations × assets). Returns ``(cov, shift)`` where
    ``shift`` is the mean absolute eigenvalue displacement normalised by the
    mean sample eigenvalue — an "intensity" diagnostic analogous to the linear
    shrinkage weight, logged like every other estimator judgment.

    Faithful port of the published ``analytical_shrinkage`` reference: the
    bandwidth is *per-eigenvalue* (``H = h·λ_j`` with ``h = T**(-1/3)``) and the
    Hilbert-transform term carries the ``1/π`` factor (their Eqs. 4.7/4.8/4.3,
    with the ``n > T`` rank-deficient limit from Eqs. C.4/C.5/C.8). A tiny
    relative eigenvalue floor guards the degenerate case (demeaning drops the
    rank by one) so the bandwidth ``H`` never vanishes and the result stays
    finite, symmetric and PSD.
    """
    T, n = X.shape
    Xc = X - X.mean(axis=0, keepdims=True)
    S = (Xc.T @ Xc) / T
    lam, U = np.linalg.eigh(S)
    order = np.argsort(lam)
    lam = np.clip(lam[order], 0.0, None)
    U = U[:, order]

    # the min(n, T) potentially-nonzero eigenvalues (drop the structural zeros)
    lam_keep = lam[max(0, n - T):]
    m = lam_keep.shape[0]                           # = min(n, T)
    floor = max(float(lam_keep.max()), 1e-300) * 1e-12
    lam_keep = np.clip(lam_keep, floor, None)

    h = T ** (-1.0 / 3.0)                           # bandwidth (Eq. 4.9)
    L = np.tile(lam_keep.reshape(-1, 1), (1, m))    # L[i, j] = λ_i
    H = h * L.T                                     # H[i, j] = h·λ_j
    x = (L - L.T) / H

    # Epanechnikov density estimate f̃ (Eq. 4.7) and its Hilbert transform (4.8)
    sqrt5 = np.sqrt(5.0)
    ftilde = (3.0 / 4.0 / sqrt5) * np.mean(
        np.maximum(1.0 - x ** 2 / 5.0, 0.0) / H, axis=1)
    Hftemp = ((-3.0 / 10.0 / np.pi) * x
              + (3.0 / 4.0 / sqrt5 / np.pi) * (1.0 - x ** 2 / 5.0)
              * np.log(np.abs((sqrt5 - x) / (sqrt5 + x))))
    boundary = np.abs(x) == sqrt5                   # log term vanishes there
    Hftemp[boundary] = (-3.0 / 10.0 / np.pi) * x[boundary]
    Hftilde = np.mean(Hftemp / H, axis=1)

    q = n / T
    if n <= T:                                      # Eq. 4.3
        dtilde = lam_keep / ((np.pi * q * lam_keep * ftilde) ** 2
                             + (1.0 - q - np.pi * q * lam_keep * Hftilde) ** 2)
    else:                                           # Eqs. C.4/C.5/C.8
        log_arg = (1.0 + sqrt5 * h) / max(1.0 - sqrt5 * h, 1e-12)
        Hftilde0 = (1.0 / np.pi) * (
            3.0 / 10.0 / h ** 2
            + 3.0 / 4.0 / sqrt5 / h * (1.0 - 1.0 / 5.0 / h ** 2) * np.log(log_arg)
        ) * np.mean(1.0 / lam_keep)
        dtilde0 = 1.0 / (np.pi * (n - T) / T * Hftilde0)
        dtilde1 = lam_keep / (np.pi ** 2 * lam_keep ** 2 * (ftilde ** 2 + Hftilde ** 2))
        dtilde = np.concatenate([np.full(n - T, dtilde0), dtilde1])

    dtilde = np.where(np.isfinite(dtilde), dtilde, floor)
    dtilde = np.clip(dtilde, floor, None)
    cov = (U * dtilde) @ U.T
    cov = (cov + cov.T) / 2.0
    shift = float(np.mean(np.abs(dtilde - lam)) / max(float(np.mean(lam)), 1e-18))
    return cov, shift


# ---------------------------------------------------------------------------
# Marchenko–Pastur / RMT denoising
# ---------------------------------------------------------------------------
def marchenko_pastur_denoise(cov: np.ndarray, T: int) -> np.ndarray:
    """Clean the covariance eigenspectrum with the Marchenko–Pastur cutoff.

    Eigenvalues of the *correlation* matrix below the MP upper edge are treated
    as noise and replaced by their average (trace-preserving); eigenvalues above
    it are signal and kept (López de Prado's constant-residual method).
    """
    n = cov.shape[0]
    if n < 2 or T <= n:
        return cov
    d = np.sqrt(np.clip(np.diag(cov), 1e-18, None))
    corr = cov / np.outer(d, d)
    corr = (corr + corr.T) / 2.0

    vals, vecs = np.linalg.eigh(corr)
    q = T / n
    lam_plus = (1.0 + np.sqrt(1.0 / q)) ** 2       # MP upper edge, unit variance

    is_signal = vals > lam_plus
    if is_signal.all() or (~is_signal).all():
        cleaned = vals.copy()
        if (~is_signal).any():
            cleaned[~is_signal] = vals[~is_signal].mean()
    else:
        cleaned = vals.copy()
        noise_avg = vals[~is_signal].mean()
        cleaned[~is_signal] = noise_avg

    corr_clean = (vecs * cleaned) @ vecs.T
    # restore unit diagonal, then rescale back to covariance
    dd = np.sqrt(np.clip(np.diag(corr_clean), 1e-18, None))
    corr_clean = corr_clean / np.outer(dd, dd)
    cov_clean = corr_clean * np.outer(d, d)
    return (cov_clean + cov_clean.T) / 2.0


# ---------------------------------------------------------------------------
# Higher co-moments with structured shrinkage
# ---------------------------------------------------------------------------
def co_moments(
    X: np.ndarray,
    cov: np.ndarray,
    *,
    comoment_shrinkage: float | str = 0.5,
    target: str = "isserlis",
) -> tuple[np.ndarray, np.ndarray]:
    """Central co-moments with structured, optionally automatic shrinkage.

    ``comoment_shrinkage`` is either a fixed convex weight or ``"auto"``, which
    derives separate third- and fourth-order weights from the ratio of unique
    tensor parameters to observations. ``target`` is ``"isserlis"`` (Gaussian)
    or ``"one_factor"`` (an equal-weight common-factor target that retains
    systematic skew and excess kurtosis while shrinking idiosyncratic noise).
    """
    Xc = X - X.mean(axis=0, keepdims=True)
    T, n = Xc.shape

    coskew = np.einsum("ti,tj,tk->ijk", Xc, Xc, Xc) / T
    cokurt = np.einsum("ti,tj,tk,tl->ijkl", Xc, Xc, Xc, Xc) / T

    if isinstance(comoment_shrinkage, str):
        if comoment_shrinkage != "auto":
            raise ValueError(
                "comoment_shrinkage must be a number in [0, 1] or 'auto'"
            )
        rho3 = comb(n + 2, 3) / T
        rho4 = comb(n + 3, 4) / T
        delta3 = float(np.clip(rho3 / (1.0 + rho3), 0.2, 0.9))
        delta4 = float(np.clip(rho4 / (1.0 + rho4), 0.2, 0.9))
    else:
        delta3 = delta4 = float(np.clip(comoment_shrinkage, 0.0, 1.0))
    co_moments.last_deltas = {"delta3": delta3, "delta4": delta4}

    isserlis_target = (
        np.einsum("ij,kl->ijkl", cov, cov)
        + np.einsum("ik,jl->ijkl", cov, cov)
        + np.einsum("il,jk->ijkl", cov, cov)
    )
    if target == "isserlis":
        coskew_target = np.zeros_like(coskew)
        cokurt_target = isserlis_target
    elif target == "one_factor":
        factor = Xc.mean(axis=1)
        factor_variance = max(float(np.var(factor)), 1e-18)
        betas = (Xc.T @ factor) / (T * factor_variance)
        factor_m3 = float(np.mean(factor ** 3))
        factor_m4 = float(np.mean(factor ** 4))
        coskew_target = factor_m3 * np.einsum(
            "i,j,k->ijk", betas, betas, betas,
        )
        factor_excess_m4 = factor_m4 - 3.0 * factor_variance ** 2
        cokurt_target = isserlis_target + factor_excess_m4 * np.einsum(
            "i,j,k,l->ijkl", betas, betas, betas, betas,
        )
    else:
        raise ValueError(f"unknown comoment target {target!r}")

    if delta3 > 0:
        coskew = (1.0 - delta3) * coskew + delta3 * coskew_target
    if delta4 > 0:
        cokurt = (1.0 - delta4) * cokurt + delta4 * cokurt_target
    return coskew, cokurt


def shrunk_mean(X: np.ndarray, intensity: float = 0.5) -> np.ndarray:
    """James–Stein-style shrinkage of the sample mean toward the grand mean.

    Only used for the secondary max-utility objective variant; the primary
    objective is risk-only and carries no ``mu`` at all.
    """
    m = X.mean(axis=0)
    grand = m.mean()
    return (1 - intensity) * m + intensity * grand


# ---------------------------------------------------------------------------
# Portfolio central moments (used by objective + metrics)
# ---------------------------------------------------------------------------
def portfolio_moments(w: np.ndarray, ms: MomentSet) -> dict[str, float]:
    """Central portfolio moments implied by weights and a MomentSet.

    Returns variance, third and fourth central moments, plus standardized
    skewness/kurtosis — the direct test of whether optimizing tail shape
    actually delivered tail shape.
    """
    w = np.asarray(w, dtype=float)
    var = float(w @ ms.cov @ w)
    out = {"variance": var, "vol": float(np.sqrt(max(var, 0.0)))}
    if ms.coskew is not None:
        m3 = float(np.einsum("ijk,i,j,k->", ms.coskew, w, w, w))
        out["m3"] = m3
        out["skew"] = m3 / (var ** 1.5 + 1e-18)
    if ms.cokurt is not None:
        m4 = float(np.einsum("ijkl,i,j,k,l->", ms.cokurt, w, w, w, w))
        out["m4"] = m4
        out["kurt"] = m4 / (var ** 2 + 1e-18)
    return out


# ---------------------------------------------------------------------------
# Regime detection (v1: realized-vol threshold; v2: HMM — documented stub)
# ---------------------------------------------------------------------------
def detect_regime(
    snapshot: DataSnapshot,
    *,
    window: int = 63,
    quantile: float = 0.80,
) -> dict[str, object]:
    """Classify the current regime as ``calm`` or ``stress``.

    v1 is a trailing-realized-vol threshold: if the recent equal-weight
    portfolio vol exceeds its historical ``quantile``, we are in ``stress``.
    This is an honest ML-free signal with no return prediction. An HMM
    (2–3 states) is the documented v2 upgrade — same interface, richer model.
    """
    rets = snapshot.log_returns().dropna(how="any")
    if len(rets) < window + 5:
        return {"regime": "calm", "signal": 0.0, "method": "insufficient_data"}
    port = rets.mean(axis=1)                      # equal-weight proxy
    rolling_vol = port.rolling(window).std() * np.sqrt(_TRADING_DAYS)
    rolling_vol = rolling_vol.dropna()
    current = float(rolling_vol.iloc[-1])
    threshold = float(rolling_vol.quantile(quantile))
    regime = "stress" if current > threshold else "calm"
    return {
        "regime": regime,
        "signal": current,
        "threshold": threshold,
        "window": window,
        "method": "realized_vol_threshold",
    }
