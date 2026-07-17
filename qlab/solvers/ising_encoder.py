"""MVSK -> binary pseudo-Boolean -> quadratized Ising: the Q-C artifact, built.

research-plan §0.3 asserted ~434 logical qubits from a closed-form count.  This
module *constructs* the encoding from the canonical polynomial so the count is a
measured property of an actual Hamiltonian: binarize each weight at ``r`` bits
(w_i = sum_b 2^b/(2^r - 1) * x_{i,b}), expand every polynomial term, reduce by
idempotence (x^2 = x), then Rosenberg-quadratize, counting the auxiliaries the
construction actually needs alongside the all-pairs worst case.
"""
from __future__ import annotations

from itertools import product
from math import comb

import numpy as np

from qlab.core.objective import polynomial_terms


def _bit_coeff(b: int, r: int) -> float:
    return (2 ** b) / (2 ** r - 1)


def decode(x, n: int, r: int) -> np.ndarray:
    x = np.asarray(x, dtype=float).reshape(n, r)
    scale = np.array([_bit_coeff(b, r) for b in range(r)])
    return x @ scale


def binarize(terms, n: int, r: int) -> dict[tuple[int, ...], float]:
    """Expand polynomial terms over binary bits; idempotent-reduced dict."""
    pb: dict[tuple[int, ...], float] = {}
    stats = {"raw_degree3_expansions": 0, "raw_degree4_expansions": 0}
    for c, idx in terms:
        deg = len(idx)
        for bits in product(range(r), repeat=deg):
            if deg == 3:
                stats["raw_degree3_expansions"] += 1
            elif deg == 4:
                stats["raw_degree4_expansions"] += 1
            coeff = c * np.prod([_bit_coeff(b, r) for b in bits])
            key = tuple(sorted(set(i * r + b for i, b in zip(idx, bits))))  # x^2 = x
            pb[key] = pb.get(key, 0.0) + float(coeff)
    binarize.stats = stats
    return pb


def eval_pb(pb: dict, x) -> float:
    x = np.asarray(x, dtype=float)
    return float(sum(c * np.prod(x[list(k)]) for k, c in pb.items()))


def quadratize(pb: dict) -> tuple[dict, int]:
    """Rosenberg: replace a variable pair with an auxiliary until degree <= 2."""
    pb = dict(pb)
    aux: dict[tuple[int, int], int] = {}
    next_var = 1 + max((max(k) for k in pb if k), default=-1)
    while True:
        high = [k for k in pb if len(k) > 2]
        if not high:
            break
        # most frequent pair among high-degree monomials
        pair_counts: dict[tuple[int, int], int] = {}
        for k in high:
            for i in range(len(k)):
                for j in range(i + 1, len(k)):
                    p = (k[i], k[j])
                    pair_counts[p] = pair_counts.get(p, 0) + 1
        p = max(pair_counts, key=pair_counts.get)
        if p not in aux:
            aux[p] = next_var
            next_var += 1
        a = aux[p]
        new_pb: dict[tuple[int, ...], float] = {}
        for k, c in pb.items():
            if len(k) > 2 and p[0] in k and p[1] in k:
                k = tuple(sorted((set(k) - set(p)) | {a}))
            new_pb[k] = new_pb.get(k, 0.0) + c
        pb = new_pb
    return pb, len(aux)


def resource_report(obj, resolution_bits: int) -> dict:
    n, r = obj.n, resolution_bits
    N = n * r
    terms = polynomial_terms(obj)
    pb = binarize(terms, n, r)
    _quad, n_aux = quadratize(pb)
    worst_aux = comb(N, 2) + N
    return {
        "n_assets": n, "resolution_bits": r, "weight_qubits": N,
        **binarize.stats,
        "distinct_binary_monomials": len(pb),
        "constructed_auxiliary_qubits": n_aux,
        "constructed_total_logical_qubits": N + n_aux,
        "worst_case_auxiliary_qubits": worst_aux,
        "worst_case_total_logical_qubits": N + worst_aux,
        "dirac3_continuous_variables": n,
    }
