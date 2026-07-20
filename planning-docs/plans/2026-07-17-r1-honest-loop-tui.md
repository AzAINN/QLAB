# R1 — One Honest Loop + TUI Implementation Plan

> **Status: IMPLEMENTED / HISTORICAL RECIPE.** R1 landed before the 2026-07-19
> cleanup. References below to staged quantum controls are superseded by the
> offline algorithm boundary in ../2026-07-19-continuation-ledger.md.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consolidate DB ownership into one MCP process, add the deterministic signals layer (turbulence/absorption/FRED vol indices/regime conditioning), the Tier-1 estimators (nonlinear shrinkage, vol targeting), and a Textual operator console — culminating in the July 31 demo: a referee-gated rebalance watched live in the TUI.

**Architecture:** New `qlab/signals/` package (hard signals + regime conditioning), one combined MCP server (`qlab/mcp/server.py`) owning the single DuckDB writer, new `qlab/tui/` Textual app that observes via the existing HTTP API and launches work via subprocesses — never a second mutation path.

**Tech Stack:** Python ≥3.10; new optional deps: `textual` + `httpx` (extra `[tui]`), `hmmlearn` (extra `[signals]`, optional with fallback). FRED CSV endpoints (no API key).

## Global Constraints

- **Prerequisite: the R0 plan (2026-07-17-r0-trust-repair.md) is fully merged.** All paths are repo-root.
- Tests run offline; network-dependent code paths must have a cached/synthetic fallback exercised in tests.
- The TUI **never mutates state directly** — reads via HTTP API, acts via `qlab` CLI / headless `claude -p` subprocesses.
- MCP tool *names* stay frozen; only the hosting process changes.
- Full suite green before each commit; commit per task with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Combined MCP server — one process owns the DuckDB book

> **[AMENDED 2026-07-17 after commit a28f333]** The TUI slice establishes the UI runtime (`UISession`) as the single paper-book owner while it runs, with `qlab/mcp/tui_proxy.py` as the HTTP-proxy MCP for governed sessions. The combined server below is for **headless orchestrator use** (no UI runtime alive) and MUST add a startup guard: probe the owner port (default 8765, `GET /api/system`); if it responds, exit with a clear message directing to the `qlab-operator` proxy instead of opening DuckDB — never two writers. Add a `--port` env override (`QLAB_UI_PORT`).

**Files:**
- Modify: `qlab/mcp/quant_lab.py`, `qlab/mcp/quant_trader.py` (extract `register_*_tools(app, st)`), `qlab/agents/loader.py` (single server name), `.mcp.json`
- Create: `qlab/mcp/server.py`
- Test: `tests/test_agents.py` (adapter regen), new `tests/test_mcp_server.py`

`.mcp.json` currently launches quant-lab and quant-trader as two processes, each opening the same DuckDB file read-write — DuckDB allows only one writer process, so the two-server topology deadlocks the moment both touch the book. Fix: one FastMCP app exposing both tool namespaces over one shared `Registry`. Role separation lives where it actually is enforced — per-agent tool allowlists.

**Interfaces:**
- Produces: `register_lab_tools(app, st: LabState) -> None` and `register_trader_tools(app, st: TraderState) -> None` (all existing `@app.tool` registrations moved inside, bodies unchanged); `build_server()` in each module preserved as a thin wrapper (creates own app + state, calls the register fn) so standalone use keeps working; `qlab/mcp/server.py::build_combined_server() -> app` sharing one `Registry`; `.mcp.json` has a single `qlab` entry running `python -m qlab.mcp.server`.

- [ ] **Step 1: Write the failing test** (`tests/test_mcp_server.py`)

```python
class StubApp:
    def __init__(self):
        self.names = []

    def tool(self, name: str):
        def deco(fn):
            self.names.append(name)
            return fn
        return deco


def test_combined_registration_exposes_both_namespaces():
    from qlab.mcp.quant_lab import register_lab_tools
    from qlab.mcp.quant_trader import register_trader_tools
    from qlab.mcp.guardrails import LabState
    from qlab.mcp.quant_trader import TraderState
    from qlab.state.registry import Registry
    reg = Registry(":memory:")
    app = StubApp()
    register_lab_tools(app, LabState(offline=True, registry=reg))
    register_trader_tools(app, TraderState(registry=reg, offline=True))
    assert "moments.estimate" in app.names
    assert "registry.log_verdict" in app.names
    assert "propose_rebalance" in app.names and "execute_plan" in app.names
    assert not any("place" in n and "order" in n for n in app.names)  # still no raw order tool


def test_lab_and_trader_share_one_registry():
    from qlab.mcp.guardrails import LabState
    from qlab.mcp.quant_trader import TraderState
    from qlab.state.registry import Registry
    reg = Registry(":memory:")
    lab, trader = LabState(offline=True, registry=reg), TraderState(registry=reg, offline=True)
    lab.registry.record_event("x", {})
    assert trader.registry.read_events(5)[0]["kind"] == "x"
```

