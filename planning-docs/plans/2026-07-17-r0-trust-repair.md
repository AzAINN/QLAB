# R0 — Merge + Trust Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge the `barbara-feature` branch to repo root and repair the five defects that currently invalidate every number the system produces.

**Architecture:** No new subsystems — surgical fixes inside the existing `qlab` package: objective scaling, a canonical polynomial-terms representation with cross-compiler property tests, corrected deflated-Sharpe + bootstrap CIs, code-enforced referee/reconcile/idempotency in the trader path, a closed reflection loop, and an events read API.

**Tech Stack:** Python ≥3.10, numpy/scipy/pandas, DuckDB, pydantic, pytest. No new runtime dependencies in R0.

## Global Constraints

- Python ≥ 3.10; all work at repo root **after Task 1 promotes `QuantAgentv1/` up** (paths below are post-promotion, e.g. `qlab/core/objective.py`).
- Every test must pass offline (`offline=True` / synthetic data); no network in tests.
- Registries in tests use `Registry(":memory:")` (see `tests/conftest.py` fixtures).
- Do not rename existing public functions/tools; MCP tool names are frozen (agent `.md` files reference them).
- Run the full suite (`pytest tests/ -q`) before every commit; commit per task with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Existing suite baseline: 49 passed, 1 skipped — never go below it.

---

### Task 1: Merge `barbara-feature` and promote `QuantAgentv1/` to repo root

**Files:**
- Modify: git tree only (merge + moves); `README.md` (replaced by branch version)
- Delete after move: `QuantAgentv1/planning-docs/` (duplicate of root copies)

**Interfaces:**
- Produces: repo root contains `qlab/`, `tests/`, `agents/`, `configs/`, `mandate.yaml`, `pyproject.toml`, `.mcp.json`, `.claude/`, `.bob/`, `scripts/`, `UI_VALIDITY.md`. All later tasks assume these root paths.

- [ ] **Step 1: Commit the planning docs currently uncommitted on main**

```bash
cd /Users/azainmac/codebases/quant-trading-agent
git add planning-docs/ && git commit -m "docs: revised research plan + development roadmap (2026-07-17)"
```

- [ ] **Step 2: Merge the branch** (branch only *adds* `QuantAgentv1/`; no conflicting paths)

```bash
git merge origin/barbara-feature -m "merge: adopt QuantAgentv1 (Agent version 1)"
```

- [ ] **Step 3: Promote to root.** Root `README.md` is a 2-line stub — take the branch's. The nested `planning-docs/` is an outdated duplicate — drop it.

```bash
git rm README.md
git mv QuantAgentv1/README.md README.md
git rm -r QuantAgentv1/planning-docs
for f in QuantAgentv1/* QuantAgentv1/.claude QuantAgentv1/.bob QuantAgentv1/.mcp.json QuantAgentv1/.env.example QuantAgentv1/.gitignore; do git mv "$f" .; done
rmdir QuantAgentv1
```

(If `git mv` balks on the glob, move items explicitly: `qlab tests agents configs scripts mandate.yaml pyproject.toml UI_VALIDITY.md .claude .bob .mcp.json .env.example .gitignore`.)

- [ ] **Step 4: Install and verify the suite still passes from root**

```bash
uv venv .venv && uv pip install -e . pytest --python .venv/bin/python
.venv/bin/python -m pytest tests/ -q
```

Expected: `49 passed, 1 skipped`. (Path-relative code uses `parents[N]` from module files, which is preserved by moving the whole tree up one level.)

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "refactor: promote QuantAgentv1 to repo root"
```

---

### Task 2: R0.1 — Auto-scale the MVSK λs so higher moments actually bite

**Files:**
- Modify: `qlab/core/objective.py` (in `build_objective`)
- Test: `tests/test_objective.py`

**Interfaces:**
- Consumes: `Objective`, `MomentSet` from `qlab/core/types.py`; `compile_scipy(obj)` unchanged.
- Produces: `build_objective(form, ms, *, skew_lambda, kurt_lambda, risk_aversion, extra, lambda_scale="auto")`. With `"auto"`, the stored `obj.skew_lambda`/`obj.kurt_lambda` are the **effective** (scaled) values, and `obj.extra` gains `{"lambda_scale": "auto", "skew_lambda_raw": ..., "kurt_lambda_raw": ...}`. New helper `term_contributions(obj, w) -> dict` with keys `variance`, `skew_term`, `kurt_term`.

Semantics: `skew_lambda=0.5` now means *"the skew term is worth 0.5× the variance term at the equal-weight portfolio"* — units-invariant, so daily-return tensors no longer nullify the higher moments.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_objective.py`)

```python
import numpy as np
from qlab.core.moments import co_moments, ledoit_wolf
from qlab.core.objective import build_objective, compile_scipy, term_contributions
from qlab.core.types import MomentSet
from datetime import date


def _heavy_tailed_ms(seed=3, n=5, T=750):
    rng = np.random.default_rng(seed)
    X = rng.standard_t(df=4, size=(T, n)) * 0.01
    X[:, 0] -= (rng.random(T) < 0.03) * 0.05          # asymmetric crash asset
    cov, _ = ledoit_wolf(X)
    S, K = co_moments(X, cov, comoment_shrinkage=0.0)
    return MomentSet(tickers=[f"A{i}" for i in range(n)], as_of=date(2020, 1, 1),
                     cov=cov, coskew=S, cokurt=K), X


def test_auto_scaled_terms_are_comparable():
    ms, _ = _heavy_tailed_ms()
    obj = build_objective("mvsk", ms, skew_lambda=0.5, kurt_lambda=0.5)
    w0 = np.full(obj.n, 1.0 / obj.n)
    c = term_contributions(obj, w0)
    assert c["variance"] > 0
    # each active term within one order of magnitude of 0.5x variance
    assert 0.05 * c["variance"] < abs(c["skew_term"]) < 5.0 * c["variance"]
    assert 0.05 * c["variance"] < abs(c["kurt_term"]) < 5.0 * c["variance"]
    assert obj.extra["lambda_scale"] == "auto"
    assert obj.extra["skew_lambda_raw"] == 0.5


def test_raw_scale_preserves_old_behavior():
    ms, _ = _heavy_tailed_ms()
    obj = build_objective("mvsk", ms, skew_lambda=0.5, kurt_lambda=0.5,
                          lambda_scale="raw")
    assert obj.skew_lambda == 0.5 and obj.kurt_lambda == 0.5


def test_mvsk_diverges_from_min_variance_when_scaled():
    ms, _ = _heavy_tailed_ms()
    from qlab.solvers.base import Constraints, get_solver
    c = Constraints(max_weight=0.6)
    w_mv = get_solver("classical").solve(build_objective("min_variance", ms), c).weights.as_array()
    w_mvsk = get_solver("classical_multistart").solve(
        build_objective("mvsk", ms, skew_lambda=1.0, kurt_lambda=1.0), c).weights.as_array()
    assert np.abs(w_mv - w_mvsk).sum() > 0.02
```

- [ ] **Step 2: Run to verify failure** — `.venv/bin/python -m pytest tests/test_objective.py -q` → FAIL (`term_contributions` not defined; raw λs identical to auto).

- [ ] **Step 3: Implement in `qlab/core/objective.py`** — replace `build_objective` and add the helper:

