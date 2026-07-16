"""Hierarchical Risk Parity (López de Prado) — arm B2, *the real bar*.

HRP beats mean-variance out of sample precisely *because* MV is estimation-error
fragile (research-plan §6). If MVSK cannot beat HRP, there is no result — so HRP
is both a benchmark and a genuine method (§7.2). Pure scipy, no extra deps.
"""

from __future__ import annotations

import time

import numpy as np
from scipy.cluster.hierarchy import linkage
from scipy.spatial.distance import squareform

from qlab.core.types import Objective, SolveResult, Weights
from qlab.solvers.base import Constraints, Solver, finalize_weights, register_solver


def _cov_to_corr(cov: np.ndarray) -> np.ndarray:
    d = np.sqrt(np.clip(np.diag(cov), 1e-18, None))
    corr = cov / np.outer(d, d)
    return np.clip(corr, -1.0, 1.0)


def _quasi_diag(link: np.ndarray) -> list[int]:
    """Return the leaf order that clusters correlated assets together."""
    link = link.astype(int)
    n = link[-1, 3]                                # total original items
    order = [link[-1, 0], link[-1, 1]]
    while max(order) >= n:                          # expand any cluster ids
        new = []
        for item in order:
            if item < n:
                new.append(item)
            else:
                row = link[item - n]
                new.extend([row[0], row[1]])
        order = new
    return order


def _inv_var_weights(cov_slice: np.ndarray) -> np.ndarray:
    ivp = 1.0 / np.clip(np.diag(cov_slice), 1e-18, None)
    return ivp / ivp.sum()


def _cluster_var(cov: np.ndarray, idx: list[int]) -> float:
    sub = cov[np.ix_(idx, idx)]
    w = _inv_var_weights(sub)
    return float(w @ sub @ w)


def _recursive_bisection(cov: np.ndarray, sort_ix: list[int]) -> np.ndarray:
    w = np.ones(len(sort_ix))
    clusters = [sort_ix]
    while clusters:
        clusters = [
            c[j:k]
            for c in clusters
            for j, k in ((0, len(c) // 2), (len(c) // 2, len(c)))
            if len(c) > 1
        ]
        for i in range(0, len(clusters), 2):
            c0, c1 = clusters[i], clusters[i + 1]
            v0, v1 = _cluster_var(cov, c0), _cluster_var(cov, c1)
            alpha = 1.0 - v0 / (v0 + v1)
            for a in c0:
                w[sort_ix.index(a)] *= alpha
            for a in c1:
                w[sort_ix.index(a)] *= (1.0 - alpha)
    # map back from sort order to original order
    out = np.zeros(len(sort_ix))
    for pos, orig in enumerate(sort_ix):
        out[orig] = w[pos]
    return out


@register_solver("hrp")
class HRPSolver(Solver):
    def solve(self, objective: Objective, constraints: Constraints, **_ctx) -> SolveResult:
        t0 = time.perf_counter()
        cov = objective.cov
        n = objective.n
        if n < 2:
            w = np.array([constraints.budget])
        else:
            corr = _cov_to_corr(cov)
            dist = np.sqrt(np.clip((1.0 - corr) / 2.0, 0.0, None))
            link = linkage(squareform(dist, checks=False), method="single")
            sort_ix = _quasi_diag(link)
            w = _recursive_bisection(cov, sort_ix)
            w = finalize_weights(w, constraints)
        constraints.validate(w)
        return SolveResult(
            weights=Weights(tickers=objective.tickers, values=[float(x) for x in w]),
            objective_value=float(w @ cov @ w),
            solver=self.name,
            status="optimal",
            wall_clock_s=time.perf_counter() - t0,
        )
