# qlab — Status & Delivery Map

> **Superseded 2026-07-19.** This file preserves an earlier delivery snapshot
> present at the 06f9596 continuation baseline; embedded commit and test markers
> below are historical. Quantum sections describe the former staged direction;
> QAOA and Ising construction analysis are now offline research only. Current
> status and the recovered code-review ledger live in
> [2026-07-19-continuation-ledger.md](2026-07-19-continuation-ledger.md).

**Date:** 2026-07-17 · **HEAD:** `0cdd0f5` · **Suite:** 118 passed, 1 skipped · **Deadlines:** July 31 (slice), August 31 (full system)

---

## 1. The end product

A **governed agentic quant research desk** that promotes its own validated policy into a mandated paper-trading autopilot — and can prove every step of that promotion.

> *TradingAgents puts the LLM where the alpha is. We put the LLM where the judgment is, and machines where the numbers are.*

The delivery chain the whole system exists to walk:

```
research question ─▶ honest ablation ─▶ champion policy ─▶ referee PASS ─▶ mandated autopilot ─▶ audited paper book
     (agents judge)   (machines count)   (registry proves)  (code enforces)   (human confirms)     (everything logged)
```

Three layers, hard boundaries: **agents** own judgment (windows, shrinkage, regime calls — logged, challenged, scored later), **solvers** own numbers (classical, QAOA, Dirac-3 — all compiled from one property-tested polynomial), **deterministic code** owns rigor (mandate, referee gate, look-ahead tripwires, trial counting). The Textual console is the face; one DuckDB registry is the memory; the same loop runs as backtest, live demo, and cron.

---

## 2. What is implemented (and how we know)

### Substrate — DONE
| Piece | Evidence |
|---|---|
| 7-ETF cross-asset core + 19-candidate pool, point-in-time snapshots (hard `as_of` truncation) | leak tests; provenance-tagged cache (`source=yfinance`, refuses synthetic-as-live) |
| Content-hashed DuckDB registry: runs, moments, objectives, solutions, backtests, decisions, verdicts, plans (+legs), orders, events | idempotent-run + round-trip tests |
| Moment estimation: Ledoit–Wolf, **LW2020 nonlinear analytical**, Marchenko–Pastur, co-moment tensors with **data-driven ("auto") shrinkage + one-factor target** | estimator tests incl. Frobenius dominance & delta-vs-T |
| One polynomial source of truth (`polynomial_terms`) compiled to scipy / Dirac-3 / binary-Ising, property-tested to 1e-10 | cross-compiler agreement tests |
| Walk-forward backtest with **cash-carry drift** (sub-1 weights stay de-levered) | half-invested vol-ratio test |

### Statistics that can be trusted — DONE
Deflated Sharpe with the Bailey–López de Prado null (√V̂·E[maxZ]), **cumulative registry trial counting** (benchmark- and research-arm-excluded), stationary block-bootstrap CIs on every arm. The MVSK λs auto-scale so higher moments actually participate; the ERC solver is scale-invariant (both were silent scale bugs — found, fixed, regression-locked).

### Governance — DONE (the differentiator)
- **Referee gate in code:** `execute_plan` refuses without a registry PASS **bound to the exact targets hash** (stale approvals cannot authorize different trades); monotonic latest-verdict ordering; deterministic referee runs in every autopilot cycle, LLM referee submits through the same tool.
- **Two-phase, leg-idempotent, transactional execution** with persisted legs — crash mid-rebalance resumes without double-ordering, cross-session.
- **Challenger** argues the alternate estimation window on every decision; **reflection loop** resolves pending decisions against realized vol/regime and feeds lessons back; **events bus** instruments every MCP tool call.
- **Mandate as code:** whitelist, long-only, caps, turnover, order count, trailing-drawdown kill-switch. No raw order tool exists anywhere.

### Quantum — MEASURED, honestly scoped
- Q-A selection QUBO + Q-B discretized MV run real QAOA on Aer with exact-ground-state optimality gaps.
- The **434-vs-7 headline is now a constructed artifact**: the MVSK→binary→Ising encoder builds the actual Hamiltonian and counts it (worst-case 434 logical qubits vs 7 Dirac-3 continuous variables).
- Dirac-3 adapter consumes the canonical polynomial; falls back loudly without QCI credentials.

### Signals layer v1 — DONE (deterministic, injection-immune)
Turbulence, absorption ratio, FRED vol indices (cache + offline fallback), composite regime λ; **regime-conditional covariance** powering arm B4; MVSK+conditioning fails loud until tensors are conditioned consistently.

