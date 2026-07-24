"""Exact classical relevance/redundancy selection.

The selection objective is the staged classical twin of the offline selection
QUBO: reward assets with strong standalone diversification value and penalize
selecting correlated pairs. Cardinality is enforced by enumerating only
``C(N, k)`` feasible baskets, so no penalty coefficient can weaken the exact
``k`` constraint.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations, islice
from typing import Literal, Sequence

import numpy as np
import pandas as pd


MAX_EXACT_ASSETS = 25
_ENUMERATION_BATCH_SIZE = 16_384
RelevanceMode = Literal["inverse_vol", "decorrelation"]


@dataclass
class SelectionResult:
    """One certified minimum over all exactly-``k`` baskets."""

    selected: list[str]
    score: float
    contributions: dict[str, float]

    def to_dict(self) -> dict:
        return {
            "selected": list(self.selected),
            "score": float(self.score),
            "contributions": dict(self.contributions),
        }


def build_selection_matrix(
    returns: pd.DataFrame | np.ndarray | None = None,
    *,
    covariance: np.ndarray | None = None,
    volatilities: Sequence[float] | np.ndarray | None = None,
    relevance: RelevanceMode = "inverse_vol",
    relevance_weight: float = 1.0,
    redundancy_weight: float = 1.0,
) -> np.ndarray:
    """Build the symmetric matrix for the exact binary objective.

    Exactly one of ``returns`` or ``covariance`` must be supplied. When a
    covariance is supplied, volatilities may be supplied alongside it or are
    derived from its diagonal.

    Standalone relevance is a reward, so it enters the diagonal with a minus
    sign in the minimization objective. The mathematical pair penalty is
    ``redundancy_weight * abs(correlation)``. A symmetric ``Q`` stores half of
    that value in each off-diagonal position because ``w.T @ Q @ w`` visits
    every selected pair twice.
    """
    cov, vols = _covariance_and_volatilities(
        returns=returns,
        covariance=covariance,
        volatilities=volatilities,
    )
    relevance_weight = _nonnegative_finite("relevance_weight", relevance_weight)
    redundancy_weight = _nonnegative_finite("redundancy_weight", redundancy_weight)

    corr = cov / np.outer(vols, vols)
    corr = (corr + corr.T) * 0.5
    off_diagonal = ~np.eye(len(vols), dtype=bool)
    if np.any(np.abs(corr[off_diagonal]) > 1.0 + 1e-8):
        raise ValueError(
            "covariance and volatilities imply an absolute correlation above 1"
        )
    corr = np.clip(corr, -1.0, 1.0)
    np.fill_diagonal(corr, 1.0)
    absolute_corr = np.abs(corr)

    if relevance == "inverse_vol":
        standalone = 1.0 / vols
        standalone /= standalone.max()
    elif relevance == "decorrelation":
        if len(vols) == 1:
            standalone = np.ones(1, dtype=float)
        else:
            standalone = 1.0 - (
                (absolute_corr.sum(axis=1) - 1.0) / (len(vols) - 1)
            )
    else:
        raise ValueError(
            "relevance must be 'inverse_vol' or 'decorrelation'"
        )

    q = 0.5 * redundancy_weight * absolute_corr
    np.fill_diagonal(q, -relevance_weight * standalone)
    return q


def solve_exact_selection(
    q: np.ndarray,
    tickers: Sequence[str],
    k: int,
) -> SelectionResult:
    """Minimize ``w.T @ q @ w`` over binary vectors with exactly ``k`` ones.

    Feasible combinations are evaluated in vectorized batches. Combination
    order is lexicographic in the supplied ticker order, and the first optimum
    wins an exact tie, making repeated calls deterministic.
    """
    matrix = np.asarray(q, dtype=float)
    if matrix.ndim != 2 or matrix.shape[0] != matrix.shape[1]:
        raise ValueError("selection matrix must be square")
    if not np.isfinite(matrix).all():
        raise ValueError("selection matrix must contain only finite values")

    names = _validate_tickers(tickers)
    n = len(names)
    if matrix.shape != (n, n):
        raise ValueError(
            f"selection matrix shape {matrix.shape} does not match {n} tickers"
        )
    if n > MAX_EXACT_ASSETS:
        raise ValueError(
            f"exact k-of-N selection requires N <= {MAX_EXACT_ASSETS}; got N={n}"
        )
    if isinstance(k, bool) or not isinstance(k, (int, np.integer)):
        raise TypeError("k must be an integer")
    k = int(k)
    if not 1 <= k <= n:
        raise ValueError(f"k must satisfy 1 <= k <= N; got k={k}, N={n}")

    best_score = np.inf
    best_indices: np.ndarray | None = None
    feasible = combinations(range(n), k)
    while True:
        batch = list(islice(feasible, _ENUMERATION_BATCH_SIZE))
        if not batch:
            break
        indices = np.asarray(batch, dtype=np.intp)
        submatrices = matrix[
            indices[:, :, np.newaxis],
            indices[:, np.newaxis, :],
        ]
        scores = submatrices.sum(axis=(1, 2))
        batch_best = int(np.argmin(scores))
        candidate_score = float(scores[batch_best])
        if candidate_score < best_score:
            best_score = candidate_score
            best_indices = indices[batch_best].copy()

    if best_indices is None:  # cardinality validation makes this unreachable
        raise RuntimeError("exact selection produced no feasible basket")

    # Only the symmetric part contributes to w'Qw. Using it here assigns half
    # of each pair term to each selected ticker and makes contributions additive.
    effective = 0.5 * (matrix + matrix.T)
    selected_matrix = effective[np.ix_(best_indices, best_indices)]
    contribution_values = selected_matrix.sum(axis=1)
    selected = [names[index] for index in best_indices]
    contributions = {
        ticker: float(value)
        for ticker, value in zip(selected, contribution_values, strict=True)
    }
    return SelectionResult(
        selected=selected,
        score=float(contribution_values.sum()),
        contributions=contributions,
    )


def select_k_of_n(
    tickers: Sequence[str] | pd.DataFrame | np.ndarray | None,
    k: int,
    *,
    returns: pd.DataFrame | np.ndarray | None = None,
    covariance: np.ndarray | None = None,
    volatilities: Sequence[float] | np.ndarray | None = None,
    relevance: RelevanceMode = "inverse_vol",
    relevance_weight: float = 1.0,
    redundancy_weight: float = 1.0,
) -> SelectionResult:
    """Build and solve an exact selection problem.

    ``tickers`` is normally a sequence of names. As a convenience, a returns
    DataFrame may be passed in that position and its columns become the ticker
    order.
    """
    if isinstance(tickers, pd.DataFrame):
        if returns is not None:
            raise ValueError("returns were supplied twice")
        returns = tickers
        names = _validate_tickers([str(column) for column in returns.columns])
    elif isinstance(tickers, np.ndarray):
        if returns is not None:
            raise ValueError("returns were supplied twice")
        returns = tickers
        raise ValueError("tickers are required for a numpy returns panel")
    elif tickers is None:
        raise ValueError("tickers are required when returns is not a DataFrame")
    else:
        names = _validate_tickers(tickers)

    if isinstance(returns, pd.DataFrame):
        columns = [str(column) for column in returns.columns]
        if set(columns) != set(names) or len(columns) != len(names):
            raise ValueError("returns columns must match tickers exactly")
        returns = returns.loc[:, names]

    q = build_selection_matrix(
        returns,
        covariance=covariance,
        volatilities=volatilities,
        relevance=relevance,
        relevance_weight=relevance_weight,
        redundancy_weight=redundancy_weight,
    )
    return solve_exact_selection(q, names, k)


# Q is the conventional name in the corresponding offline QUBO formulation.
build_selection_q = build_selection_matrix


def _covariance_and_volatilities(
    *,
    returns: pd.DataFrame | np.ndarray | None,
    covariance: np.ndarray | None,
    volatilities: Sequence[float] | np.ndarray | None,
) -> tuple[np.ndarray, np.ndarray]:
    if (returns is None) == (covariance is None):
        raise ValueError("supply exactly one of returns or covariance")
    if returns is not None:
        if volatilities is not None:
            raise ValueError("volatilities may only accompany covariance")
        panel = np.asarray(returns, dtype=float)
        if panel.ndim != 2 or panel.shape[1] == 0:
            raise ValueError("returns must be a non-empty two-dimensional panel")
        if panel.shape[0] < 2:
            raise ValueError("returns must contain at least two observations")
        if not np.isfinite(panel).all():
            raise ValueError("returns must contain only finite values")
        cov = np.atleast_2d(np.cov(panel, rowvar=False, ddof=1))
        vols = panel.std(axis=0, ddof=1)
    else:
        cov = np.asarray(covariance, dtype=float)
        if cov.ndim != 2 or cov.shape[0] == 0 or cov.shape[0] != cov.shape[1]:
            raise ValueError("covariance must be a non-empty square matrix")
        if not np.isfinite(cov).all():
            raise ValueError("covariance must contain only finite values")
        scale = max(1.0, float(np.max(np.abs(cov))))
        if not np.allclose(cov, cov.T, rtol=1e-10, atol=1e-12 * scale):
            raise ValueError("covariance must be symmetric")
        cov = 0.5 * (cov + cov.T)
        if np.any(np.diag(cov) <= 0.0):
            raise ValueError("covariance diagonal must be strictly positive")
        if volatilities is None:
            vols = np.sqrt(np.diag(cov))
        else:
            vols = np.asarray(volatilities, dtype=float)
            if vols.ndim != 1 or len(vols) != len(cov):
                raise ValueError(
                    "volatilities must be one-dimensional and match covariance"
                )

    if not np.isfinite(vols).all() or np.any(vols <= 0.0):
        raise ValueError("volatilities must be finite and strictly positive")
    return cov, vols


def _validate_tickers(tickers: Sequence[str]) -> list[str]:
    names = list(tickers)
    if not names:
        raise ValueError("selection requires at least one ticker")
    if any(not isinstance(ticker, str) or not ticker.strip() for ticker in names):
        raise ValueError("tickers must be non-empty strings")
    if len(set(names)) != len(names):
        raise ValueError("tickers must be unique")
    return names


def _nonnegative_finite(name: str, value: float) -> float:
    number = float(value)
    if not np.isfinite(number) or number < 0.0:
        raise ValueError(f"{name} must be finite and non-negative")
    return number