```python
def build_objective(form, ms, *, skew_lambda=0.0, kurt_lambda=0.0,
                    risk_aversion=1.0, extra=None, lambda_scale="auto"):
    extra = dict(extra or {})
    l3, l4 = float(skew_lambda), float(kurt_lambda)
    if form == "mvsk" and lambda_scale == "auto" and (l3 or l4):
        n = ms.n
        w0 = np.full(n, 1.0 / n)
        var0 = float(w0 @ ms.cov @ w0)
        extra.update({"lambda_scale": "auto",
                      "skew_lambda_raw": l3, "kurt_lambda_raw": l4})
        if ms.coskew is not None and l3:
            m3 = abs(float(np.einsum("ijk,i,j,k->", ms.coskew, w0, w0, w0)))
            l3 = l3 * var0 / max(m3, 1e-30)
        if ms.cokurt is not None and l4:
            m4 = abs(float(np.einsum("ijkl,i,j,k,l->", ms.cokurt, w0, w0, w0, w0)))
            l4 = l4 * var0 / max(m4, 1e-30)
    elif form == "mvsk":
        extra.setdefault("lambda_scale", lambda_scale)
    return Objective(form=form, tickers=ms.tickers, cov=ms.cov, mu=ms.mu,
                     coskew=ms.coskew, cokurt=ms.cokurt,
                     skew_lambda=l3, kurt_lambda=l4,
                     risk_aversion=risk_aversion, extra=extra)


def term_contributions(obj: Objective, w: np.ndarray) -> dict[str, float]:
    """Per-term magnitudes at ``w`` — the R0.1 diagnostic (logged with solves)."""
    w = np.asarray(w, dtype=float)
    out = {"variance": float(w @ obj.cov @ w), "skew_term": 0.0, "kurt_term": 0.0}
    if obj.coskew is not None and obj.skew_lambda:
        out["skew_term"] = -obj.skew_lambda * float(
            np.einsum("ijk,i,j,k->", obj.coskew, w, w, w))
    if obj.cokurt is not None and obj.kurt_lambda:
        out["kurt_term"] = obj.kurt_lambda * float(
            np.einsum("ijkl,i,j,k,l->", obj.cokurt, w, w, w, w))
    return out
```

- [ ] **Step 4: Run tests** — target tests PASS, then full suite (`pytest tests/ -q`; the existing `test_dirac_hubo_payload_structure` and solver tests must stay green — `compile_scipy` semantics are unchanged, only λ magnitudes moved).

- [ ] **Step 5: Commit** — `git add qlab/core/objective.py tests/test_objective.py && git commit -m "fix(objective): auto-scale MVSK lambdas so higher moments contribute (R0.1)"`

---

### Task 3: R0.2a — Canonical polynomial terms + cross-compiler property tests

**Files:**
- Modify: `qlab/core/objective.py`, `qlab/solvers/dirac3.py`
- Test: `tests/test_objective.py`

**Interfaces:**
- Produces: `polynomial_terms(obj) -> list[tuple[float, tuple[int, ...]]]` — the **single source of truth**: unique sorted index tuples with permutation multiplicity folded into the coefficient, covering degree-2 (Σ), degree-3 (−λ₃S), degree-4 (+λ₄K). `evaluate_terms(terms, w) -> float`. `dirac3._mvsk_polynomial(obj)` refactored to consume `polynomial_terms` (returns the same `(coeffs, indices)` payload shape it does today, 1-based indices padded to the max degree with 0s — the eqc-models convention).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_objective.py`; reuse `_heavy_tailed_ms` from Task 2)

```python
def test_polynomial_terms_match_scipy_compiler():
    from qlab.core.objective import evaluate_terms, polynomial_terms
    ms, _ = _heavy_tailed_ms(seed=11, n=4, T=600)
    obj = build_objective("mvsk", ms, skew_lambda=0.7, kurt_lambda=0.3)
    f, _g = compile_scipy(obj)
    terms = polynomial_terms(obj)
    rng = np.random.default_rng(0)
    for _ in range(20):
        w = rng.dirichlet(np.ones(obj.n))
        assert abs(evaluate_terms(terms, w) - f(w)) < 1e-10


def test_dirac_encoder_agrees_with_scipy():
    from qlab.core.objective import polynomial_terms
    from qlab.solvers.dirac3 import _mvsk_polynomial
    ms, _ = _heavy_tailed_ms(seed=11, n=4, T=600)
    obj = build_objective("mvsk", ms, skew_lambda=0.7, kurt_lambda=0.3)
    f, _g = compile_scipy(obj)
    coeffs, indices = _mvsk_polynomial(obj)
    max_deg = max(len([i for i in idx if i > 0]) for idx in indices)
    rng = np.random.default_rng(1)
    for _ in range(10):
        w = rng.dirichlet(np.ones(obj.n))
        val = sum(c * np.prod([w[i - 1] for i in idx if i > 0])
                  for c, idx in zip(coeffs, indices))
        assert abs(val - f(w)) < 1e-10
    assert max_deg == 4
```

- [ ] **Step 2: Run to verify failure** — `polynomial_terms` undefined.

- [ ] **Step 3: Implement in `qlab/core/objective.py`**

```python
from itertools import combinations_with_replacement
from math import factorial
from collections import Counter


def _perm_multiplicity(idx: tuple[int, ...]) -> int:
    c = Counter(idx)
    m = factorial(len(idx))
    for v in c.values():
        m //= factorial(v)
    return m


def polynomial_terms(obj: Objective) -> list[tuple[float, tuple[int, ...]]]:
    """The one polynomial, as unique sorted-index terms (invariant 4).

    ``sum(c * prod(w[i] for i in idx))`` over the returned terms equals
    ``compile_scipy(obj)[0](w)`` exactly — property-tested, and consumed by the
    Dirac-3 and Ising encoders so all arms share one coefficient source.
    """
    if obj.form not in ("min_variance", "mvsk"):
        raise ValueError(f"no polynomial form for {obj.form!r}")
    terms: list[tuple[float, tuple[int, ...]]] = []
    n = obj.n
    for idx in combinations_with_replacement(range(n), 2):
        c = float(obj.cov[idx]) * _perm_multiplicity(idx)
        if c:
            terms.append((c, idx))
    if obj.form == "mvsk" and obj.coskew is not None and obj.skew_lambda:
        for idx in combinations_with_replacement(range(n), 3):
            c = -obj.skew_lambda * float(obj.coskew[idx]) * _perm_multiplicity(idx)
            if c:
                terms.append((c, idx))
    if obj.form == "mvsk" and obj.cokurt is not None and obj.kurt_lambda:
        for idx in combinations_with_replacement(range(n), 4):
            c = obj.kurt_lambda * float(obj.cokurt[idx]) * _perm_multiplicity(idx)
            if c:
                terms.append((c, idx))
    return terms


def evaluate_terms(terms, w) -> float:
    w = np.asarray(w, dtype=float)
    return float(sum(c * np.prod(w[list(idx)]) for c, idx in terms))
```

(Symmetry note: `co_moments` builds tensors via symmetric einsums of centered data, so `S`/`K` are fully symmetric and the multiplicity fold is exact.)

- [ ] **Step 4: Refactor `qlab/solvers/dirac3.py`** — replace the entire body of `_mvsk_polynomial` with a thin adapter (keep its existing return signature used by `_submit`):

```python
def _mvsk_polynomial(obj):
    """Flatten the canonical polynomial into eqc-models (coeffs, indices) form.

    Single source of truth: qlab.core.objective.polynomial_terms. Indices are
    1-based and left-padded with 0 to the max degree, per PolynomialModel.
    """
    from qlab.core.objective import polynomial_terms
    terms = polynomial_terms(obj)
    max_deg = max(len(idx) for _, idx in terms)
    coeffs, indices = [], []
    for c, idx in terms:
        coeffs.append(float(c))
        padded = (0,) * (max_deg - len(idx)) + tuple(i + 1 for i in idx)
        indices.append(list(padded))
    return coeffs, indices
