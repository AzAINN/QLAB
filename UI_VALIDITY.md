# UI Validity Analysis

An honest, tab-by-tab assessment of **what the numbers in the `qlab ui` app
actually mean** — what is rigorous, what is a simulation artifact, and what is a
data-independent fact. Read this before quoting any figure from the UI.

## The one distinction that governs everything: data source vs. machinery

The UI runs the *same code* in two modes:

- **Offline (default, `qlab ui`)** — prices come from a **deterministic synthetic
  generator** ([`qlab/core/data.py:synthetic_prices`](qlab/core/data.py)). It is
  seeded, reproducible, and deliberately built with a factor structure, a
  calm/stress regime, Student-t shocks, and asymmetric downside jumps so that
  correlations flip sign and the return distribution has **genuine negative skew
  and fat tails** — i.e. enough structure for the higher-moment machinery to have
  something real to chew on. **But every return, Sortino, drawdown and weight it
  produces is a property of that generator, not of any real market.**
- **Online (`qlab ui --online`)** — the identical pipeline runs on **live
  yfinance** adjusted-close data. Same optimizers, same backtest, same mandate.

**So the split is:**

| Category | Depends on data? | Validity |
|---|---|---|
| The **machinery** (optimizers, estimators, backtest, mandate, QAOA) | No — it is correct either way | **Rigorous / correct implementation** |
| The **market numbers** (returns, Sortino, drawdown, specific weights) | Yes | **Real only with `--online`; otherwise a reproducible simulation** |
| **Data-independent facts** (QUBO resource count, QAOA optimality gap) | No | **Fully rigorous regardless of data** |

Three things in the app are **rigorous no matter what data feeds it**, and are the
figures you can quote without qualification:

1. **The QUBO resource count** (Quantum tab): exact combinatorics.
2. **The QAOA optimality gap** (Quantum tab): measured against the *exact*
   ground state, which is enumerable at ≤ 21 qubits.
3. **The paper-book accounting and mandate enforcement**: deterministic and
   exact (equity is conserved on deployment to floating-point; every mandate
   limit is checked in code).

---

## Tab-by-tab

### Dashboard
**Shows:** equity, cash, drawdown, kill-switch headroom, positions, target weights.

- **Method:** `broker.portfolio_state()` marks each held position at the latest
  available price and sums to equity; drawdown is measured against a running
  high-water mark; the kill-switch distance is `mandate.trailing_drawdown_pct − drawdown`.
- **Valid & exact:** the accounting. `cash + Σ(qty·price) = equity`, conserved at
  deployment net only of the modeled cost drag; fills update cash and positions
  by double entry; the kill-switch arithmetic is correct.
- **Simulation artifact (offline):** the *price marks* are synthetic, so equity
  movements reflect the synthetic path, not real P&L. Positions/weights are real
  bookkeeping; their dollar value is only as real as the marks.
- **Quote-able:** "the book is internally consistent and the mandate is enforced."
  **Not quote-able:** "the strategy made X% " (unless `--online`, and even then
  see the small-sample caveats under Experiment).

### Recommend
**Shows:** MVSK champion weights, estimator diagnostics, classical-vs-quantum compare.

- **Method:** `estimate_moments` (Ledoit–Wolf covariance shrinkage → Marchenko–
  Pastur eigenvalue denoising → structured coskew/cokurt shrinkage toward the
  Gaussian tensor) → `build_objective("mvsk")` → `classical_multistart` SLSQP
  under the mandate box constraints. The comparison solves min-variance
  classically and discretized-MV via QAOA **on the same covariance**.
- **Valid & correct:** the optimization is a faithful implementation of the
  **risk-only MVSK** objective (minimize variance − λ₃·coskew + λ₄·cokurt, no
  expected-return term). The scipy objective is **property-tested** against a
  brute-force polynomial ([`tests/test_objective.py`](tests/test_objective.py)),
  so the classical and quantum arms provably optimize the *same* function and
  their objective values are directly comparable. The diagnostics (shrinkage
  intensity, condition number, avg correlation) are the estimator's real outputs.
- **Simulation artifact (offline):** the *specific weights* reflect the synthetic
  co-moments. The result is a **methodological demonstration**, not a live
  investment recommendation. Return forecasting is deliberately out of scope, so
  this is a risk-shape recommendation only.
- **Note:** the QAOA arm (when toggled on) genuinely runs on the Aer simulator
  and can take ~30–45 s for 21 qubits — that latency is real computation, not a
  hang.

### Autopilot
**Shows:** one pipeline iteration — regime, targets, trade outcome, equity change.

- **Method:** analyze regime → solve the champion under the mandate → `build_plan`
  (mandate-checked, two-phase) → `execute_plan` (idempotent) → `log_decision`.
- **Valid & correct:** mandate enforcement is deterministic — universe whitelist,
  per-asset cap, turnover cap (with the first-deployment exemption), daily order
  cap, and the trailing-drawdown kill-switch are all checked in code before a
  plan can reach `checked`. Execution is idempotent
  (`client_order_id = hash(plan_id, leg)`). Regime detection is an honest
  realized-vol threshold with **no return prediction**.
