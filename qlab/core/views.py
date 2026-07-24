"""Entropy-pooling risk views — the qualitative lane's deterministic core.

Views are bounded, typed statements about *risk moments* (volatility,
correlation, tail mass) over a historical scenario panel. Sequential
minimum-relative-entropy reweighting (Meucci-style) tilts the scenario
probabilities to satisfy each view while **every per-asset mean stays pinned**
— a view can never smuggle in a return forecast. That pinning is the research
plan's news-ban, kept as arithmetic.

Each view solves the convex dual of

    min KL(p || p_prev)  s.t.  E_p[f] = b,  E_p[r_j] = m_j  for all assets j

so p_i ∝ p_prev_i · exp(λ'f_i); confidence blends the satisfied-view
distribution with its prior. A KL budget caps the total tilt, with a per-view
decomposition in the refusal so the caller knows which view to relax.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import optimize

_MIN_SCENARIOS = 60
_TAIL_Z = 2.0
_FATTER_SCALE = 1.25
_THINNER_SCALE = 0.80


def _check_confidence(confidence: float) -> float:
    c = float(confidence)
    if not 0.0 < c <= 1.0:
        raise ValueError(f"view confidence must be in (0, 1], got {c}")
    return c


@dataclass(frozen=True)
class VolView:
    """Target an asset's (reweighted) volatility; bounded around realized."""

    ticker: str
    target_vol: float
    confidence: float = 1.0
    lo_scale: float = 0.25
    hi_scale: float = 4.0

    def __post_init__(self) -> None:
        _check_confidence(self.confidence)
        if not np.isfinite(self.target_vol) or self.target_vol <= 0:
            raise ValueError("target_vol must be finite and positive")
        if not 0 < self.lo_scale < 1 <= self.hi_scale:
            raise ValueError("vol clamp scales must satisfy 0 < lo < 1 <= hi")

    def label(self) -> str:
        return f"vol({self.ticker}→{self.target_vol:.4f})"


@dataclass(frozen=True)
class CorrView:
    """Target the correlation of a pair; hard-clamped away from ±1."""

    ticker_a: str
    ticker_b: str
    target_corr: float
    confidence: float = 1.0

    def __post_init__(self) -> None:
        _check_confidence(self.confidence)
        if self.ticker_a == self.ticker_b:
            raise ValueError("correlation view needs two distinct tickers")
        if not -0.95 <= float(self.target_corr) <= 0.95:
            raise ValueError("target_corr must lie in [-0.95, 0.95]")

    def label(self) -> str:
        return f"corr({self.ticker_a},{self.ticker_b}→{self.target_corr:+.2f})"


@dataclass(frozen=True)
class TailView:
    """Scale an asset's two-sided tail mass beyond 2σ, fatter or thinner."""

    ticker: str
    direction: str
    confidence: float = 1.0

    def __post_init__(self) -> None:
        _check_confidence(self.confidence)
        if self.direction not in ("fatter", "thinner"):
            raise ValueError("direction must be 'fatter' or 'thinner'")

    def label(self) -> str:
        return f"tail({self.ticker} {self.direction})"


@dataclass(frozen=True)
class ViewsResult:
    probabilities: np.ndarray
    kl_total: float
    kl_per_view: dict[str, float]
    moments_before: dict[str, dict[str, float]]
    moments_after: dict[str, dict[str, float]]
    labels: tuple[str, ...] = field(default_factory=tuple)


def _kl(p: np.ndarray, q: np.ndarray) -> float:
    mask = p > 0
    return float(np.sum(p[mask] * np.log(p[mask] / q[mask])))


