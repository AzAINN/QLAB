# Development Roadmap & Architecture Design — 2026-07-17

**Purpose:** decides the starting point for development and lays out the additive architecture, based on (a) the master brief (`research-plan.md`), (b) a full audit of the remote `barbara-feature` branch (`QuantAgentv1`, pushed 2026-07-16, 8,246 LOC), and (c) a literature/method sweep on news→optimizer integration and agent governance.
**Status:** DRAFT — awaiting owner approval before implementation planning.
**Deadlines in play:** July 31 entry (14 days out), August 31 full submission.

---

## 1. What exists on the remote branch — audit verdict

`origin/barbara-feature` is a genuine implementation of the research-plan architecture, not a shell. Verdict: **adopt it as the foundation** — but no number it produces can be trusted until five defects are repaired (§2).

### What is real and solid
- **Classical experiment arms all work**: B0 60/40, B1 equal-weight, B2 HRP (real López-de-Prado quasi-diag + recursive bisection), B3 ERC, A1 min-var with genuine Ledoit-Wolf 2004 closed form, A2 scenario-CVaR as a correct Rockafellar–Uryasev LP (`linprog/highs`), A3 MVSK multistart SLSQP.
- **Real Qiskit** (`qlab/solvers/quantum.py`): Q-A selection QUBO and Q-B discretized MV build genuine `QuadraticProgram`s, always solve exact via `NumPyMinimumEigensolver`, and additionally run real QAOA on `AerSimulator` (SamplerV2 + COBYLA) reporting a true optimality gap.
- **Moments layer**: real central co-moment tensors (n³/n⁴ einsum), Marchenko–Pastur denoising, Isserlis-target cokurtosis shrinkage, regime v1 (realized-vol threshold).
- **State**: genuine DuckDB registry, content-hashed specs, idempotent `ON CONFLICT DO NOTHING` run logging, plan state machine, Pydantic-enforced `Decision`.
- **Trader**: no raw order tool; two-phase propose→execute; mandate-as-code (whitelist, long-only, weight caps, turnover, order count, trailing-drawdown kill-switch); real `alpaca-py` paper adapter (env-gated, defaults to a DuckDB-booked simulator).
- **Agents**: five roles (moments-analyst, challenger, optimization-runner, referee, reporter) defined once in `agents/*.md` and *genuinely generated* into `.claude/agents/` and `.bob/personas/` by `qlab/agents/loader.py` — the orchestrator-agnostic invariant holds.
- **UI**: dependency-free SPA + threaded HTTP server wired to the real registry/broker/pipeline, with an unusually honest `UI_VALIDITY.md`.
- **Data**: yfinance with parquet cache + deterministic synthetic fallback; row-level look-ahead truncation enforced in `DataSnapshot.__post_init__`.

### What is broken, unwired, or missing
**Result-invalidating (fix before trusting any output):**
1. **MVSK λ scaling is unmanaged.** With daily-return tensors, the skew/kurt terms are 4–6 orders of magnitude below variance at λ=0.5 → A3/A4 collapse to min-variance. The headline "do higher moments help?" experiment is currently null by construction.
2. **The "one polynomial, three compilers, property-tested" invariant does not hold.** Three independent reconstructions (scipy einsum, `dirac3._mvsk_polynomial`, and a Qiskit "compiler" that is only a resource *counter*); the sole property test compares scipy against itself; no numeric cross-encoder agreement test exists.
3. **Deflated Sharpe is miscalibrated** (null benchmark omits the across-trial √V̂_SR term → DSR≈0 for all arms); `block_bootstrap_ci` has zero callers; registry `trial_count()` is never fed into DSR.
4. **Governance dynamics are prompt-only.** Referee PASS gate, challenger debate, and the reflection loop exist as prompts/schema columns; no code enforces or closes them. The autopilot `run_once` path goes solve→decide→trade with **no referee, no reconcile**.
5. **Per-leg idempotency is not real**: `apply_fill` isn't keyed on `client_order_id`; a re-executed plan double-applies cash/positions in the simulated broker (only the ledger row is deduped).