```

- [ ] **Step 5: Run tests** — both new tests PASS; full suite green (existing `test_dirac_unavailable_carries_payload` must still pass — the payload path is unchanged).

- [ ] **Step 6: Commit** — `git commit -am "feat(objective): canonical polynomial_terms + scipy/dirac agreement tests (R0.2a)"`

---

### Task 4: R0.2b — True MVSK→binary→Ising encoder (construct, then count)

**Files:**
- Create: `qlab/solvers/ising_encoder.py`
- Test: `tests/test_objective.py`

**Interfaces:**
- Consumes: `polynomial_terms(obj)`.
- Produces: `binarize(terms, n, r) -> dict[tuple[int, ...], float]` (pseudo-Boolean over `N=n*r` binary vars, idempotent-reduced, sorted-tuple keys; also returns raw expansion counters via `binarize.stats` — see impl), `quadratize(pb) -> tuple[dict, int]` (quadratic dict + number of Rosenberg auxiliaries actually introduced), `resource_report(obj, r) -> dict` (constructed counts + the closed-form worst case side by side), `decode(x, n, r) -> np.ndarray` (binary assignment → weight vector on the discrete grid).

- [ ] **Step 1: Write the failing tests**

```python
def test_ising_encoder_constructs_and_counts():
    from math import comb
    from qlab.solvers.ising_encoder import binarize, decode, resource_report
    from qlab.core.objective import polynomial_terms
    ms, _ = _heavy_tailed_ms(seed=5, n=7, T=900)
    obj = build_objective("mvsk", ms, skew_lambda=0.5, kurt_lambda=0.5)
    rep = resource_report(obj, resolution_bits=4)
    assert rep["weight_qubits"] == 28
    assert rep["raw_degree4_expansions"] == comb(7 + 3, 4) * 4 ** 4   # 53,760
    assert rep["worst_case_total_logical_qubits"] == 434              # §0.3 headline
    assert 0 < rep["constructed_auxiliary_qubits"] <= rep["worst_case_auxiliary_qubits"]
    assert rep["constructed_total_logical_qubits"] == 28 + rep["constructed_auxiliary_qubits"]


def test_pseudo_boolean_matches_polynomial_on_grid():
    from qlab.solvers.ising_encoder import binarize, decode, eval_pb
    from qlab.core.objective import evaluate_terms, polynomial_terms
    ms, _ = _heavy_tailed_ms(seed=5, n=3, T=600)
    obj = build_objective("mvsk", ms, skew_lambda=0.5, kurt_lambda=0.5)
    terms = polynomial_terms(obj)
    r = 3
    pb = binarize(terms, obj.n, r)
    rng = np.random.default_rng(2)
    for _ in range(10):
        x = rng.integers(0, 2, size=obj.n * r)
        w = decode(x, obj.n, r)
        assert abs(eval_pb(pb, x) - evaluate_terms(terms, w)) < 1e-9
```

- [ ] **Step 2: Run to verify failure** — module missing.

- [ ] **Step 3: Implement `qlab/solvers/ising_encoder.py`**

```python
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
```

- [ ] **Step 4: Run tests.** The n=7 construction test iterates ~60k expansions — assert runtime stays under ~30s; if slower, keep n=7 but drop the quadratize call into a cached single run (both tests share one `resource_report` result via a module-level fixture).

- [ ] **Step 5: Wire the report into the existing MCP surface** — in `qlab/mcp/quant_lab.py`, extend `solve.qubo_resource_count` to include the constructed numbers when tensors are available: change the tool body to

```python
        out = mvsk_qubo_resource_count(n, resolution_bits)
        out["note"] = ("worst-case closed form; run objective.build + "
                       "solve.constructed_resource_count for measured counts")
        return out
```

and add a sibling tool:

```python
    @app.tool(name="solve.constructed_resource_count")
    def solve_constructed_resource_count(objective_id: str, resolution_bits: int = 4) -> dict:
        """Measured MVSK->QUBO->Ising resources from an actual construction."""
        st.budget.charge("solve.constructed_resource_count")
        from qlab.solvers.ising_encoder import resource_report
        return resource_report(st.get_objective(objective_id), resolution_bits)
```

- [ ] **Step 6: Full suite; commit** — `git add qlab/solvers/ising_encoder.py qlab/mcp/quant_lab.py tests/test_objective.py && git commit -m "feat(quantum): constructed MVSK->Ising encoder + measured resource report (R0.2b)"`

---

### Task 5: R0.3 — Fix deflated Sharpe, wire trial counting, report bootstrap CIs

**Files:**
- Modify: `qlab/core/metrics.py`, `qlab/experiment.py`, `qlab/state/registry.py`
- Test: `tests/test_backtest.py`, `tests/test_registry.py`

**Interfaces:**
- Produces: `deflated_sharpe(returns, sharpe_periodic, n_trials=1, trial_sharpe_var=None)` (Bailey–López de Prado null with the √V̂ term); `compute_metrics(..., trial_sharpe_var=None)` passthrough; `Registry.backtest_trial_count() -> int` (`COUNT(DISTINCT arm_id) FROM backtests`); `run_ablation` post-pass that (1) computes cross-arm periodic-Sharpe variance, (2) recomputes each arm's DSR with `n_trials=registry.backtest_trial_count()`, (3) adds `sharpe_ci` and `sortino_ci` (95% stationary block bootstrap) into each arm's metrics **before** `log_backtest`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_backtest.py`)

```python
import numpy as np
import pandas as pd
from qlab.core.metrics import block_bootstrap_ci, deflated_sharpe


def _noise(seed, n=1000, mu=0.0):
    rng = np.random.default_rng(seed)
    return pd.Series(rng.normal(mu, 0.01, n))


def test_deflated_sharpe_is_calibrated_not_degenerate():
    rs = [_noise(s) for s in range(8)]
    srs = [float(r.mean() / r.std(ddof=1)) for r in rs]
    v = float(np.var(srs, ddof=1))
    dsrs = [deflated_sharpe(r, sr, n_trials=8, trial_sharpe_var=v)
            for r, sr in zip(rs, srs)]
    assert all(0.0 < d < 1.0 for d in dsrs)
    assert max(dsrs) > 0.01                      # the old bug pinned all of these to ~0
    skilled = _noise(99, mu=0.001)
    sr_sk = float(skilled.mean() / skilled.std(ddof=1))
    assert deflated_sharpe(skilled, sr_sk, n_trials=8, trial_sharpe_var=v) > max(dsrs)


def test_deflated_sharpe_monotone_in_trials():
    r = _noise(1, mu=0.0005)
    sr = float(r.mean() / r.std(ddof=1))
    assert deflated_sharpe(r, sr, n_trials=20, trial_sharpe_var=0.001) < \
           deflated_sharpe(r, sr, n_trials=2, trial_sharpe_var=0.001)


def test_ablation_reports_cis_and_registry_trials(tmp_path):
    from qlab.experiment import run_ablation
    from qlab.state.registry import Registry
    reg = Registry(":memory:")
    spec = {"name": "t", "seed": 7,
            "data": {"universe": "core", "start": "2016-01-01", "end": "2020-12-31"},
            "backtest": {"rebalance": "quarterly", "lookback_days": 504, "cost_bps": 5},
            "moments": {}, "arms": [
                {"id": "B1", "objective": "equal_weight", "solver": "none"},
                {"id": "A1", "objective": "min_variance", "solver": "classical"}]}
    out = run_ablation(spec, registry=reg, offline=True, run_qaoa=False)
    m = out["arms"]["A1"]["metrics"]
    assert "sharpe_ci" in m and m["sharpe_ci"][0] <= m["sharpe_ci"][1]
    assert "sortino_ci" in m
    assert reg.backtest_trial_count() == 2
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Fix `deflated_sharpe` in `qlab/core/metrics.py`** — replace the `e_max`/`sr0` block:

```python
def deflated_sharpe(returns, sharpe_periodic, n_trials=1, trial_sharpe_var=None):
    n = len(returns)
    if n < 4 or sharpe_periodic == 0:
        return 0.0
    g3 = float(stats.skew(returns, bias=False))
    g4 = float(stats.kurtosis(returns, fisher=False, bias=False))
    if n_trials > 1:
        # Bailey & Lopez de Prado (2014): SR0 = sqrt(V[SR_trials]) * E[max Z_N]
        if trial_sharpe_var is None or trial_sharpe_var <= 0:
            trial_sharpe_var = (1 + 0.5 * sharpe_periodic ** 2) / max(n - 1, 1)
        e_max = (1 - np.euler_gamma) * stats.norm.ppf(1 - 1.0 / n_trials) + \
            np.euler_gamma * stats.norm.ppf(1 - 1.0 / (n_trials * np.e))
        sr0 = float(np.sqrt(trial_sharpe_var) * e_max)
    else:
        sr0 = 0.0
    denom = np.sqrt(
        max(1e-12, 1 - g3 * sharpe_periodic + (g4 - 1) / 4.0 * sharpe_periodic ** 2))
    dsr_stat = (sharpe_periodic - sr0) * np.sqrt(n - 1) / denom
    return float(stats.norm.cdf(dsr_stat))