def _solve_tilt(p_prev: np.ndarray, F: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Minimum-KL reweighting: p ∝ p_prev·exp(λ'f), λ from the convex dual.

    ``F`` is (T, K) moment functions, ``b`` their required expectations. The
    dual D(λ) = log Σ p_prev·exp(F λ) − λ'b has gradient E_pλ[f] − b, so a
    quasi-Newton solve is exact-in-the-limit and cheap at K ≈ N+1.
    """
    F = np.asarray(F, dtype=float)
    b = np.asarray(b, dtype=float)

    def objective(lam: np.ndarray) -> tuple[float, np.ndarray]:
        z = F @ lam
        shift = z.max()
        w = p_prev * np.exp(z - shift)
        total = w.sum()
        p = w / total
        value = float(np.log(total) + shift - lam @ b)
        return value, F.T @ p - b

    result = optimize.minimize(
        objective, x0=np.zeros(F.shape[1]), jac=True, method="BFGS",
        options={"maxiter": 500, "gtol": 1e-10})
    if not np.all(np.isfinite(result.x)):
        raise ValueError("entropy-pooling dual diverged; view is infeasible "
                         "on this panel")
    grad_norm = float(np.linalg.norm(objective(result.x)[1]))
    if grad_norm > 1e-6:
        raise ValueError(
            f"entropy-pooling constraints unmet (residual {grad_norm:.2e}); "
            "the view is infeasible or near-infeasible on this panel")
    z = F @ result.x
    z -= z.max()
    p = p_prev * np.exp(z)
    return p / p.sum()


def _weighted_moments(panel: np.ndarray, p: np.ndarray,
                      tickers: list[str]) -> dict[str, dict[str, float]]:
    mean = p @ panel
    centered = panel - mean
    var = p @ (centered ** 2)
    out: dict[str, dict[str, float]] = {}
    for j, ticker in enumerate(tickers):
        out[ticker] = {"mean": float(mean[j]), "vol": float(np.sqrt(var[j]))}
    return out


def apply_views(panel: np.ndarray, tickers: list[str],
                views: list[VolView | CorrView | TailView],
                *, kl_budget: float = 0.25) -> ViewsResult:
    """Sequentially tilt scenario probabilities to satisfy bounded risk views.

    Order matters (documented, deliberate): each view is satisfied relative
    to the distribution the previous views produced, which is the sequential
    Meucci construction. Every step pins all per-asset means to their
    previous values, so the chain's means equal the sample means exactly.
    """
    panel = np.asarray(panel, dtype=float)
    if panel.ndim != 2:
        raise ValueError("panel must be a T x N returns matrix")
    n_obs, n_assets = panel.shape
    if n_obs < _MIN_SCENARIOS:
        raise ValueError(
            f"entropy pooling needs >= {_MIN_SCENARIOS} scenarios, got {n_obs}")
    if len(tickers) != n_assets:
        raise ValueError("tickers must match the panel's columns")
    if not np.all(np.isfinite(panel)):
        raise ValueError("panel contains non-finite returns")
    if not views:
        raise ValueError("apply_views requires at least one view")

    index = {t: j for j, t in enumerate(tickers)}
    uniform = np.full(n_obs, 1.0 / n_obs)
    p_prev = uniform.copy()
    kl_per_view: dict[str, float] = {}
    moments_before = _weighted_moments(panel, uniform, tickers)
    labels: list[str] = []

    for view in views:
        mean_prev = p_prev @ panel
        # Mean-pinning rows are shared by every view type: E_p[r_j] = m_j.
        rows = [panel[:, j] for j in range(n_assets)]
        targets = [mean_prev[j] for j in range(n_assets)]

        if isinstance(view, VolView):
            j = _require(index, view.ticker)
            var_prev = p_prev @ (panel[:, j] - mean_prev[j]) ** 2
            vol_prev = float(np.sqrt(var_prev))
            lo, hi = view.lo_scale * vol_prev, view.hi_scale * vol_prev
            if not lo <= view.target_vol <= hi:
                raise ValueError(
                    f"{view.label()} outside the clamp [{lo:.4f}, {hi:.4f}] "
                    "around the panel's realized vol")
            rows.append(panel[:, j] ** 2)
            targets.append(view.target_vol ** 2 + mean_prev[j] ** 2)
        elif isinstance(view, CorrView):
            a, b_ix = _require(index, view.ticker_a), _require(index, view.ticker_b)
            var_a = float(p_prev @ (panel[:, a] - mean_prev[a]) ** 2)
            var_b = float(p_prev @ (panel[:, b_ix] - mean_prev[b_ix]) ** 2)
            # Pin both variances too: with only means pinned the tilt would
            # move σ_a/σ_b and the achieved correlation would miss its target.
            rows.append(panel[:, a] ** 2)
            targets.append(var_a + mean_prev[a] ** 2)
            rows.append(panel[:, b_ix] ** 2)
            targets.append(var_b + mean_prev[b_ix] ** 2)
            rows.append(panel[:, a] * panel[:, b_ix])
            targets.append(view.target_corr * np.sqrt(var_a * var_b)
                           + mean_prev[a] * mean_prev[b_ix])
        elif isinstance(view, TailView):
            j = _require(index, view.ticker)
            vol_prev = float(np.sqrt(p_prev @ (panel[:, j] - mean_prev[j]) ** 2))
            indicator = (np.abs(panel[:, j] - mean_prev[j])
                         > _TAIL_Z * vol_prev).astype(float)
            base = float(p_prev @ indicator)
            if base <= 0:
                raise ValueError(
                    f"{view.label()}: no scenarios beyond {_TAIL_Z}σ; the "
                    "panel cannot express this view")
            scale = _FATTER_SCALE if view.direction == "fatter" else _THINNER_SCALE
            rows.append(indicator)
            targets.append(min(0.5, base * scale))
        else:  # pragma: no cover - typing guards this
            raise TypeError(f"unknown view type {type(view)!r}")

        p_view = _solve_tilt(p_prev, np.column_stack(rows), np.array(targets))
        p_next = view.confidence * p_view + (1.0 - view.confidence) * p_prev
        kl_per_view[view.label()] = _kl(p_next, p_prev)
        p_prev = p_next
        labels.append(view.label())

    kl_total = _kl(p_prev, uniform)
    if kl_total > kl_budget:
        decomposition = ", ".join(
            f"{label}: {value:.4f}" for label, value in kl_per_view.items())
        raise ValueError(
            f"KL budget exceeded: total {kl_total:.4f} > {kl_budget:.4f} "
            f"({decomposition}) — relax the largest view or its confidence")

    moments_after = _weighted_moments(panel, p_prev, tickers)
    for ticker in tickers:
        drift = abs(moments_after[ticker]["mean"]
                    - moments_before[ticker]["mean"])
        if drift > 1e-8:
            raise AssertionError(
                f"mean pinning violated for {ticker} (drift {drift:.2e}) — "
                "this is a bug, not a data problem")
    return ViewsResult(
        probabilities=p_prev, kl_total=kl_total, kl_per_view=kl_per_view,
        moments_before=moments_before, moments_after=moments_after,
        labels=tuple(labels))


def conditioned_moments(panel: np.ndarray,
                        probabilities: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(means, covariance) under the tilted scenario probabilities."""
    panel = np.asarray(panel, dtype=float)
    p = np.asarray(probabilities, dtype=float)
    if p.ndim != 1 or len(p) != panel.shape[0]:
        raise ValueError("probabilities must align with the panel's rows")
    if abs(p.sum() - 1.0) > 1e-9 or np.any(p < 0):
        raise ValueError("probabilities must be a distribution")
    mean = p @ panel
    centered = panel - mean
    cov = (centered * p[:, None]).T @ centered
    return mean, 0.5 * (cov + cov.T)


def _require(index: dict[str, int], ticker: str) -> int:
    if ticker not in index:
        raise ValueError(f"view names {ticker!r}, absent from the panel")
    return index[ticker]