(If `LabState.__init__` doesn't yet accept a `registry` kwarg, add it — default `None` → construct as today.)

- [ ] **Step 2: Run to verify failure** (`register_lab_tools` missing).

- [ ] **Step 3: Extract the registration functions.** In `qlab/mcp/quant_lab.py`, change `build_server` to:

```python
def register_lab_tools(app, st: LabState) -> None:
    # ... every existing @app.tool definition, verbatim, unchanged bodies ...

def build_server(state: LabState | None = None):
    FastMCP = require_fastmcp()
    st = state or LabState(offline=os.environ.get("QLAB_OFFLINE") == "1")
    app = FastMCP("quant-lab")
    register_lab_tools(app, st)
    return app
```

Mirror in `qlab/mcp/quant_trader.py` (`register_trader_tools(app, st: TraderState)`).

- [ ] **Step 4: Create `qlab/mcp/server.py`**

```python
"""The combined qlab MCP server - one process, one DuckDB writer, both roles.

DuckDB permits a single read-write process. Running quant-lab and quant-trader
as separate servers (the original .mcp.json) makes them fight over the file
lock; here both tool namespaces mount on one FastMCP app over one shared
Registry. Governance separation is enforced where it lives: per-agent tool
allowlists (agents/*.md), not process boundaries.
"""
from __future__ import annotations

import os

from qlab.mcp.guardrails import LabState, require_fastmcp
from qlab.mcp.quant_lab import register_lab_tools
from qlab.mcp.quant_trader import TraderState, register_trader_tools
from qlab.state.registry import Registry


def build_combined_server():
    FastMCP = require_fastmcp()
    offline = os.environ.get("QLAB_OFFLINE") == "1"
    app = FastMCP("qlab")
    registry = Registry()
    register_lab_tools(app, LabState(offline=offline, registry=registry))
    register_trader_tools(app, TraderState(registry=registry, offline=offline))
    return app


def main() -> None:  # pragma: no cover
    build_combined_server().run()


if __name__ == "__main__":  # pragma: no cover
    main()
```

- [ ] **Step 5: Repoint `.mcp.json`**

```json
{
  "mcpServers": {
    "qlab": {
      "command": "python",
      "args": ["-m", "qlab.mcp.server"],
      "env": {},
      "description": "Research lab + execution gateway in one process (single DuckDB writer). No raw order tool by design."
    }
  }
}
```

- [ ] **Step 6: Single server name in the agent adapters.** In `qlab/agents/loader.py`, find where Bob personas derive `mcp_servers` from tool prefixes (`server_scopes`) and collapse the mapping so every scope resolves to `"qlab"` (add a module-level `SERVER_NAME = "qlab"` and emit `mcp_servers: [SERVER_NAME]`). Re-run the loader sync to regenerate `.claude/agents/` and `.bob/personas/`.

- [ ] **Step 7: Run tests** — new file + `tests/test_agents.py` (its sync test must pass with the regenerated adapters) + full suite.

- [ ] **Step 8: Commit** — `git commit -am "refactor(mcp): combined single-process server; one DuckDB writer (R1)"`

---

### Task 2: Hard-signals module — turbulence, absorption ratio, FRED vol indices

**Files:**
- Create: `qlab/signals/__init__.py`, `qlab/signals/hard.py`
- Test: `tests/test_signals.py` (new)

**Interfaces:**
- Produces (all consumed by Task 3 and the TUI):
  - `turbulence(returns: pd.DataFrame, lookback=252) -> pd.Series` — Mahalanobis distance per day.
  - `absorption_ratio(returns: pd.DataFrame, window=500, n_components=None) -> pd.Series` — top-eigenvector variance share (Kritzman et al.), `n_components` defaults to `ceil(n/5)`.
  - `fred_series(series_id: str, start="2008-01-01", *, cache_dir=".lab/cache", offline=False, seed=7) -> pd.Series` — FRED CSV fetch with parquet cache; offline → cache, else deterministic synthetic.
  - `composite_regime(snapshot, *, offline=False) -> dict` with keys `regime` (`"calm"|"stress"`), `regime_lambda` (float ∈ [0,1]), `components` (dict of the individual signal percentiles).

- [ ] **Step 1: Write the failing tests**

```python
import numpy as np
import pandas as pd


def _two_regime_returns(seed=7, n=6):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2018-01-01", periods=1000)
    calm = rng.normal(0, 0.006, (500, n))
    common = rng.normal(0, 0.02, (500, 1))
    stress = 0.8 * common + rng.normal(0, 0.009, (500, n))   # high vol + high corr
    return pd.DataFrame(np.vstack([calm, stress]), index=idx,
                        columns=[f"A{i}" for i in range(n)])


def test_turbulence_spikes_in_stress():
    from qlab.signals.hard import turbulence
    r = _two_regime_returns()
    t = turbulence(r, lookback=252)
    assert t.iloc[-100:].mean() > 2 * t.iloc[260:360].mean()


def test_absorption_rises_when_correlation_concentrates():
    from qlab.signals.hard import absorption_ratio
    r = _two_regime_returns()
    a = absorption_ratio(r, window=300)
    assert a.iloc[-1] > a.iloc[350] + 0.1


def test_fred_series_offline_is_deterministic_without_network(tmp_path):
    from qlab.signals.hard import fred_series
    s1 = fred_series("VIXCLS", cache_dir=tmp_path, offline=True, seed=3)
    s2 = fred_series("VIXCLS", cache_dir=tmp_path, offline=True, seed=3)
    assert len(s1) > 100 and (s1 == s2).all() and (s1 > 0).all()


def test_composite_regime_lambda_higher_in_stress():
    from qlab.core.types import DataSnapshot
    from qlab.signals.hard import composite_regime
    r = _two_regime_returns()
    px = (1 + r).cumprod() * 100
    calm_snap = DataSnapshot(list(px.columns), px, px.index[480].date())
    stress_snap = DataSnapshot(list(px.columns), px, px.index[-1].date())
    lam_calm = composite_regime(calm_snap, offline=True)["regime_lambda"]
    lam_stress = composite_regime(stress_snap, offline=True)["regime_lambda"]
    assert lam_stress > lam_calm + 0.2
    assert composite_regime(stress_snap, offline=True)["regime"] == "stress"
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement `qlab/signals/hard.py`**

```python
"""Deterministic market-stress signals - the injection-immune half of the
signals layer (roadmap Amendment A / §3). Everything here is computed from
prices or fetched from fixed public index series; no text, no LLM.
"""
from __future__ import annotations

import hashlib
from math import ceil
from pathlib import Path

import numpy as np
import pandas as pd

from qlab.core.types import DataSnapshot

_TRADING_DAYS = 252


def turbulence(returns: pd.DataFrame, lookback: int = 252) -> pd.Series:
    """Chow-Kritzman turbulence: d_t = (r_t - mu)' Sigma^-1 (r_t - mu)."""
    out = {}
    X = returns.to_numpy(dtype=float)
    for t in range(lookback, len(returns)):
        W = X[t - lookback:t]
        mu = W.mean(axis=0)
        inv = np.linalg.pinv(np.cov(W, rowvar=False))
        d = X[t] - mu
        out[returns.index[t]] = float(d @ inv @ d)
    return pd.Series(out)


def absorption_ratio(returns: pd.DataFrame, window: int = 500,
                     n_components: int | None = None, step: int = 5) -> pd.Series:
    """Kritzman et al.: share of variance absorbed by the top eigenvectors."""
    n = returns.shape[1]
    k = n_components or max(1, ceil(n / 5))
    out = {}
    X = returns.to_numpy(dtype=float)
    for t in range(window, len(returns), step):
        vals = np.linalg.eigvalsh(np.cov(X[t - window:t], rowvar=False))
        out[returns.index[t]] = float(vals[-k:].sum() / max(vals.sum(), 1e-18))
    return pd.Series(out)


def fred_series(series_id: str, start: str = "2008-01-01", *,
                cache_dir=".lab/cache", offline: bool = False,
                seed: int = 7) -> pd.Series:
    cache = Path(cache_dir) / f"fred_{series_id}.parquet"
    if cache.exists():
        return pd.read_parquet(cache)["value"]
    if offline:
        # deterministic synthetic stand-in (positive, VIX-like mean level)
        h = int(hashlib.md5(f"{series_id}:{seed}".encode()).hexdigest()[:8], 16)
        rng = np.random.default_rng(h)
        idx = pd.bdate_range(start, periods=1500)
        level = 18.0 * np.exp(np.cumsum(rng.normal(0, 0.03, len(idx))
                                        - 0.0005))            # mean-ish reverting
        return pd.Series(level, index=idx, name="value")
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    df = pd.read_csv(url, index_col=0, parse_dates=True, na_values=".")
    s = df.iloc[:, 0].dropna().astype(float).rename("value")
    s = s[s.index >= pd.Timestamp(start)]
    cache.parent.mkdir(parents=True, exist_ok=True)
    s.to_frame().to_parquet(cache)
    return s


def composite_regime(snapshot: DataSnapshot, *, offline: bool = False) -> dict:
    """Blend price-based stress signals into one lambda in [0, 1].

    lambda = mean of (turbulence percentile, absorption percentile, trailing-vol
    percentile), each computed against the snapshot's own history. VIX-family
    series can sharpen this when cached; the price-only version is always
    available and is the referee-auditable floor.
    """
    rets = snapshot.log_returns().dropna(how="any")
    if len(rets) < 300:
        return {"regime": "calm", "regime_lambda": 0.0,
                "components": {}, "method": "insufficient_data"}
    turb = turbulence(rets, lookback=252)
    absr = absorption_ratio(rets, window=min(500, len(rets) - 5))
    vol = rets.mean(axis=1).rolling(63).std().dropna() * np.sqrt(_TRADING_DAYS)
    comp = {
        "turbulence_pct": float((turb <= turb.iloc[-1]).mean()),
        "absorption_pct": float((absr <= absr.iloc[-1]).mean()),
        "vol_pct": float((vol <= vol.iloc[-1]).mean()),
    }
    lam = float(np.clip(np.mean(list(comp.values())), 0.0, 1.0))
    return {"regime": "stress" if lam > 0.6 else "calm",
            "regime_lambda": lam, "components": comp, "method": "composite_v1"}
```

(`qlab/signals/__init__.py` re-exports the four public functions.)

- [ ] **Step 4: Run tests; tune only test thresholds if a generator quirk trips one (signal *direction* must hold). Full suite. Commit** — `git commit -am "feat(signals): turbulence, absorption ratio, FRED fetch, composite regime lambda (R1)"`

---

### Task 3: Regime-conditional moments + arm B4

**Files:**
- Create: `qlab/signals/condition.py`
- Modify: `qlab/arms.py` (`MomentsConfig.regime_conditional`, per-arm override, conditioning hook in `estimate`), `configs/specs/ablation_v1.yaml` (add B4)
- Test: `tests/test_signals.py`

**Interfaces:**
- Produces: `regime_labels(returns, window=63, quantile=0.8) -> np.ndarray` (bool stress mask per row); `condition_covariance(X, labels, lam) -> np.ndarray` (λ-mix of per-regime Ledoit-Wolf covariances, symmetrized + PSD-floored); `MomentsConfig(regime_conditional: bool = False)`; `solve_arm` honors `arm.params["regime_conditional"]`; spec arm `B4 = min_variance / classical / {regime_conditional: true}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_conditioned_covariance_scales_with_lambda():
    from qlab.signals.condition import condition_covariance, regime_labels
    r = _two_regime_returns()
    X = r.to_numpy()
    labels = regime_labels(r)
    cov0 = condition_covariance(X, labels, 0.0)
    cov1 = condition_covariance(X, labels, 1.0)
    assert np.trace(cov1) > 1.5 * np.trace(cov0)          # stress cov is hotter
    for c in (cov0, cov1):
        assert np.all(np.linalg.eigvalsh(c) > -1e-10)     # PSD


def test_b4_arm_runs_regime_conditional():
    from datetime import date
    from qlab.arms import Arm, MomentsConfig, solve_arm
    from qlab.core.types import DataSnapshot
    r = _two_regime_returns()
    px = (1 + r).cumprod() * 100
    snap = DataSnapshot(list(px.columns), px, px.index[-1].date())
    arm = Arm("B4", "min_variance", "classical", {"regime_conditional": True})
    w, diag = solve_arm(arm, snap, moments=MomentsConfig(lookback_days=750))
    assert abs(sum(w.values) - 1.0) < 1e-6
    assert diag["moments"].get("regime_lambda") is not None
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement `qlab/signals/condition.py`**

```python
"""Regime-conditional moment estimation (signals v1: lambda-mixing).

Sigma(lam) = (1 - lam) * Sigma_calm + lam * Sigma_stress, with lam supplied by
the composite hard-signal regime (later: clamped LLM views, roadmap §3). The
solver stack is untouched - conditioning only changes the coefficients.
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
```

- [ ] **Step 4: Wire into `qlab/arms.py`** — add the field to `MomentsConfig`:

```python
    regime_conditional: bool = False
```

In `solve_arm`, honor a per-arm override right after `moments = moments or MomentsConfig()`:

```python
    if "regime_conditional" in arm.params:
        from dataclasses import replace
        moments = replace(moments, regime_conditional=bool(arm.params["regime_conditional"]))
```

and in `estimate(...)`, after the base `MomentSet` is built, condition it:

```python
def estimate(snapshot, cfg, *, higher):
    ms = estimate_moments(snapshot, lookback_days=cfg.lookback_days,
                          shrinkage=cfg.shrinkage, denoise=cfg.denoise,
                          comoment_shrinkage=cfg.comoment_shrinkage,
                          include_mu=False, higher_moments=higher)
    if cfg.regime_conditional:
        from qlab.signals.condition import condition_covariance, regime_labels
        from qlab.signals.hard import composite_regime
        rets = snapshot.log_returns(cfg.lookback_days).dropna(how="any")
        X = rets.to_numpy(dtype=float)
        reg = composite_regime(snapshot)
        ms.cov = condition_covariance(X, regime_labels(rets), reg["regime_lambda"])
        ms.diagnostics["regime_lambda"] = reg["regime_lambda"]
        ms.diagnostics["regime"] = reg["regime"]
    return ms
```

- [ ] **Step 5: Add B4 to `configs/specs/ablation_v1.yaml`** under `arms:` (keep existing entries):

```yaml
  - {id: B4, objective: min_variance, solver: classical,
     params: {regime_conditional: true}}
```

- [ ] **Step 6: Run tests; full suite. Commit** — `git commit -am "feat(signals): regime-conditional covariance + B4 baseline arm (R1)"`

---

### Task 4: Nonlinear shrinkage + volatility-targeting overlay

**Files:**
- Modify: `qlab/core/moments.py` (nonlinear shrinkage option), `qlab/arms.py` (`target_vol` overlay in `build_policy`), `configs/specs/ablation_v1.yaml` (A3t research arm)
- Test: `tests/test_moments.py`, `tests/test_backtest.py`

**Interfaces:**
- Produces: `nonlinear_shrinkage(X) -> tuple[np.ndarray, float]` (Ledoit-Wolf 2020 analytical kernel estimator; second element reports mean eigenvalue shift as the "intensity" diagnostic) selectable via `estimate_moments(..., shrinkage="nonlinear")`; `build_policy` wraps the arm policy with exposure scaling when `arm.params["target_vol"]` is set: `scale = min(1.0, target_vol / est_vol)`, remainder implicitly cash (weights sum < 1 — **research arms only**; the mandate's fully-invested check stays untouched, so A3t cannot reach the trader).

- [ ] **Step 1: Write the failing tests** (`tests/test_moments.py`)

```python
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
```

and (`tests/test_backtest.py`):

```python
def test_vol_target_overlay_reduces_realized_vol():
    from qlab.arms import Arm, MomentsConfig, build_policy
    from qlab.core.backtest import run_backtest
    from qlab.core import data as market
    px = market.get_prices(["ACWI", "BNDW", "GSG", "GLD", "VNQ"],
                           "2015-01-01", "2021-12-31", offline=True, seed=7)
    raw = Arm("A3", "mvsk", "classical_multistart",
              {"skew_lambda": 0.5, "kurt_lambda": 0.5})
    tgt = Arm("A3t", "mvsk", "classical_multistart",
              {"skew_lambda": 0.5, "kurt_lambda": 0.5, "target_vol": 0.06})
    cfg = MomentsConfig(lookback_days=504)
    m_raw = run_backtest(px, build_policy(raw, moments=cfg), cadence="quarterly",
                         lookback_days=504).metrics
    m_tgt = run_backtest(px, build_policy(tgt, moments=cfg), cadence="quarterly",
                         lookback_days=504).metrics
    assert m_tgt["ann_vol"] < m_raw["ann_vol"]
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement nonlinear shrinkage** (`qlab/core/moments.py`) — the Ledoit-Wolf (2020) *analytical* estimator:

```python
def nonlinear_shrinkage(X: np.ndarray) -> tuple[np.ndarray, float]:
    """Ledoit-Wolf (2020) analytical nonlinear shrinkage of sample eigenvalues.

    Epanechnikov-kernel estimate of the sample spectral density and its Hilbert
    transform give per-eigenvalue shrinkage; strictly dominates linear LW for
    n growing with T. Pure numpy port of the published analytical formulas.
    """
    T, n = X.shape
    Xc = X - X.mean(axis=0, keepdims=True)
    S = (Xc.T @ Xc) / T
    lam, U = np.linalg.eigh(S)
    lam = np.clip(lam, 0.0, None)
    q = n / T
    keep = lam[max(0, n - T):]                    # nonzero spectrum
    h = T ** (-1 / 3) * np.mean(keep)             # bandwidth (paper's c * T^-1/3)
    L = keep.reshape(-1, 1)
    x = (L - L.T) / h
    ftilde = np.mean(3 / (4 * h) * np.maximum(1 - x ** 2 / 5, 0) / np.sqrt(5), axis=1)
    Hftilde = np.mean(np.where(np.abs(x) < np.sqrt(5),
                               (-3 * x / (10 * h)) +
                               (3 / (4 * np.sqrt(5) * h)) * (1 - x ** 2 / 5) *
                               np.log(np.abs((np.sqrt(5) - x) / (np.sqrt(5) + x))
                                      + 1e-30),
                               (3 / (4 * np.sqrt(5) * h)) *
                               np.log(np.abs((np.sqrt(5) - x) / (np.sqrt(5) + x)))),
                      axis=1)
    denom = (np.pi * q * keep * ftilde) ** 2 + \
            (1 - q - np.pi * q * keep * Hftilde) ** 2
    d = keep / np.maximum(denom, 1e-18)
    dtilde = np.concatenate([np.full(max(0, n - T),
                                     1 / ((1 - q) *
                                          max(np.mean(1 / keep), 1e-18))
                                     if q > 1 else 0.0), d]) if n > T else d
    cov = (U * np.clip(dtilde, 1e-12, None)) @ U.T
    shift = float(np.mean(np.abs(dtilde - lam)) / max(np.mean(lam), 1e-18))
    return (cov + cov.T) / 2.0, shift
```

and add the branch in `estimate_moments`:

```python
    elif shrinkage == "nonlinear":
        cov, delta = nonlinear_shrinkage(X)
```

- [ ] **Step 4: Implement the overlay** — replace `build_policy` in `qlab/arms.py`:

```python
def build_policy(arm, *, moments=None, constraints=None):
    target_vol = arm.params.get("target_vol")

    def policy(snapshot: DataSnapshot) -> Weights:
        w, _diag = solve_arm(arm, snapshot, moments=moments, constraints=constraints)
        if not target_vol:
            return w
        # trailing realized vol of the *decided* portfolio (backward-looking only)
        cfg = moments or MomentsConfig()
        rets = snapshot.log_returns(min(cfg.lookback_days, 252)).dropna(how="any")
        arr = w.as_series().reindex(rets.columns).fillna(0.0).to_numpy()
        port = rets.to_numpy() @ arr
        est_vol = float(np.std(port, ddof=1) * np.sqrt(252))
        scale = min(1.0, float(target_vol) / max(est_vol, 1e-9))   # long-only: no leverage
        return Weights(tickers=w.tickers,
                       values=[float(v * scale) for v in w.values])

    policy.__name__ = f"policy_{arm.id}"
    return policy
```

- [ ] **Step 5: Add A3t to the spec** (research arm; note in the YAML comment that fully-invested mandate excludes it from trading):

```yaml
  - {id: A3t, objective: mvsk, solver: classical_multistart,
     params: {skew_lambda: 0.5, kurt_lambda: 0.5, target_vol: 0.08}}
```

- [ ] **Step 6: Run tests; full suite. Commit** — `git commit -am "feat(estimators): LW2020 nonlinear shrinkage + vol-targeting overlay arm (R1)"`

---

### Task 5: Textual TUI — the operator console

> **[SUPERSEDED 2026-07-17 by commit a28f333]** The concurrent session shipped a more complete console (`planning-docs/plans/2026-07-17-quiet-workstation-tui.md`): spine/canvas/agent-rail shell, HTTP-only observer invariant, command surface (`rebalance dry|paper`, `daily`, `batch`, `ask`, `governed`), paper-confirm modal, `qlab tui` with owned-server startup, and propose-only governed Claude via the `qlab-operator` proxy. **Skip this task entirely.** Follow-up captured elsewhere: surface referee verdicts in the audit view once R0-T6 lands (small `tui_snapshot` field addition — fold into R1 Task 6).

**Files:**
- Create: `qlab/tui/__init__.py`, `qlab/tui/client.py`, `qlab/tui/app.py`
- Modify: `qlab/autopilot/cli.py` (`qlab tui` subcommand), `pyproject.toml` (extras `tui = ["textual>=0.60", "httpx>=0.27"]`)
- Test: `tests/test_tui.py` (new)

**Interfaces:**
- Produces: `ApiClient(base_url)` with `.get(path, **params) -> dict` and `.post(path, body) -> dict` (httpx); `QlabTui(client)` Textual `App` with four panes — Portfolio (Static), Decisions (DataTable), Runs (DataTable), Events (RichLog) — refreshing every 2s, plus key bindings: `r` = dry-run rebalance, `R` = executed rebalance, `d` = daily-ops, `b` = batch ablation (all spawned as `qlab ...` subprocesses, output tailed into the Events pane), `g` = governed rebalance via headless Claude (`claude -p`), `q` = quit. `qlab tui --port 8765 [--online]` auto-starts the UI server subprocess if the port is closed. **Observer invariant: the TUI holds no Registry handle; every read is HTTP, every action is a subprocess.**

- [ ] **Step 1: Write the failing tests** (`tests/test_tui.py`) — framework-independent, using a fake client over the in-process API:

```python
class FakeClient:
    """Adapts qlab.ui.server.handle_api so TUI logic is testable without sockets."""

    def __init__(self, session):
        self.session = session

    def get(self, path, **params):
        from qlab.ui.server import handle_api
        q = {k: [str(v)] for k, v in params.items()}
        status, obj = handle_api(self.session, "GET", path, q, {})
        assert status == 200, obj
        return obj


def _session():
    from qlab.state.registry import Registry
    from qlab.ui.server import UISession
    return UISession(offline_default=True, registry=Registry(":memory:"))


def test_snapshot_gathers_all_panes():
    from qlab.tui.app import gather_snapshot
    s = _session()
    s.registry.record_event("demo", {"x": 1})
    snap = gather_snapshot(FakeClient(s))
    assert "equity" in snap["portfolio"]
    assert isinstance(snap["decisions"], list)
    assert isinstance(snap["runs"], list)
    assert snap["events"][0]["kind"] == "demo"


def test_app_composes_headless():
    import asyncio
    from qlab.tui.app import QlabTui

    async def run():
        app = QlabTui(client=FakeClient(_session()))
        async with app.run_test() as pilot:
            assert app.query_one("#portfolio") is not None
            assert app.query_one("#events") is not None
            await pilot.pause()
    asyncio.run(run())
```

- [ ] **Step 2: Add deps and run to verify failure** — `uv pip install -e '.[tui]' --python .venv/bin/python` after editing `pyproject.toml`; tests fail on missing module.

- [ ] **Step 3: Implement `qlab/tui/client.py`**

```python
from __future__ import annotations

import httpx


class ApiClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8765"):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(timeout=30.0)

    def get(self, path: str, **params) -> dict:
        r = self._http.get(self.base_url + path, params=params)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: dict | None = None) -> dict:
        r = self._http.post(self.base_url + path, json=body or {})
        r.raise_for_status()
        return r.json()
```

- [ ] **Step 4: Implement `qlab/tui/app.py`**

```python
"""qlab operator console (Textual). Observer-first: reads over HTTP, acts via
subprocesses (qlab CLI / headless `claude -p`) - never a second mutation path.
"""
from __future__ import annotations

import shutil
import subprocess
import threading

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, RichLog, Static

GOVERNED_PROMPT = (
    "Run one governed rebalance using the qlab MCP tools: moments-analyst "
    "judgment (moments.estimate), challenger check, objective.build, "
    "solve.classical, registry.log_decision, referee review then "
    "registry.log_verdict, propose_rebalance, execute_plan. Stop and report "
    "if any gate fails.")


def gather_snapshot(client) -> dict:
    return {
        "portfolio": client.get("/api/portfolio"),
        "decisions": client.get("/api/decisions").get("decisions", []),
        "runs": client.get("/api/runs").get("runs", []),
        "events": client.get("/api/events", limit=50).get("events", []),
    }


class QlabTui(App):
    CSS = """
    #portfolio { height: 9; border: solid $accent; padding: 0 1; }
    #decisions, #runs { height: 1fr; border: solid $primary; }
    #events { height: 12; border: solid $secondary; }
    """
    BINDINGS = [
        ("r", "rebalance_dry", "Rebalance (dry)"),
        ("R", "rebalance_live", "Rebalance (exec)"),
        ("d", "daily_ops", "Daily ops"),
        ("b", "batch", "Batch ablation"),
        ("g", "governed", "Governed (claude)"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, client, **kw):
        super().__init__(**kw)
        self.client = client

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield Static(id="portfolio")
            with Horizontal():
                yield DataTable(id="decisions")
                yield DataTable(id="runs")
            yield RichLog(id="events", markup=False, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#decisions", DataTable).add_columns(
            "as_of", "kind", "choice", "reflection")
        self.query_one("#runs", DataTable).add_columns("run_id", "kind", "created")
        self.refresh_data()
        self.set_interval(2.0, self.refresh_data)

    def refresh_data(self) -> None:
        try:
            snap = gather_snapshot(self.client)
        except Exception as exc:
            self.query_one("#events", RichLog).write(f"[api error] {exc!r}")
            return
        p = snap["portfolio"]
        self.query_one("#portfolio", Static).update(
            f"equity {p['equity']:.2f}  cash {p['cash']:.2f}  "
            f"dd {p['drawdown']:.2%} (kill @ {p['kill_switch_at']:.0%})  "
            f"halted={p['halted']}  broker={p['broker']}\n"
            f"weights: " + "  ".join(f"{t}:{w:.1%}"
                                     for t, w in sorted(p["weights"].items())))
        dt = self.query_one("#decisions", DataTable)
        dt.clear()
        for d in snap["decisions"][:20]:
            dt.add_row(str(d.get("as_of")), str(d.get("kind")),
                       str(d.get("choice", {}).get("regime", ""))[:24],
                       (d.get("reflection") or "-")[:60])
        rt = self.query_one("#runs", DataTable)
        rt.clear()
        for r in snap["runs"][:20]:
            rt.add_row(r["run_id"][:10], r["kind"], r["created_at"][:19])
        ev = self.query_one("#events", RichLog)
        for e in reversed(snap["events"][:8]):
            ev.write(f"{e['ts'][11:19]}  {e['kind']}  {e['payload']}")

    # -- actions: subprocesses only ------------------------------------------
    def _spawn(self, argv: list[str], label: str) -> None:
        log = self.query_one("#events", RichLog)
        log.write(f"[launch] {label}: {' '.join(argv)}")

        def run() -> None:
            try:
                out = subprocess.run(argv, capture_output=True, text=True,
                                     timeout=1800)
                tail = (out.stdout or out.stderr).strip().splitlines()[-8:]
                self.call_from_thread(
                    lambda: [log.write(f"[{label}] {line}") for line in tail])
            except Exception as exc:
                self.call_from_thread(lambda: log.write(f"[{label}] {exc!r}"))

        threading.Thread(target=run, daemon=True).start()

    def action_rebalance_dry(self) -> None:
        self._spawn(["qlab", "run-once", "--offline", "--dry-run"], "rebalance-dry")

    def action_rebalance_live(self) -> None:
        self._spawn(["qlab", "run-once", "--offline"], "rebalance")

    def action_daily_ops(self) -> None:
        self._spawn(["qlab", "daily-ops", "--offline"], "daily-ops")

    def action_batch(self) -> None:
        self._spawn(["qlab", "batch", "configs/specs/ablation_v1.yaml",
                     "--offline", "--no-qaoa"], "batch")

    def action_governed(self) -> None:
        if not shutil.which("claude"):
            self.query_one("#events", RichLog).write(
                "[governed] claude CLI not found on PATH")
            return
        self._spawn(["claude", "-p", GOVERNED_PROMPT], "governed")
```

- [ ] **Step 5: CLI entry** — in `qlab/autopilot/cli.py` add:

```python
def _cmd_tui(args) -> int:
    import socket
    import subprocess
    import sys
    import time
    from qlab.tui.app import QlabTui
    from qlab.tui.client import ApiClient

    def port_open() -> bool:
        with socket.socket() as s:
            return s.connect_ex(("127.0.0.1", args.port)) == 0

    if not port_open():
        srv_args = [sys.executable, "-m", "qlab.autopilot.cli", "ui",
                    "--port", str(args.port), "--no-browser"]
        if args.online:
            srv_args.append("--online")
        subprocess.Popen(srv_args)
        for _ in range(50):
            if port_open():
                break
            time.sleep(0.2)
    QlabTui(client=ApiClient(f"http://127.0.0.1:{args.port}")).run()
    return 0
```

and the parser entry:

```python
    tui = sub.add_parser("tui", help="operator console (observes the UI server API)")
    tui.add_argument("--port", type=int, default=8765)
    tui.add_argument("--online", action="store_true")
    tui.set_defaults(func=_cmd_tui)
```

- [ ] **Step 6: Run tests** (`pytest tests/test_tui.py -q`), then manual smoke: `qlab tui` in a real terminal — panes populate, `r` runs a dry rebalance and its tail appears in the events pane. Full suite.

- [ ] **Step 7: Commit** — `git commit -am "feat(tui): Textual operator console - observe via HTTP, act via subprocesses (R1)"`

---

### Task 6: July 31 package — real-data run + governed demo

**Files:**
- Modify: `README.md` (quickstart: single MCP server, `qlab tui`, governed-rebalance demo)
- Test: none new (verification gate)

- [ ] **Step 1:** `qlab prewarm --universe core` and `qlab prewarm --universe candidates` (online).
- [ ] **Step 2:** `qlab batch configs/specs/ablation_v1.yaml` (online). Record in the run log: B4 and A3t present; every arm has `sharpe_ci`; DSR non-degenerate; A3 vs A1 weight divergence from the diagnostics.
- [ ] **Step 3:** Governed demo end-to-end: `qlab tui` in one terminal → press `g` (requires this repo's `.mcp.json`; the headless Claude session drives moments-analyst → challenger → referee `log_verdict` → `propose_rebalance` → `execute_plan`) → watch the verdict, plan, and fills land in the decisions/events panes. If `claude -p` MCP wiring needs the project dir, run from repo root.
- [ ] **Step 4:** Update `README.md` quickstart (three commands: `pip install -e '.[tui]'`, `qlab prewarm`, `qlab tui`) and the governance section (referee gate now code-enforced; single MCP server).
- [ ] **Step 5:** Full suite; commit `git commit -am "docs: July package - quickstart + governed demo (R1)"`; record the demo (screen capture) for the submission.