```

Thread `trial_sharpe_var` through `compute_metrics` (new keyword, default `None`, passed to `deflated_sharpe`).

- [ ] **Step 4: Add `Registry.backtest_trial_count`** (next to `trial_count` in `qlab/state/registry.py`)

```python
    def backtest_trial_count(self) -> int:
        r = self.con.execute("SELECT COUNT(DISTINCT arm_id) FROM backtests").fetchone()
        return int(r[0]) if r else 0
```

- [ ] **Step 5: Rework the arms loop in `run_ablation` (`qlab/experiment.py`)** — collect results first, enrich, then log:

```python
    from qlab.core.metrics import block_bootstrap_ci, deflated_sharpe

    bt_results: dict[str, "BacktestResult"] = {}
    for arm in arms:
        try:
            res = run_backtest(prices, build_policy(arm, moments=moments_cfg),
                               arm_id=arm.id, cadence=bt.get("rebalance", "quarterly"),
                               lookback_days=moments_cfg.lookback_days,
                               cost_bps=float(bt.get("cost_bps", 5)), n_trials=n_trials)
            bt_results[arm.id] = res
        except Exception as exc:
            results["arms"][arm.id] = {"error": repr(exc)}

    # cross-trial Sharpe variance + registry-counted trials -> honest DSR + CIs
    def _psr(r):
        sd = r.std(ddof=1)
        return float(r.mean() / sd) if sd > 0 else 0.0
    psrs = [_psr(r.returns) for r in bt_results.values()]
    v_sr = float(np.var(psrs, ddof=1)) if len(psrs) > 1 else 0.0
    for arm_id, res in bt_results.items():
        res.metrics["deflated_sharpe"] = deflated_sharpe(
            res.returns, _psr(res.returns),
            n_trials=max(len(bt_results), 2), trial_sharpe_var=v_sr)
        for name, fn in (("sharpe_ci", _psr), ("sortino_ci", _sortino_stat)):
            res.metrics[name] = list(block_bootstrap_ci(res.returns, fn))
        reg.log_backtest(run_id, arm_id, res.metrics)
        results["arms"][arm_id] = {
            "objective": next(a.objective for a in arms if a.id == arm_id),
            "solver": next(a.solver for a in arms if a.id == arm_id),
            "metrics": res.metrics, "total_turnover": res.total_turnover}
    results["n_trials_registry"] = reg.backtest_trial_count()
```

with the module-level helper:

```python
def _sortino_stat(r) -> float:
    downside = r[r < 0]
    dv = downside.std(ddof=1) if len(downside) > 1 else 0.0
    return float(r.mean() / dv) if dv > 0 else 0.0
```

- [ ] **Step 6: Run tests; full suite** (the existing `test_ablation_runs_and_ranks` must still pass — ranking keys unchanged).

- [ ] **Step 7: Commit** — `git commit -am "fix(metrics): calibrated deflated Sharpe + bootstrap CIs wired into ablation (R0.3)"`

---

### Task 6: R0.4a — Referee verdict gate + reconcile, enforced in code

**Files:**
- Create: `qlab/governance/__init__.py`, `qlab/governance/referee.py`, `qlab/trader/reconcile.py`
- Modify: `qlab/state/registry.py` (verdicts table + methods), `qlab/trader/plan.py` (gate), `qlab/mcp/quant_lab.py` (verdict tool), `qlab/mcp/quant_trader.py` (delegate reconcile), `qlab/autopilot/loop.py` (referee + reconcile before execute), `agents/referee.md` (add the new tool to its allowlist, then re-run the loader sync)
- Test: `tests/test_trader.py`, `tests/test_autopilot.py`

**Interfaces:**
- Produces: `Registry.log_verdict(decision_id, verdict, reasons, source) -> str`, `Registry.get_verdict(decision_id) -> dict | None` (latest); `deterministic_referee(targets, mandate, as_of, moments_summary=None) -> tuple[str, list[str]]` returning `("PASS"|"FAIL", reasons)`; `reconcile(registry, broker, tickers) -> dict` (moved verbatim from the MCP tool); `execute_plan` now **raises `MandateViolation` unless the plan's `decision_id` has a PASS verdict**; new lab MCP tool `registry.log_verdict`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_trader.py`)

```python
def test_execute_requires_referee_pass(reg_and_broker):     # use existing fixtures' style
    import pytest
    from qlab.trader.mandate import MandateViolation, load_mandate
    from qlab.trader.plan import build_plan, execute_plan
    reg, broker = reg_and_broker
    mandate = load_mandate()
    targets = {t: 1.0 / len(mandate.universe_whitelist) for t in mandate.universe_whitelist}
    plan = build_plan(reg, broker, mandate, targets, "dec1")
    with pytest.raises(MandateViolation, match="referee"):
        execute_plan(reg, broker, plan)
    reg.log_verdict("dec1", "FAIL", ["planted flaw"], "deterministic")
    with pytest.raises(MandateViolation, match="referee"):
        execute_plan(reg, broker, plan)
    reg.log_verdict("dec1", "PASS", [], "deterministic")
    out = execute_plan(reg, broker, plan)
    assert out["state"] == "reconciled"


def test_deterministic_referee_catches_planted_flaw():
    from datetime import date
    from qlab.governance.referee import deterministic_referee
    from qlab.trader.mandate import load_mandate
    m = load_mandate()
    bad = {m.universe_whitelist[0]: 0.95, m.universe_whitelist[1]: 0.05}  # cap breach
    verdict, reasons = deterministic_referee(bad, m, date(2020, 1, 1))
    assert verdict == "FAIL" and any("cap" in r or "weight" in r for r in reasons)
    good = {t: 1.0 / len(m.universe_whitelist) for t in m.universe_whitelist}
    assert deterministic_referee(good, m, date(2020, 1, 1))[0] == "PASS"
```

and (append to `tests/test_autopilot.py`):