### The console & orchestration — DONE
- **Quiet-workstation TUI**: spine/canvas/agent-rail, verdicts + reflections + challenger detail in the Audit view, `DATA source·age` provenance token, paper-confirm modal; strictly an HTTP observer of the single owner process.
- **Single `qlab` MCP server** (both tool namespaces, one DuckDB writer, owner-port guard) + **`qlab-operator` proxy** for governed propose-only Claude sessions (no execution tool exists to hijack).
- **Five subagents** generated from one neutral source into Claude + Bob adapters, least-privilege verified (only the reporter can touch the trader; the referee can only judge).

### What today's real-data ablation says (2018–2026, quarterly, DSR over 9 trials)
| | sortino | vol | maxDD | DSR |
|---|---|---|---|---|
| 60/40 · HRP · **ERC** · 1/N | 0.86 / 0.82 / 0.80 / 0.74 | 12–7% | −22…−26% | 0.75–0.79 |
| B4 regime-cond · A2 CVaR · A1 min-var | 0.32–0.42 | 5–6% | −16…−20% | 0.37–0.48 |
| **A3t MVSK+vol-target** · A3/A4 MVSK | 0.10 / 0.03 | 7.4 / 8.9% | −27 / −32% | 0.24 / 0.20 |

**The honest finding:** benchmarks still win out-of-sample; MVSK does not yet pay for its complexity on this window. That is a *feature* of the submission — the lab is capable of falsifying its own thesis, which is exactly what makes a PASS from it mean something. Attacking this result is the August research program.

---

## 3. What needs to be implemented

### August research track (the thesis attack)
1. **News → structured views → optimizer** — the approved centerpiece: quarantined LLM extractor → typed, clamped risk-moment views (means pinned) → **Sequential Entropy Pooling** reweights the scenario panel → conditioned Σ/M3/M4 flow into the *unchanged* solver stack. KL view budget, corroboration haircuts vs hard signals, calibration ledger scoring views against realized vol. Nobody has published news-conditioned moments feeding a quantum MVSK formulation — a legitimate novelty claim.
2. **λ-sweep + estimator study** — why MVSK loses: sweep the auto-scaled λs, comoment targets, windows; DSR-honest reporting either rescues the objective claim or retires it with evidence.
3. **Quantum completion** — Q-C hybrid pipeline (QAOA selection → Dirac-3 weights), live Dirac-3 runs (QCI credentials), QPU sampling inside Runtime sessions (10-min budget pattern), async job queue.
4. **Solver claim at scale** — the 15–19-ETF stress run where classical multistart genuinely struggles.

### August operations track (the desk goes live-paper)
5. **Alpaca paper live** + shadow-booked challenger arms marked to the same prints.
6. **Real scheduling** — cron/launchd headless sessions, market-calendar checks (replace `watch`'s sleep loop).
7. **IBM Bob orchestration** — adapters already generated; exercise them when access lands (required for the submission).

### Fast-follows (18 triaged, ledger `.superpowers/sdd/progress.md`)
Top of the list: no-op plan resume false-refusal · CLI DuckDB-lock guidance + TOCTOU on the owner guard · long-lived owner staleness (build stamp in `/api/system`) · MCP `backtest.run` polluting DSR trials · trader-tool event emission · regime-λ window alignment. All fail-closed today; none block the July package.

### July 31 remainders (small)
Governed-demo screen capture · submission write-up/video · SkillsBuild activity + project page · push to GitHub when ready.

---

## 4. How it all delivers

**The demo is the delivery chain, live.** Open `qlab tui` → the desk shows real provenance-tagged data, the paper book, and the audit trail. Type `governed` → a propose-only Claude session runs moments-analyst → challenger → optimizer → referee, and you watch the verdict land in the Audit view before you — the only party with an execute button — confirm the paper trade. Every claim in the pitch is a thing the judge can click.

**The submission narrative writes itself from the architecture:**
- *Wildcard fit* — an intelligent system for the future of work: judgment scaffolded by agents, rigor enforced by machines, one auditable substrate for both.
- *Governance story* — Bob governs how the software is built; the referee governs how the research is trusted. Same principle, two layers.
- *Quantum story* — measured, not asserted: 7 continuous variables vs a constructed 434-qubit Hamiltonian, with QAOA optimality gaps as the reality check.
- *Honesty story* — the falsifying arm (CVaR), the benchmark that wins (HRP), the referee that can fail us, and a results table that currently says the benchmarks are winning.

**The end state (post-August):** a desk where news becomes bounded risk views, views become conditioned tensors, tensors become quantum-and-classically-solved weights, weights survive a code-enforced referee, and a mandated autopilot trades them on paper — every judgment logged, challenged, scored, and reflected back into the next decision. The lab that can prove itself wrong, promoting only what survives.