**Missing vs plan:** Q-C hybrid pipeline (QAOA selection → Dirac-3) doesn't exist (Q-A equal-weights its basket); the 434-qubit encoder is a combinatorial count, not an Ising construction; Dirac-3 silently falls back to classical without QCI creds and its encoder has no coefficient tests; async job pattern (`job_id`/poll) absent; ArtifactStore built but never wired; `check_constraints` guardrail is dead code; scheduling is a `while True: sleep()` loop, no market calendar; "parallel tempering" is mislabeled multistart; co-moment shrinkage is a fixed 0.5 scalar (no 1-factor target, not data-driven); HMM regime is a stub; PIT is row-truncation only (yfinance `auto_adjust` back-propagates today's split/dividend factors).

---

## 2. Starting point — Phase R0 "Trust Repair" (merge + 5 fixes)

**Step 0 — adopt the branch.** Merge `barbara-feature` into `main`, promoting `QuantAgentv1/*` to repo root (the nested directory becomes the package). Keep `planning-docs/` at root.

**Then fix, in order (est. 4–6 days total):**

| # | Fix | Where | Acceptance |
|---|---|---|---|
| R0.1 | **Standardize the MVSK objective.** Standardize returns (z-score by asset vol, or annualize + auto-scale λ₃/λ₄ by tensor norms, e.g. λ_k · ‖Σ‖/‖M_k‖) so each term contributes at comparable magnitude; sweep λ in the spec. | `core/objective.py`, `configs/specs/` | A3 weights measurably diverge from A1 on real data; term-contribution diagnostics logged per solve |
| R0.2 | **One polynomial source of truth.** Materialize the coefficient tensor once in `Objective`; make `compile_scipy`, the Dirac encoder, and a real Qiskit encoder consume *the same coefficients*; property tests assert numeric agreement (random w, small n) across all three; build the true MVSK→QUBO→Ising encoder (emitting the 434-qubit resource count from an actual Hamiltonian, completing Q-C's artifact). | `core/objective.py`, `solvers/dirac3.py`, `solvers/quantum.py`, `tests/` | Cross-compiler agreement test passes; count derived from constructed Ising terms |
| R0.3 | **Fix statistics.** Correct deflated-Sharpe null (Bailey–López de Prado √V̂_SR term); wire `Registry.trial_count()` into it; call `block_bootstrap_ci` in `run_ablation` and report intervals everywhere | `core/metrics.py`, `experiment.py` | DSR varies sensibly with trials; every metric ships with a CI |
| R0.4 | **Enforce governance in code.** (a) `execute_plan` requires a registry-recorded referee verdict (typed PASS enum, TradingAgents-style structured judge output) for the linked `decision_id`; (b) `run_once` calls `reconcile()` first and refuses on dirty ledger; (c) challenger invocation recorded into `Decision.challenger_view` in the rebalance path; (d) key `apply_fill` on `client_order_id` (idempotent replay test that executes twice) | `trader/plan.py`, `autopilot/loop.py`, `state/registry.py`, `mcp/quant_trader.py` | Autopilot cannot trade without PASS + clean reconcile; double-execution test passes |
| R0.5 | **Close the reflection loop.** Pending→resolved decision lifecycle: when a period resolves, compute realized outcome *of the judgment* (did the window/shrinkage/regime call pay?), write reflection via cheap LLM or deterministic scorer, inject recent reflections into moments-analyst context | `state/registry.py`, `autopilot/loop.py`, agents | A backtest run produces non-empty scored reflections; analyst prompt receives them |

R0 exit = the July-31 vertical slice is honest: agent-operated classical ablation on real data, referee-gated, statistically defensible.

---

## 3. Additive Track 1 — the Signals Layer (news → structured views → optimizer)

**Design change to the master brief.** The brief's news ban (§3 reject list) is *narrowed*, not repealed: news→trade-calls and news→return-forecasts stay banned. News enters only as **bounded, schema-typed views on risk moments** (vol, correlation, tail shape, regime), through a quarantined boundary, and the optimizer consumes them via re-estimated moment tensors. The LLM still never computes a number that isn't clamped, and never touches μ.

This is exactly the owner's ask ("news formulates the covariance matrix; news/market signals go as inputs to the optimizer") made injection-safe and falsifiable. It also lands on Fang's bottleneck #2 (estimation judgment) — news becomes *evidence for the judgment slot that already exists*.

### 3.1 Pipeline

```
[allowlisted feeds]──▶[quarantined LLM extractor]──▶[deterministic validator]──▶[view engine: SeqEP]──▶[moments]──▶[objective]──▶[all solvers]
[hard signals: VIX/OVX/GVZ/EVZ/SKEW (FRED), turbulence, absorption ratio,      ▲            (unchanged, incl. Dirac-3 & QAOA)
 HMM regime posterior, HY OAS / NFCI]──────────────────────────────────────────┘ corroboration + clamps
```

1. **Hard-signal block** (deterministic, injection-immune, free data): CBOE vol/skew indices via FRED (VIXCLS, OVXCLS, GVZCLS, EVZCLS, SKEW), turbulence index (Mahalanobis), absorption ratio (PCA variance share), HMM regime posterior (replaces the stub; hmmlearn, 2–3 states), credit spreads (BAMLH0A0HYM2), NFCI. New module `qlab/signals/`.
2. **Quarantined extractor** — a subagent with **no tools and no system state**, reading allowlisted feed text (Alpha Vantage NEWS_SENTIMENT free tier, GDELT GKG, EPU/GPR indices), emitting one strict Pydantic object: regime simplex over {calm, stress, crisis}; per-sleeve vol views (direction ∈ {−1,0,+1}, magnitude bucket ∈ {small, med, large}, confidence ∈ [0,1], evidence ids); block-level correlation views; tail views (skew direction / kurt-up). Bucketed enums, not free floats. Confidence floored by sampling dispersion (k≈10–20 repeated extractions). Rationale text goes to the audit log only — **no privileged agent ever re-ingests it** (dual-LLM quarantine pattern, per Willison / arXiv 2506.08837).
3. **Deterministic validator** (server code): schema check; hard clamps (vol tilt ≤ ±50% relative; LLM regime λ may deviate ≤ ±0.2 from the hard-signal λ; asymmetric correlation clamps — up-tilts looser than down-tilts); **corroboration haircut** (news stress flag without VIX/turbulence support → confidence cut); PSD projection of any resulting Σ; SHA-256 manifest of payload + source snapshots into the registry (run-manifest pattern from arXiv 2512.07867); signed numeric handoff.
4. **View engine — Sequential Entropy Pooling** (Meucci 2008; Vorobets SeqEP; `fortitudo.tech` BSD-3 package). Prior = historical scenario panel (the same panel A2's CVaR LP already consumes — one mechanism, two uses). Views expressed as constraints on posterior **variance/correlation/skew/kurt with means pinned to the prior** (the mathematically native "risk views only" device; SeqEP ordering prevents implicit mean drift). Posterior scenario probabilities → recompute μ(untouched)/Σ/M3/M4 → existing objective builder → **every solver downstream unchanged, including Dirac-3 and the QUBOs**. Fallback v1: λ-mixing Σ = (1−λ)Σ_calm + λΣ_crisis + Qian–Gorman (2001) conditional-covariance tilts for single-sleeve vol shocks.
5. **Influence caps ("view budget")**: cap KL(posterior‖prior) per rebalance (EP computes it natively; equivalently effective-sample-size floor ≥ 0.5); cap L1 weight deviation vs the no-view solution (e.g. ≤ 10%); one-switch kill back to no-view prior. Confidence ceilings per view type adapt to realized calibration.
6. **Calibration ledger** (TradingAgents reflection pattern, re-targeted from trade calls to views): every view logged pending with hash; resolved next rebalance against **realized** vol/corr/regime — risk views are *verifiable*, unlike return views; Brier/hit-rate per view type per regime (LLM-signal validity is regime-dependent — arXiv 2604.10996); 2–4 sentence reflections; standing A/B of views-on vs views-off vs a GARCH-conditioning baseline. **Deletion rule: if the LLM layer can't beat plain DCC-GARCH/turbulence conditioning out-of-sample, it goes.**

### 3.2 New experiment arms

| Arm | What | Tests |
|---|---|---|
| B4 | DCC-GARCH / turbulence-conditioned Σ (no LLM) | The econometric bar the news layer must beat |
| A3v / A4v | MVSK on entropy-pooled (news-conditioned) tensors, classical / Dirac-3 | Marginal value of views |
| Q-Av | Regime-conditioned selection QUBO (redundancy matrix per regime; regime flip triggers QAOA re-selection) | Selection layer becomes regime-aware |

### 3.3 Why this composes with quantum (the "best of both worlds" note)

Views only alter the *coefficients* (Σ, M3, M4) upstream of HUBO/QUBO construction — zero solver changes. The research sweep found **no published work** feeding news/regime-conditioned moments into a quantum MVSK formulation: "entropy-pooled, news-conditioned co-moment tensors → Dirac-3 HUBO, with QAOA regime-aware selection" is a legitimate novelty claim for the submission, on top of the 7-vs-434 headline.

### 3.4 MCP surface additions

`signals.compute_hard()` → signal_set_id; `views.extract()` (quarantined, returns view_id); `views.validate(view_id)` → clamped+signed view; `views.apply(moment_set_id, view_id)` → new moment_set_id + KL/ENS diagnostics. `moments-analyst` judges *how much* view weight by regime (a logged, challengeable decision); a new `news-quarantine` agent role is emitted like the other five with an empty tool scope.

---

## 4. Additive Track 2 — Quantum completion

1. **Q-C hybrid pipeline**: chain Q-A's selected basket into a Dirac-3 (or classical-fallback) MVSK solve — the submission's headline architecture (19 → k≈7 → weights). Currently missing entirely.
2. **True Ising encoder** for the 434-qubit artifact (R0.2 covers construction; here add the resource-count report generator + agreement with the combinatorial count).
3. **Dirac-3 live**: exercise `_mvsk_polynomial` against the real QCI SDK (creds pending — plan §11 Q5), coefficient tests first; loud, logged fallback instead of silent.
4. **Async jobs**: `job_id` + poll for `solve.quantum`, Dirac submissions, and backtests (plan invariant 5); enables Bob/Claude agents to run long jobs without blocking.
5. **Hardware budget pattern**: train QAOA on Aer, sample final circuit on IBM QPU inside a Runtime Session (10-min Open Plan budget), report sim-vs-hardware gap as a noise measurement.

## 5. Additive Track 3 — Governance & operations completion

1. Wire `check_constraints` at the solve-tool boundary (dead code today) — also validates quantum solver outputs, which published QAOA work shows violate practical constraints.
2. Wire the content-addressed ArtifactStore into registry writes (invariant 3's "hash in row, blob in store").
3. Market-calendar checks + real scheduling (cron/launchd invoking headless `run_once`/`daily_ops`; kill the `while True` loop for live use).
4. Daily-ops as an *enforced* persona (tool whitelist excluding `execute_plan`), not just structural omission; emit it via the agent loader.
5. Referee "planted flaw" drill as a test (plan M5 exit criterion); FinCon-style two-timescale risk control (within-period CVaR tripwire + across-period reflection) on the paper book.
6. PIT hardening: pin raw+adjusted price snapshots at fetch time; document the `auto_adjust` caveat honestly in the writeup.

---

## 6. Sequenced roadmap against the deadlines

**July 31 entry (14 days):**
- **Jul 17–18:** merge branch to main (promote to root), CI green, run suite on real yfinance data end-to-end.
- **Jul 19–24: R0 trust repair** (§2, five fixes — λ scaling first, it gates everything).
- **Jul 25–27:** hard-signal block + HMM regime + λ-mixing covariance v1 (no LLM extractor yet — deterministic only); B4 baseline arm; real-data classical ablation with CIs.
- **Jul 28–30:** July submission package: agent-operated ablation demo (Bob orchestration if access confirmed, Claude Code otherwise), referee drill, writeup + video. **Submit as the M0–M2 slice + signals-v1.**

**August 31 full system:**
- **Week 1 (Aug 1–8):** quarantined extractor + validator + SeqEP view engine (fortitudo.tech); A3v arm; calibration ledger.
- **Week 2 (Aug 9–15):** Q-C hybrid pipeline; Dirac-3 live (if creds); async jobs; IBM QPU sampling runs (Q-A/Q-B on hardware).
- **Week 3 (Aug 16–22):** trader hardening (reconcile-gated live paper loop, scheduling, shadow books for A1/A3/B0/B2); reflection + challenger demos; A4v.
- **Week 4 (Aug 23–31):** 15–19-ETF solver-claim stress run; sensitivity sweeps; freeze, report, video, submit.

**Unchanged blocking questions from the brief (§11):** IBM Bob access; eligibility; QCI credentials + Dirac-3 degree-4 variable ceiling; IBM Quantum plan/promo; options-chain access (BKM implied moments remain the best *additive* idea beyond this doc, and pair naturally with the view engine as shrinkage targets).

---

## 7. Decisions needing owner sign-off

1. **Adopt-and-merge `barbara-feature` to root** (recommended) vs cherry-pick rebuild.
2. **Signals-layer scope for July**: deterministic hard-signals + λ-mixing only (recommended) vs pushing the LLM extractor into July.
3. **Risk-views-only stance confirmed?** This doc pins means to the prior (no return views ever). The middle path — regime probabilities selecting among pre-estimated tensor sets — is the maximum "direction" allowed.
4. Branch owner ("Barbara") coordination: review/PR etiquette before merging their work.

## 8. Key references

- Meucci, *Fully Flexible Views* (2008), arXiv:1012.2848; Vorobets, Sequential Entropy Pooling; `fortitudo.tech` (BSD-3).
- Qian & Gorman (2001), *Conditional Distribution in Portfolio Theory* — closed-form vol/corr views.
- Idzorek (2004) — BL confidence; LLM-BLM, arXiv:2504.14345 (LLM return views ≈ regime-lucky style bets — supports the ban).
- arXiv:2512.07867 — LLM stress scenarios with hard plausibility gates, bounded λ covariance mixing, SHA-256 run manifests (closest blueprint).
- arXiv:2506.08837 + CaMeL (arXiv:2503.18813) — prompt-injection design patterns; dual-LLM quarantine.
- TradingAgents (TauricResearch) — pending→resolved reflection ledger, typed judge output, capped debate rounds.
- Manela–Moreira NVIX (JFE 2017); Bakshi–Kapadia–Madan (2003); Kritzman et al. turbulence/absorption ratio.
- arXiv:2604.10996 — regime-dependent LLM-signal validity → regime-conditional trust weights.
- No published news/regime-conditioned quantum MVSK found → novelty claim.

---

# Amendment A — 2026-07-17 (approved direction, narrowed build, TUI console)

## A.1 Approved decisions (owner sign-off)

1. Adopt-and-merge `barbara-feature` (promote `QuantAgentv1/` to repo root).
2. July signals scope = deterministic hard-signals + regime λ-mixing only; LLM extractor in August.
3. Risk-views-only confirmed (means pinned to prior, permanently).
4. **Claude Code is the operational orchestrator** for build + test; IBM Bob adapter exercised when access arrives (submission-time requirement).
5. Narrow-first build: high-quality working components (quant-lab + quant-trader + TUI vertical slice) before breadth.

## A.2 Quant algorithm recommendations (alpha / risk / beta)

**Stance:** in this architecture the product is *beta allocation across asset classes*; "alpha" is estimation alpha — better risk estimation, allocation methodology, and regime conditioning — never security selection or return forecasting.

| Tier | Method | Why / where it lands |
|---|---|---|
| 0 (in branch) | Ledoit-Wolf linear shrinkage, Marchenko-Pastur denoising, HRP, ERC, min-var, scenario-CVaR, MVSK | Keep; fix λ-scaling (R0.1) and co-moment shrinkage intensity |
| 1 (add now) | **Volatility-targeting overlay** (Moreira–Muir 2017, *Volatility-Managed Portfolios*) | The most robust documented risk-reduction device: scale exposure ∝ 1/σ̂²; mechanical, no return forecast; cuts drawdown/kurtosis; doubles as the mandate's pre-validated de-risking lever |
| 1 | **Nonlinear shrinkage** (Ledoit-Wolf 2020 analytical) | Strictly dominates linear LW; drop-in at `core/moments.py` |
| 1 | **HMM regime (2–3 states) + regime-conditional tensor sets** | Replaces the stub; the substrate for signals v1 λ-mixing |
| 1 | **Data-driven co-moment shrinkage** (Martellini–Ziemann 1-factor target) | Fixes the fixed-0.5 scalar; the plan's "hard technical task" |
| 2 (Aug) | **NCO** (López de Prado) as arm B5; **Graphical Lasso** precision → selection-QUBO prior; Sequential Entropy Pooling views (§3) | Additive arms + the QUBO cross-link |
| 3 (named, parked) | Time-series momentum / trend ("crisis alpha") — *is* return prediction → shadow arm at most; meta-labeling rebalance gate (underpowered at ~70 obs); BKM option-implied moments (data-gated); QAE demos | Roadmap slide, not July/August code |

## A.3 Textual TUI — operator console design

**Role:** the single pane of glass where the owner runs the orchestrating LLM and every component. **Observer-first invariant: the TUI is never a second mutation path** — every action shells out to the existing `qlab` CLI or spawns a headless Claude Code session (`claude -p` with the rebalance/daily-ops prompt); all state changes flow through the same MCP tools/CLI the agents use, and the TUI *watches* the registry.

**Panes:** (1) portfolio & mandate status (broker truth, drift bands, halt/kill-switch state); (2) decision feed (decisions + challenger views + reflections as they resolve); (3) runs/ablation table (arms × metrics with CIs); (4) jobs pane (async job rows: solver/backtest/quantum status — depends on R2 job queue); (5) `events` ticker; (6) command palette — `r` rebalance session (headless Claude), `d` daily-ops, `b` batch ablation, `q` quantum compare, `u` open web UI (judge demo).

**Two prerequisite fixes discovered in the branch:**
- **Events bus is write-only and sparse.** `Registry.record_event()` is called at 8 sites (plan checked/executed, halt/resume, mandate violation, autopilot runs, ablation complete) but there is **no read API** (no `read_events`, no UI/API route) and no emission at the MCP tool boundary. R0 adds `read_events` + a `/api/events` route + per-tool-call emission via the existing `CallBudget.charge` chokepoint — this is the TUI's data feed.
- **DuckDB single-writer conflict.** `.mcp.json` launches quant-lab and quant-trader as *separate processes*, each opening the same DuckDB file read-write; add the UI server and TUI and you have 3–4 processes contending for one exclusive write lock. **Fix in R1: one process owns the book.** Options: (a) merge both MCP servers into one process exposing both tool namespaces (simplest; role separation stays enforced by per-agent tool allowlists, which is where it actually lives); (b) route trader writes through the lab server. Recommend (a). TUI + web UI read via the owning process's HTTP API (the branch's `ui/server.py` single-lock pattern already does this) or read-only DuckDB connections.

## A.4 Narrowed phase layout (supersedes §6 dates)

| Phase | Dates | Contents | Exit |
|---|---|---|---|
| **R0 — merge + trust repair** | Jul 18–24 | Merge to root; five fixes (§2); wire `record_event` everywhere; data-driven co-moment shrinkage | Tests incl. cross-compiler + double-execution pass; real-data A3 ≠ A1 |
| **R1 — one honest loop + TUI** | Jul 25–30 | Consolidate DB ownership (A.3); Claude Code orchestrator drives moments-analyst → challenger → solve → referee PASS (code-enforced) → propose → execute (sim broker) → memo; hard-signals v1 + regime λ-mixing; vol-target overlay + nonlinear shrinkage; real-data ablation w/ CIs; **TUI v1** (panes 1/2/3/5 + palette); July 31 package | End-to-end governed rebalance visible live in the TUI; submitted |
| **R2** | Aug 1–8 | Async job queue + TUI jobs pane; quarantined extractor + validator + SeqEP; B4/B5 arms; calibration ledger | Views-on vs views-off vs GARCH A/B running |
| **R3** | Aug 9–15 | Q-C hybrid, true Ising encoder, Dirac-3 live (creds), QPU sessions | Measured 7-vs-434 artifact; hybrid pipeline runs |
| **R4** | Aug 16–22 | Alpaca paper live, scheduling + market calendar, shadow books, reflection/challenger demos | Unattended paper loop + decision feed |
| **R5** | Aug 23–31 | 15–19-ETF stress run, sweeps, freeze, report, video | Submitted |

## A.5 Branch gaps specific to the lab/trader/TUI slice (consolidated)

1. Events bus write-only (8 emit sites, no read API, no MCP-boundary emission) — TUI feed missing.
2. DuckDB multi-process write contention in `.mcp.json` topology — needs single-owner consolidation.
3. No async jobs → long solves block; TUI can't show progress (R2).
4. Referee gate + reconcile absent from the execution path (R0.4).
5. Per-leg fill idempotency broken (R0.4d).
6. ArtifactStore + `check_constraints` dead code (wire in R1/R2).
7. Daily-ops is not an emitted persona with an enforced no-`execute_plan` whitelist.
8. No market calendar / real scheduling (`while True: sleep`) (R4).
9. Co-moment shrinkage fixed at 0.5, no 1-factor target (R0).
10. HMM regime stub (R1).