```python
def test_run_once_logs_verdict_and_reconciles(tmp_registry):
    from qlab.autopilot.loop import run_once
    summary = run_once(registry=tmp_registry, offline=True, execute=True, as_of="2021-06-30")
    assert summary["referee"]["verdict"] == "PASS"
    assert summary["reconcile"]["clean"] is True


def test_run_once_refuses_dirty_ledger(tmp_registry, monkeypatch):
    # SimulatedPaperBroker reads positions from the same registry as the ledger,
    # so a genuine mismatch is unreachable in-process - stub the reconcile result.
    import qlab.autopilot.loop as loop
    monkeypatch.setattr(loop, "reconcile",
                        lambda *a, **k: {"clean": False, "diffs": {"ACWI": {}}})
    summary = loop.run_once(registry=tmp_registry, offline=True, execute=True,
                            as_of="2021-06-30")
    assert summary["trade"]["executed"] is False
    assert summary["trade"]["blocked_by"] == "reconcile"


def test_reconcile_detects_stub_broker_mismatch():
    from qlab.state.registry import Registry
    from qlab.trader.reconcile import reconcile

    class StubBroker:
        def portfolio_state(self, tickers):
            return {"positions": {"ACWI": {"qty": 3.0}}}

    reg = Registry(":memory:")
    out = reconcile(reg, StubBroker(), ["ACWI"])
    assert out["clean"] is False and "ACWI" in out["diffs"]
```