- **Simulation artifact (offline):** the regime call and targets are computed on
  synthetic data; trades are paper only and never touch a real venue.
- **Quote-able:** "the loop respects its mandate, is idempotent, and logs its
  reasoning." That is a property of the code, true in both modes.

### Experiment
**Shows:** the ablation ranking table + the Q-C architecture card.

- **Method:** `run_ablation` walk-forward-backtests each arm (point-in-time
  snapshots, no look-ahead, weight drift between rebalances, transaction costs),
  ranks by Sortino, content-hashes the run, and computes deflated Sharpe from the
  registry trial count.
- **Valid & correct:** the **backtest engine and metrics are correct** — the
  look-ahead tripwire is structural, the metric formulas (Sortino, max drawdown,
  CVaR, realized skew/kurtosis, deflated Sharpe) are standard, and the run is
  reproducible bit-for-bit.
- **⚠ The biggest caveat in the app:** on the **default offline synthetic data**,
  the *ranking itself* (e.g. "MVSK/A3 beats HRP") is a **property of the synthetic
  generator, not an empirical market finding.** The generator is built with real
  higher-moment structure so MVSK *can* win, but that is a demonstration that the
  pipeline detects and exploits the structure — **it is not evidence about real
  markets.** Additionally, the UI's *quick* spec is a short window (2016–2022, 6
  arms) — an even smaller sample than the full study. Real conclusions require
  `--online` data, the full [`configs/specs/ablation_v1.yaml`](configs/specs/ablation_v1.yaml)
  (2008–2024), and the stated small-sample discipline: ~70 quarterly points,
  deflated Sharpe, and block-bootstrap intervals rather than point estimates.
- **Bottom line:** treat the Experiment tab as **"does the machinery produce a
  sane, reproducible ablation?"** (yes) — not as **"is MVSK a better strategy?"**
  (unanswerable from a synthetic demo).

### Quantum
**Shows:** the 434-vs-7 resource count + a classical-vs-QAOA comparison.

- **Resource count — fully rigorous, data-independent.** `mvsk_qubo_resource_count`
  is exact combinatorics: `N = n·r` weight qubits, `C(n+2,3)` coskew and
  `C(n+3,4)` cokurt entries, `C(N,2)+N` auxiliaries/penalty gadgets. At n=7, r=4
  it returns **434 logical qubits, 406 penalty gadgets, vs 7 continuous
  variables** — pinned in [`tests/test_objective.py`](tests/test_objective.py).
  This is the single most rigorous number in the app and is true independent of
  any market data or hardware.
- **QAOA optimality gap — a genuine measurement.** The QAOA runs on the **Aer
  noiseless statevector simulator** and its energy is compared to the **exact
  ground state** (`NumPyMinimumEigensolver`), which is enumerable at ≤ 21 qubits.
  The reported gap (e.g. ~5–6% for discretized MV, ~0% for the selection QUBO) is
  therefore a **real, measured optimality gap**, not an estimate.
- **Caveats:** Aer is a *simulator* — device noise is **not** modeled. Running on
  real IBM hardware (set `IBM_QUANTUM_TOKEN`) would add a measurable
  simulator-vs-hardware gap. The 434-qubit figure is what gate hardware *would*
  require; it is **counted, not run** (that is the whole point).

### Registry
**Shows:** recent runs and the decision/reflection log.

- **Valid & correct:** an accurate provenance record. Runs are content-hashed
  (idempotent), solutions/backtests/decisions are persisted verbatim, and the
  trial count that feeds deflated Sharpe is a real column, not a guess.
- **Caveat:** the `reflection` field is populated by the reflection loop when a
  period resolves; in a short demo session it may be empty.

### About & Governance
**Shows:** the five-agent org chart with tool scopes, and the mandate limits.

- **Valid & correct:** parsed directly from the source of truth — `agents/*.md`
  (real least-privilege tool allowlists, checked in
  [`tests/test_agents.py`](tests/test_agents.py)) and `mandate.yaml`. It is an
  accurate description of the system's governance, not a marketing diagram.

---

## Summary: what you can and cannot claim

| Claim | Supported by the UI? |
|---|---|
| "The optimizers/estimators/backtest/mandate are correctly implemented." | ✅ Yes (property-tested; correct in both modes). |
| "Gate-model MVSK needs ~434 logical qubits vs 7 continuous variables." | ✅ Yes — exact count, data-independent. |
| "QAOA reaches within X% of the exact optimum on the simulator." | ✅ Yes — measured against the enumerable ground state. |
| "The paper book is consistent and the mandate is enforced." | ✅ Yes — deterministic accounting. |
| "MVSK beats HRP / this allocation is good." | ❌ Not from the offline demo — that number is a synthetic-generator property. Needs `--online`, the full spec, and small-sample discipline. |
| "The strategy earned N% / has Sharpe S." | ❌ Offline: a simulation artifact. Online: real but statistically underpowered on ~70 quarterly points — report intervals, not point estimates. |

**In one line:** the UI faithfully demonstrates that the *system works and its
quantum/architecture claims are measured facts; the market performance figures
are a reproducible simulation until you run it `--online`, and even then carry
the honest small-sample caveats the project states up front.*