(Adapt fixture names to `tests/conftest.py`'s actual fixtures; if absent, add to `conftest.py`: `tmp_registry` returning `Registry(":memory:")`, and `reg_and_broker` returning `(reg, SimulatedPaperBroker(reg, default_price_provider(offline=True), load_mandate().paper_capital, universe=load_mandate().universe_whitelist))`.)

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Registry additions** (`qlab/state/registry.py`) — append to `_SCHEMA`:

```sql
CREATE TABLE IF NOT EXISTS verdicts (
    verdict_id VARCHAR PRIMARY KEY, decision_id VARCHAR, verdict VARCHAR,
    reasons JSON, source VARCHAR, created_at VARCHAR);
```

and methods:

```python
    def log_verdict(self, decision_id: str, verdict: str, reasons: list[str],
                    source: str = "deterministic") -> str:
        vid = uuid.uuid4().hex[:16]
        self.con.execute("INSERT INTO verdicts VALUES (?,?,?,?,?,?)",
                         [vid, decision_id, verdict, _j(reasons), source, _now()])
        self.record_event("referee_verdict",
                          {"decision_id": decision_id, "verdict": verdict})
        return vid

    def get_verdict(self, decision_id: str) -> dict | None:
        r = self._rows("SELECT * FROM verdicts WHERE decision_id=? "
                       "ORDER BY created_at DESC LIMIT 1", [decision_id])
        return r[0] if r else None
```

(add `"reasons"` to the JSON-decode key list in `_rows`.)

- [ ] **Step 4: `qlab/governance/referee.py`**

```python
"""The referee gate, as code. Nothing trades without a PASS (research-plan §3).

The deterministic referee re-verifies the mandate-critical facts *independently*
of build_plan (defense in depth) and is the autopilot's gatekeeper. The LLM
referee agent adds qualitative review in interactive sessions and submits its
verdict through the same registry.log_verdict tool - one gate, two reviewers.
"""
from __future__ import annotations

from datetime import date

import numpy as np

from qlab.trader.mandate import Mandate


def deterministic_referee(targets: dict[str, float], mandate: Mandate,
                          as_of: date, moments_summary: dict | None = None,
                          ) -> tuple[str, list[str]]:
    reasons: list[str] = []
    vals = np.array(list(targets.values()), dtype=float)
    if not np.all(np.isfinite(vals)):
        reasons.append("non-finite weight")
    for t in targets:
        if t not in mandate.universe_whitelist:
            reasons.append(f"{t} outside universe whitelist")
    if mandate.long_only and np.any(vals < -1e-4):
        reasons.append("long-only violated")
    if mandate.fully_invested and abs(vals.sum() - 1.0) > 1e-2:
        reasons.append(f"budget violated: sum={vals.sum():.4f}")
    over = {t: v for t, v in targets.items() if v > mandate.max_weight_per_asset + 1e-4}
    if over:
        reasons.append(f"per-asset cap breach: {over}")
    if isinstance(as_of, date) and as_of > date.today():
        reasons.append("look-ahead as_of")
    if moments_summary and moments_summary.get("condition_number", 0) > 1e8:
        reasons.append("ill-conditioned covariance")
    return ("PASS" if not reasons else "FAIL"), reasons
```

- [ ] **Step 5: Move reconcile into `qlab/trader/reconcile.py`** — lift the body of the `reconcile` MCP tool verbatim into

```python
def reconcile(registry, broker, tickers: list[str]) -> dict:
    broker_pos = broker.portfolio_state(tickers)["positions"]
    ledger_pos = registry.get_positions()
    diffs = {}
    for t in set(list(broker_pos) + list(ledger_pos)):
        bq = broker_pos.get(t, {}).get("qty", 0.0)
        lq = ledger_pos.get(t, {}).get("qty", 0.0)
        if abs(bq - lq) > 1e-6:
            diffs[t] = {"broker_qty": bq, "ledger_qty": lq}
    return {"clean": not diffs, "diffs": diffs}
```

and make the `quant_trader.py` tool a one-line delegate.

- [ ] **Step 6: Gate `execute_plan` (`qlab/trader/plan.py`)** — after the halted check:

```python
    v = registry.get_verdict(plan.decision_id)
    if not v or v.get("verdict") != "PASS":
        raise MandateViolation(
            f"no referee PASS for decision {plan.decision_id!r}; "
            "log_verdict must record PASS before execution")
```

- [ ] **Step 7: Wire the autopilot (`qlab/autopilot/loop.py` `run_once`)** — after `decision_id = reg.log_decision(decision)`:

```python
    verdict, reasons = deterministic_referee(targets, mandate, _as_date(as_of),
                                             moments_summary=diag.get("moments"))
    reg.log_verdict(decision_id, verdict, reasons, source="deterministic")
    rec = reconcile(reg, broker, tickers)
```

and change the propose/execute block to refuse on either gate:

```python
    trade_result: dict = {"executed": False}
    if verdict != "PASS":
        trade_result = {"executed": False, "blocked_by": "referee", "reasons": reasons}
    elif not rec["clean"]:
        trade_result = {"executed": False, "blocked_by": "reconcile", "diffs": rec["diffs"]}
        reg.record_event("reconcile_dirty", rec)
    else:
        try:
            plan = build_plan(reg, broker, mandate, targets, decision_id, cost_bps=5.0)
            ...  # existing execute/dry-run logic unchanged
```

Add `"referee": {"verdict": verdict, "reasons": reasons}` and `"reconcile": rec` to the returned `summary` dict. Import the two new modules at top.

- [ ] **Step 8: Expose the verdict tool for the LLM referee** — in `qlab/mcp/quant_lab.py`:

```python
    @app.tool(name="registry.log_verdict")
    def registry_log_verdict(decision_id: str, verdict: str, reasons: list = []) -> dict:
        """Referee-only: record PASS/FAIL for a decision. Trading requires PASS."""
        st.budget.charge("registry.log_verdict")
        if verdict not in ("PASS", "FAIL"):
            raise ValueError("verdict must be PASS or FAIL")
        vid = st.registry.log_verdict(decision_id, verdict, list(reasons), source="referee-agent")
        return {"verdict_id": vid, "decision_id": decision_id, "verdict": verdict}
```

Add `registry.log_verdict` to the `tools:` list in `agents/referee.md`, run the loader sync (`python -c "from qlab.agents.loader import sync; sync()"` — check the actual sync entrypoint name in `qlab/agents/loader.py` and use it), and confirm `tests/test_agents.py` still passes (it asserts adapter regeneration).

- [ ] **Step 9: Run the new tests, then the full suite.** `test_daily_ops_never_trades` must still pass (daily_ops untouched).

- [ ] **Step 10: Commit** — `git commit -am "feat(governance): code-enforced referee gate + reconcile before trading (R0.4a)"`

---

### Task 7: R0.4b — Real per-leg idempotency; orders ledger single-writer

**Files:**
- Modify: `qlab/state/registry.py` (`get_order`, `update_order_state`), `qlab/trader/plan.py` (`execute_plan` leg loop), `qlab/trader/broker.py` (remove `add_order` from `SimulatedPaperBroker.submit_notional`)
- Test: `tests/test_trader.py`

Today `SimulatedPaperBroker.submit_notional` writes an orders row with `plan_id=""` *and* applies the fill unconditionally, while `execute_plan` writes a second row that hits the PK conflict and is silently dropped — so re-execution double-applies fills and the ledger loses `plan_id`.

**Interfaces:**
- Produces: `Registry.get_order(client_order_id) -> dict | None`, `Registry.update_order_state(client_order_id, state)`. `execute_plan` is the **only** writer of the orders ledger; brokers only move money. Replaying a plan (or a crashed session resuming) skips already-filled legs.

- [ ] **Step 1: Write the failing test** (append to `tests/test_trader.py`)

```python
def test_replayed_plan_does_not_double_fill(reg_and_broker):
    from qlab.trader.mandate import load_mandate
    from qlab.trader.plan import build_plan, execute_plan
    reg, broker = reg_and_broker
    mandate = load_mandate()
    targets = {t: 1.0 / len(mandate.universe_whitelist) for t in mandate.universe_whitelist}
    plan = build_plan(reg, broker, mandate, targets, "dec-replay")
    reg.log_verdict("dec-replay", "PASS", [], "deterministic")
    execute_plan(reg, broker, plan)
    pos1 = {t: p["qty"] for t, p in reg.get_positions().items()}
    cash1 = reg.get_account()["cash"]
    plan.state = "checked"                      # simulate a resumed session
    reg.set_plan_state(plan.plan_id, "checked")
    out2 = execute_plan(reg, broker, plan)
    pos2 = {t: p["qty"] for t, p in reg.get_positions().items()}
    assert pos1 == pos2 and abs(reg.get_account()["cash"] - cash1) < 1e-9
    assert all(f.get("replayed") for f in out2["fills"])
    orders = reg._rows("SELECT * FROM orders", [])
    assert all(o["plan_id"] == plan.plan_id for o in orders)
```

- [ ] **Step 2: Run to verify failure** (double-applied fills change positions).

- [ ] **Step 3: Registry methods**

```python
    def get_order(self, client_order_id: str) -> dict | None:
        r = self._rows("SELECT * FROM orders WHERE client_order_id=?", [client_order_id])
        return r[0] if r else None

    def update_order_state(self, client_order_id: str, state: str) -> None:
        self.con.execute("UPDATE orders SET state=? WHERE client_order_id=?",
                         [state, client_order_id])
```

- [ ] **Step 4: Rework the leg loop in `execute_plan`**

```python
    registry.set_plan_state(plan.plan_id, "submitted")
    fills = []
    for leg in plan.legs:
        existing = registry.get_order(leg.client_order_id)
        if existing and existing["state"] == "filled":
            fills.append({"client_order_id": leg.client_order_id,
                          "ticker": leg.ticker, "replayed": True})
            continue
        registry.add_order(leg.client_order_id, plan.plan_id, leg.ticker, leg.side,
                           leg.notional, state="submitted")
        fill = broker.submit_notional(leg.client_order_id, leg.ticker, leg.side,
                                      leg.notional)
        registry.update_order_state(leg.client_order_id, fill.get("state", "filled"))
        fills.append(fill)
```

- [ ] **Step 5: Delete the ledger write from the simulated broker** — in `SimulatedPaperBroker.submit_notional`, remove the `self.reg.add_order(...)` call (keep `apply_fill`).

- [ ] **Step 6: Run tests; full suite** (the old `test_execution_is_idempotent` — which only checked ID determinism — must still pass; the new test now checks the actual money path).

- [ ] **Step 7: Commit** — `git commit -am "fix(trader): leg-level idempotent execution; plan owns the orders ledger (R0.4b)"`

---

### Task 8: R0.4c — Challenger view recorded in the autopilot path

**Files:**
- Modify: `qlab/autopilot/loop.py`
- Test: `tests/test_autopilot.py`

**Interfaces:**
- Consumes: `solve_arm`, `MomentsConfig` (already imported in `loop.py`).
- Produces: `run_once`'s `Decision` always carries a non-empty `challenger_view` built from an alternate-window solve (the relocated adversarial debate: the *estimation* choice is challenged, per research-plan §3).

- [ ] **Step 1: Write the failing test**

```python
def test_run_once_records_challenger_view(tmp_registry):
    from qlab.autopilot.loop import run_once
    run_once(registry=tmp_registry, offline=True, execute=False, as_of="2021-06-30")
    dec = tmp_registry.recent_decisions(limit=1)[0]
    assert dec["challenger_view"] and "window" in dec["challenger_view"]
```

- [ ] **Step 2: Run to verify failure** (`challenger_view` is currently None).

- [ ] **Step 3: Implement** — in `run_once`, after the champion solve and before constructing `Decision`:

```python
    alt_lookback = 252 if lookback_days != 252 else 504
    alt_w, _alt_diag = solve_arm(champion, snap,
                                 moments=MomentsConfig(lookback_days=alt_lookback),
                                 constraints=constraints)
    l1 = float(np.abs(alt_w.as_array() - weights.as_array()).sum())
    challenger_view = (
        f"Challenger (window={alt_lookback}d vs {lookback_days}d): weight "
        f"divergence L1={l1:.3f}. "
        + ("Material - the window choice is driving the allocation; "
           "the shorter window should be argued for explicitly."
           if l1 > 0.10 else
           "Immaterial - the allocation is robust to the window choice."))
```

pass `challenger_view=challenger_view` into the `Decision(...)` constructor, and add `import numpy as np` to the module imports.

- [ ] **Step 4: Run tests; full suite. Commit** — `git commit -am "feat(governance): challenger view on the estimation window in every autopilot decision (R0.4c)"`

---

### Task 9: R0.5 — Close the reflection loop

**Files:**
- Create: `qlab/governance/reflection.py`
- Modify: `qlab/state/registry.py` (`pending_decisions`), `qlab/autopilot/loop.py` (store `est_vol` in the decision; call `resolve_pending` at session start in both `run_once` and `daily_ops`)
- Test: `tests/test_autopilot.py`

**Interfaces:**
- Produces: `Registry.pending_decisions() -> list[dict]` (decisions with NULL `realized_outcome`); `resolve_pending(registry, prices: pd.DataFrame, horizon_days=63) -> int` — for each pending decision older than `horizon_days` trading days, computes the realized outcome **of the judgment** (realized vol of the decided targets vs the estimate recorded at decision time; whether the regime call matched the realized-vol quantile) and writes a reflection via the existing `update_reflection`. Returns the number resolved. `run_once` decisions now include `choice["est_vol"]` (annualized, from `portfolio_moments`).

- [ ] **Step 1: Write the failing test**

```python
def test_reflection_loop_resolves_pending_decisions(tmp_registry):
    import pandas as pd
    from datetime import date
    from qlab.core import data as market
    from qlab.core.types import Decision
    from qlab.governance.reflection import resolve_pending
    from qlab.trader.mandate import load_mandate
    tickers = load_mandate().universe_whitelist
    prices = market.get_prices(tickers, "2019-01-01", "2021-12-31", offline=True, seed=7)
    n = len(tickers)
    tmp_registry.log_decision(Decision(
        as_of=date(2020, 6, 30), kind="regime",
        choice={"targets": {t: 1.0 / n for t in tickers},
                "regime": "calm", "est_vol": 0.10},
        rationale="test"))
    resolved = resolve_pending(tmp_registry, prices, horizon_days=63)
    assert resolved == 1
    dec = tmp_registry.recent_decisions(limit=1)[0]
    assert dec["realized_outcome"]["realized_vol"] > 0
    assert "vol" in dec["reflection"]
    assert resolve_pending(tmp_registry, prices) == 0        # idempotent
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Registry helper**

```python
    def pending_decisions(self) -> list[dict]:
        return self._rows(
            "SELECT * FROM decisions WHERE realized_outcome IS NULL "
            "OR json_extract_string(realized_outcome, '$') = 'null' "
            "ORDER BY as_of", [])
```

(`log_decision` serializes `None` as the JSON string `null` — cover both.)

- [ ] **Step 4: Implement `qlab/governance/reflection.py`**

```python
"""Close the reflection loop: score the *judgment*, not the portfolio.

TradingAgents' best component (research-plan §3, steal list), re-targeted:
when a decision's horizon has elapsed, compute what actually happened to the
quantities the judgment was about (realized vol vs the estimate; did the regime
call match), write a compact reflection, and let recent_decisions inject it
into the next session's context. Deterministic v1 - an LLM can rewrite the
text later; the numbers are the point.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

_TRADING_DAYS = 252


def resolve_pending(registry, prices: pd.DataFrame, horizon_days: int = 63) -> int:
    resolved = 0
    idx = prices.index
    for dec in registry.pending_decisions():
        as_of = pd.Timestamp(dec["as_of"])
        future = idx[idx > as_of]
        if len(future) < horizon_days:
            continue                                   # horizon not elapsed yet
        window = future[:horizon_days]
        targets = (dec.get("choice") or {}).get("targets") or {}
        cols = [t for t in targets if t in prices.columns]
        if not cols:
            continue
        w = np.array([targets[t] for t in cols], dtype=float)
        rets = prices.loc[window, cols].pct_change().dropna(how="all").fillna(0.0)
        port = rets.to_numpy() @ w
        realized_vol = float(np.std(port, ddof=1) * np.sqrt(_TRADING_DAYS))
        est_vol = float((dec.get("choice") or {}).get("est_vol") or 0.0)
        regime_call = (dec.get("choice") or {}).get("regime", "?")
        stressed = realized_vol > 0.18
        outcome = {"realized_vol": realized_vol, "est_vol": est_vol,
                   "horizon_days": horizon_days,
                   "regime_call": regime_call,
                   "regime_realized": "stress" if stressed else "calm"}
        ratio = realized_vol / est_vol if est_vol > 0 else float("nan")
        reflection = (
            f"Realized ann vol {realized_vol:.1%} vs estimated {est_vol:.1%} "
            f"(ratio {ratio:.2f}). Regime call '{regime_call}' was "
            f"{'consistent with' if (regime_call == outcome['regime_realized']) else 'contradicted by'} "
            f"the realized outcome. "
            + ("Estimate was materially low - revisit window/shrinkage next time."
               if est_vol and ratio > 1.5 else
               "Estimation judgment held up over this horizon."))
        registry.update_reflection(dec["decision_id"], outcome, reflection)
        registry.record_event("reflection_resolved",
                              {"decision_id": dec["decision_id"],
                               "realized_vol": realized_vol})
        resolved += 1
    return resolved
```

- [ ] **Step 5: Wire the loop** — in `run_once` (a) call `resolve_pending(reg, snap.prices)` right after the snapshot is built; (b) add the estimate to the decision: compute after the champion solve

```python
    from qlab.core.moments import portfolio_moments
    ms_diag = diag.get("moments", {})
    est_vol = float(ms_diag.get("avg_vol", 0.0)) * np.sqrt(252)
```

and include `"est_vol": est_vol` in `decision.choice`. In `daily_ops`, call `resolve_pending(reg, snap.prices)` after its snapshot too.

- [ ] **Step 6: Run tests; full suite. Commit** — `git commit -am "feat(governance): reflection loop closed - pending decisions resolve against realized vol (R0.5)"`

---

### Task 10: Events read API + emission at the MCP tool boundary

> **[RESCOPED 2026-07-17 after commit a28f333]** The concurrent TUI session delivered `Registry.read_events` (with cursor + limit clamp, tested) and the `/api/events` route. **Remaining scope: only the `CallBudget` on-charge hook + `LabState` wiring + `test_tool_calls_emit_events`.** Skip the registry `read_events` implementation, its test, and the ui/server route below — they exist. Note for the final review: `read_events(after=…)` uses strict `ts > ?` which can skip same-timestamp events (latent, low severity).

**Files:**
- Modify: `qlab/state/registry.py` (`read_events`), `qlab/mcp/guardrails.py` (`CallBudget` on-charge callback; `LabState` wires it), `qlab/ui/server.py` (`GET /api/events` route)
- Test: `tests/test_registry.py`, `tests/test_ui.py`

**Interfaces:**
- Produces: `Registry.read_events(limit=100, since_ts=None) -> list[dict]` (newest first); every lab tool call now emits a `tool_call` event (via `CallBudget(on_charge=...)` — `charge()` already runs on every tool, making it the single chokepoint); `GET /api/events?limit=N` returns `{"events": [...]}`. This is the TUI's feed (R1).

- [ ] **Step 1: Write the failing tests** (`tests/test_registry.py`)

```python
def test_read_events_newest_first():
    from qlab.state.registry import Registry
    reg = Registry(":memory:")
    reg.record_event("a", {"i": 1})
    reg.record_event("b", {"i": 2})
    ev = reg.read_events(limit=10)
    assert [e["kind"] for e in ev][:2] == ["b", "a"]
    assert ev[0]["payload"]["i"] == 2


def test_tool_calls_emit_events():
    from qlab.mcp.guardrails import LabState
    from qlab.state.registry import Registry
    st = LabState(offline=True, registry=Registry(":memory:"))
    st.budget.charge("data.fetch_universe")
    kinds = [e["kind"] for e in st.registry.read_events(10)]
    assert "tool_call" in kinds
```

(Check `LabState.__init__`'s signature in `qlab/mcp/guardrails.py`: if it doesn't yet accept a `registry` kwarg, add it — default `None` → construct as today. The R1 plan's combined server relies on the same kwarg.)

and (`tests/test_ui.py`, following its in-process `handle_api` pattern):

```python
def test_events_route(ui_session):
    from qlab.ui.server import handle_api
    ui_session.registry.record_event("demo", {"x": 1})
    status, obj = handle_api(ui_session, "GET", "/api/events", {"limit": ["10"]}, {})
    assert status == 200 and obj["events"][0]["kind"] == "demo"
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** — registry:

```python
    def read_events(self, limit: int = 100, since_ts: str | None = None) -> list[dict]:
        if since_ts:
            return self._rows("SELECT * FROM events WHERE ts > ? "
                              "ORDER BY ts DESC LIMIT ?", [since_ts, limit])
        return self._rows("SELECT * FROM events ORDER BY ts DESC LIMIT ?", [limit])
```

guardrails — extend `CallBudget.__init__` with `on_charge=None`; at the end of `charge()` add `if self.on_charge: self.on_charge(tool_name)`. In `LabState.__init__`, construct the budget as `CallBudget(on_charge=lambda name: self.registry.record_event("tool_call", {"tool": name}))` (after the registry is set). ui/server.py — add to `handle_api`:

```python
    if method == "GET" and path == "/api/events":
        limit = int(query.get("limit", ["100"])[0])
        return 200, {"events": session.registry.read_events(limit)}
```

- [ ] **Step 4: Run tests; full suite. Commit** — `git commit -am "feat(events): read API + tool-boundary emission - the TUI feed (R0 events)"`

---

### Task 11: Data-driven co-moment shrinkage with a 1-factor target

**Files:**
- Modify: `qlab/core/moments.py` (`co_moments` gains `target` and auto intensity), `qlab/arms.py` (`MomentsConfig.comoment_target`)
- Test: `tests/test_moments.py`

Today `co_moments` shrinks by a fixed 0.5 toward Gaussian targets only. Per research-plan §7.2 (Martellini–Ziemann), add a 1-factor target and a data-driven intensity so the "hard technical task" stops being a magic constant.

**Interfaces:**
- Produces: `co_moments(X, cov, *, comoment_shrinkage=0.5, target="isserlis")` where `comoment_shrinkage` may be a float or `"auto"`, and `target ∈ {"isserlis", "one_factor"}`. Auto intensity: `delta_k = clip(rho_k / (1 + rho_k), 0.2, 0.9)` with `rho_3 = C(n+2,3)/T`, `rho_4 = C(n+3,4)/T` (parameters-per-observation ratio — more tensor entries per data point → more shrinkage). One-factor targets from the equal-weight market factor `f_t = mean_i(X_ti)`: betas `b_i = cov(X_i, f)/var(f)`, `S_target[ijk] = b_i b_j b_k · E[f³]`, `K_target[ijkl] = b_i b_j b_k b_l · E[f⁴] + Isserlis(Σ) − b_i b_j b_k b_l · 3·var(f)²` (factor-implied excess kurtosis on top of the Gaussian combination). `estimate_moments` passes both through; `MomentsConfig` gains `comoment_target: str = "isserlis"`; diagnostics record the effective `delta3`/`delta4`.

- [ ] **Step 1: Write the failing tests** (`tests/test_moments.py`)

```python
def test_auto_comoment_shrinkage_decreases_with_sample_size():
    rng = np.random.default_rng(4)
    n = 5
    from qlab.core.moments import co_moments, ledoit_wolf
    deltas = []
    for T in (150, 3000):
        X = rng.standard_t(5, (T, n)) * 0.01
        cov, _ = ledoit_wolf(X)
        co_moments(X, cov, comoment_shrinkage="auto")
        deltas.append(co_moments.last_deltas["delta4"])
    assert deltas[1] < deltas[0]
    assert 0.2 <= deltas[0] <= 0.9


def test_one_factor_target_tracks_factor_skew():
    rng = np.random.default_rng(9)
    T, n = 2000, 4
    f = rng.gamma(2.0, 1.0, T) - 2.0            # skewed common factor
    X = np.outer(f, np.ones(n)) * 0.01 + rng.normal(0, 0.001, (T, n))
    from qlab.core.moments import co_moments, ledoit_wolf
    cov, _ = ledoit_wolf(X)
    S_of, _ = co_moments(X, cov, comoment_shrinkage=1.0, target="one_factor")
    S_gauss, _ = co_moments(X, cov, comoment_shrinkage=1.0, target="isserlis")
    assert abs(S_of).max() > 0                   # 1-factor keeps skew structure
    assert abs(S_gauss).max() == 0               # gaussian target zeroes it
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** — replace `co_moments` in `qlab/core/moments.py`:

```python
from math import comb


def co_moments(X, cov, *, comoment_shrinkage=0.5, target: str = "isserlis"):
    """Central coskew/cokurt with structured shrinkage (Martellini-Ziemann style).

    ``comoment_shrinkage`` is a float or "auto" (parameters-per-observation
    rule); ``target`` is "isserlis" (Gaussian) or "one_factor" (equal-weight
    market factor - keeps factor-driven skew/kurt structure while killing
    idiosyncratic tensor noise).
    """
    Xc = X - X.mean(axis=0, keepdims=True)
    T, n = Xc.shape
    coskew = np.einsum("ti,tj,tk->ijk", Xc, Xc, Xc) / T
    cokurt = np.einsum("ti,tj,tk,tl->ijkl", Xc, Xc, Xc, Xc) / T

    if comoment_shrinkage == "auto":
        rho3 = comb(n + 2, 3) / T
        rho4 = comb(n + 3, 4) / T
        d3 = float(np.clip(rho3 / (1 + rho3), 0.2, 0.9))
        d4 = float(np.clip(rho4 / (1 + rho4), 0.2, 0.9))
    else:
        d3 = d4 = float(np.clip(comoment_shrinkage, 0.0, 1.0))
    co_moments.last_deltas = {"delta3": d3, "delta4": d4}

    kurt_gauss = (np.einsum("ij,kl->ijkl", cov, cov)
                  + np.einsum("ik,jl->ijkl", cov, cov)
                  + np.einsum("il,jk->ijkl", cov, cov))
    if target == "one_factor":
        f = Xc.mean(axis=1)
        vf = float(np.var(f)) or 1e-18
        b = (Xc.T @ f) / (T * vf)
        m3f, m4f = float(np.mean(f ** 3)), float(np.mean(f ** 4))
        skew_t = m3f * np.einsum("i,j,k->ijk", b, b, b)
        kurt_t = kurt_gauss + (m4f - 3.0 * vf ** 2) * np.einsum(
            "i,j,k,l->ijkl", b, b, b, b)
    elif target == "isserlis":
        skew_t = np.zeros_like(coskew)
        kurt_t = kurt_gauss
    else:
        raise ValueError(f"unknown comoment target {target!r}")

    if d3 > 0:
        coskew = (1 - d3) * coskew + d3 * skew_t
    if d4 > 0:
        cokurt = (1 - d4) * cokurt + d4 * kurt_t
    return coskew, cokurt
```

In `estimate_moments`, accept `comoment_shrinkage: float | str` and a new `comoment_target: str = "isserlis"` kwarg, pass both to `co_moments`, and record `co_moments.last_deltas` plus the target into `diagnostics`. In `qlab/arms.py`, add `comoment_target: str = "isserlis"` to `MomentsConfig` and pass it through `estimate`; in `qlab/experiment.py`, read it from the spec's `moments:` block.

- [ ] **Step 4: Run tests; full suite** (existing `test_comoment_tensor_shapes_and_gaussian_shrink` pins the Isserlis default — must stay green).

- [ ] **Step 5: Commit** — `git commit -am "feat(moments): data-driven co-moment shrinkage + one-factor target (R0)"`

---

### Task 12: R0 exit gate — real-data sanity run

**Files:** none (verification only)

- [ ] **Step 1:** `.venv/bin/python -m pytest tests/ -q` → all green (baseline 49 + ~15 new).
- [ ] **Step 2:** `qlab prewarm --universe core` (online) then `qlab batch configs/specs/ablation_v1.yaml` and confirm in the output: A3's weights differ from A1's (challenger/diagnostics show divergence), `deflated_sharpe` values are neither all ~0 nor all ~1, every arm's metrics include `sharpe_ci`.
- [ ] **Step 3:** `qlab run-once --offline --dry-run` → summary shows `referee: PASS`, `reconcile: clean`, decision has a challenger view; run again and confirm a reflection resolves once the horizon allows (or run with two historical `as_of` dates 6 months apart).
- [ ] **Step 4:** Tag: `git tag r0-trust-repair && git push origin main --tags` (push only if the user has confirmed pushing).
