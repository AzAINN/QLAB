# The quantum predictor surface — design

**Date:** 2026-07-31
**Status:** design, awaiting operator review. No code written.
**Branch:** `worktree-quantum-predictor-surface` (worktree, based on main `8febbea`)

## What this is

Give Atlas its own forward-looking predictors — linear-regression-family and
kernel models over the quantum-inspired feature maps, evaluated on live ETF
data — surfaced as read-only advisory evidence in `atlas_context`, the MCP
tool surface, and both TUIs. Nothing here widens what Atlas can execute.

This is a revival, not a greenfield. The repo already holds:

- `qlab/research/prediction.py` — purged walk-forward ridge vol forecaster
  (`predict_vol_ridge`), 21-day embargo, inner-fold alpha, IC admission gate
  (IC > 0.03 AND stability > 0.5), wired as owner-only tool
  `research.predict_vol` and rendered in both TUIs.
- `qlab/research/quantum_features.py` — angle/ZZ feature maps, pure numpy,
  stage research, default off.
- `planning-docs/2026-07-30-ml-lane.md` — the measured negative result: with a
  single-alpha ridge, ZZ augmentation *hurts* (0/12 wins, paired t −4.53).
  Diagnosis: variance inflation — 6→48 near-collinear columns against a few
  hundred rows, and one global alpha over-shrinks the raw columns too. The doc
  ranks the rescue paths: (1) group-wise ridge penalties, (2) a kernel
  formulation ("the ZZ map's natural home is a kernel, where the feature count
  never materialises"), (3) fewer story-backed pairs. `TODO.md` repeats this.

This design builds exactly those two rescue paths, evaluates them honestly
against the existing baseline on identical purged folds, and surfaces the
result — including a negative one — to Atlas and the operator.

## Approaches considered

**A. Revive-and-surface (chosen).** Extend the existing prediction lane with
group-wise ridge and closed-form quantum-kernel ridge; add a paired
champion/challenger "predictor board"; persist it as a `runs` row; surface it
through one new owner-only MCP tool, an `atlas_context` section, and a card in
each TUI. No new tables, no new deps, no new process.

**B. New predictors subsystem.** A `qlab/predictors/` package with its own
registry tables (`predictor_models`, `predictor_predictions`,
`predictor_evals`) and a scheduled owner refresh job. Cleaner long-term home,
but it duplicates machinery the lane already owns (CV harness, admission,
`runs` persistence), adds schema and a thread for needs nobody has yet, and
maximises collision surface with the in-flight worktrees. Rejected for now;
the board's run-spec shape is designed so a later migration to tables is
mechanical.

**C. Surface-only.** Expose the existing `predict_vol_ridge(augmentation=...)`
knob to Atlas without new models. Cheapest, but every non-default setting of
that knob is measured-to-hurt under the current estimator — we would be
surfacing a control whose entire range is known-bad. Rejected.

## The design

### 1. Models — `qlab/research/prediction.py` + new `qlab/research/kernels.py`

All pure numpy (core deps stay at six packages; no sklearn — repo precedent).
All reuse the existing harness: `build_vol_prediction_frame`,
`purged_walk_forward_splits` (21-day embargo), per-fold standardisation, and
`scale_to_unit` bounds fitted on train and passed to test.

**Group-wise ridge** (rescue path 1): `(XᵀX + Λ)β = Xᵀy` with `Λ` diagonal,
one alpha for the raw-feature group and one for the augmentation group, chosen
by inner purged CV over a small 2-D grid (extends `_choose_alpha`). Reduces
exactly to plain ridge when both alphas coincide — that identity is a test.

**Quantum-kernel ridge** (rescue path 2): hand-rolled kernel ridge
`(K + αI)c = y`. The kernels are the closed-form inner products of the
existing maps, so the O(n²)-column ZZ design matrix never materialises:

- angle: `k(x,z) = Σᵢ cos(θᵢ − ζᵢ)` where `θ = (π/2)·x̂`
- zz: `k(x,z) = Σ_{i≠j} cos(aᵢⱼ − bᵢⱼ)` with `aᵢⱼ = (π−θᵢ)(π−θⱼ)`
- combined kernel `k = w_raw·(x·z) + w_map·k_map(x,z)` — the kernel analogue
  of "every augmentation keeps the raw features", and the weights `(w_raw,
  w_map)` are the kernel answer to the diagnosed failure: raw and augmented
  parts shrink separately. Weights + alpha from a small inner-CV grid.

Property test in the compiler-agreement style: each closed-form kernel equals
the explicit `augment()` feature inner product to 1e-10. Gram at ≤ ~1900
observations is ~29 MB and a one-second solve; `augmented_width` cost logic
becomes irrelevant by construction.

Fail loud on non-finite input; no fitted state on modules; bounds travel with
the data.

### 2. The predictor board — paired champion/challenger evaluation

`run_predictor_board(panel, models=DEFAULT_BOARD)` evaluates every model on
**identical** outer folds (paired, per the ml-lane methodology):

- Fixed baseline: `ridge / none` (the existing forecaster).
- Default board: baseline; group-wise ridge × {angle, zz, angle_zz}; kernel
  ridge × {linear-sanity, angle, zz}.
- Per model: `mean_ic, ic_std, ic_stability, per_fold[], wins_vs_baseline,
  paired_t_vs_baseline, chosen_hyperparams, usable` (the existing admission
  gate, unchanged thresholds, read off the payload by consumers — never
  hard-coded downstream).
- Deterministic ranking; deterministic under a fixed seed offline (the
  seed-keyed synthetic cache guard from the ml-lane fix already protects the
  sweep).

Persisted as one `registry.log_run("predictor_board", spec)` with
`dsr_trial_counted: False` (the `window_evidence` precedent: forecaster
research must not enlarge the deflated-Sharpe trial universe). No new tables;
`runs.spec` JSON is the established shape both TUIs already read.

### 3. Live ETF data

The board tool takes `(as_of="", universe="core", lookback_days)` and builds a
point-in-time `market.snapshot` under the owner's offline flag — provider
daily bars (yfinance/Alpaca) when online, seeded synthetic offline. The
look-ahead tripwire in `DataSnapshot.__post_init__` and `check_as_of` apply
unchanged. "Live" here means the same provider-backed daily-bar lane the rest
of the desk uses; the unwired websocket tape stays unwired.

### 4. MCP wiring (one new tool)

`research.predictor_board`, following the `research.predict_vol` pattern
exactly:

1. Register in `register_lab_tools` under `if owner_only:` (executes a
   research-stage model → owner/proxy path only, absent from the headless
   combined server), with `st.budget.charge`.
2. Add to `OWNER_LAB_TOOLS` (`qlab/ui/server.py:154`).
3. Proxy wrapper `research_predictor_board` in `qlab/mcp/tui_proxy.py` via
   `lab(...)`.
4. Add the dotted base to `_LAB_TOOL_BASES` (`qlab/tui/claude.py:60`).

### 5. Atlas consumption

- **Tool grant:** add `mcp__qlab__research.predictor_board` (dotted, matching
  peer agent files) to `agents/atlas.md`; run `python -m qlab.agents.loader
  sync`; commit the regenerated adapters (byte-compared by
  `tests/test_agents.py:207`). The pre-existing underscored-regime-tool
  anomaly in `agents/atlas.md` is noted but deliberately left alone here.
- **Context section:** `atlas_context()["predictors"]` — the newest
  `predictor_board` run summarised: `{as_of, source, age_days, champion:
  {model, augmentation, mean_ic, ic_stability, usable}, baseline: {...},
  delta_vs_baseline, admitted_any}` — or an explicit `{"status": "never_ran"}`
  (absent facts are named, not omitted). `age_days` is a number; whether it is
  *too old* is the reasoner's judgment, not the server's.
- **The gate is untouched.** `atlas_facts` stays nine keys
  (`test_the_gate_input_stays_narrow` continues to pass); `check_startable`,
  templates, budgets, and every execution invariant are out of scope by
  construction. Predictors inform what Atlas thinks, never what it can start.

### 6. Display — both clients, read-only

- **Textual** (`qlab/tui/app.py`): extend `_render_research` to pick the
  newest `kind == "predictor_board"` run and render the model table with
  admission per row, next to the existing vol-forecast line.
- **Ratatui** (`clients/atlas-tui`): new read-only card in
  `src/ui/views/research.rs` following `draw_forecast` — Option-typed structs
  in `model.rs` (a defaulted 0.0 is a claim nobody made), a `Store` accessor,
  three-state rendering (never-ran / unreadable-spec / table), `refuse()`
  below the width floor, fixture rows in `tests/fixtures/tui_snapshot.json`,
  a golden test, and the model round-trip test. No server change needed —
  both clients read `runs` from `/api/tui` already.

### 7. Measurement — the deliverable either way

Re-run the ml-lane protocol on the new board: 12 synthetic seeds × 5 paired
outer folds, offline, plus one online run per provider availability. Record a
dated planning-doc with the table, whichever way it goes:

- If a challenger beats baseline with admission passed → it becomes the
  board's champion; still research-stage, still advisory.
- If the kernels also fail → the doc says so, the board surfaces
  `admitted_any: false`, and Atlas sees an honest "no admitted predictor".
  A negative result is a deliverable (invariant 11); an implausibly good one
  is a bug until shown otherwise.

### 8. Tests

- Kernel/feature-map agreement to 1e-10; Gram symmetry/PSD; degenerate inputs
  fail loud.
- Group-wise ridge reduces to plain ridge under equal alphas; inner CV never
  sees outside its outer fold's purged history.
- Board: folds identical across models; baseline always present; deterministic
  under seed; admission propagated; empty model list refused.
- MCP: dual-namespace registration and owner-only asymmetry
  (`test_mcp_server.py` patterns); `OWNER_LAB_TOOLS` membership.
- Agents: adapters in sync; no execution-verb tools granted.
- UI: `atlas_context` carries the predictors section; never-ran shape;
  gate-stays-narrow untouched; `Registry(":memory:")` throughout, fully
  offline.
- Rust: golden for the new card; round-trip; operator-gate unaffected.

## Invariants respected, explicitly

1. One DuckDB writer — everything reaches the registry through the owner
   (`log_run` inside the owner-registered tool).
2. Tests in-memory and offline.
3. No execution path is created or widened; no `creates_plan` template.
4. Fail loud — non-finite refuses, never-ran is named.
7. Qiskit quantum stays offline. These are the *quantum-inspired* pure-numpy
   maps already staged as research; the real-circuit lane and its promotion
   path (evidence, catalog stage change, tool-authority review, tests) are
   untouched.
10. Every new seam ships with its caller: tool → proxy → grant → context →
    two rendered cards → tests.
11. The measurement doc is written whether the result is positive or negative.

## Coexistence with in-flight work

- `feat/atlas-reasoner` == main (`8febbea`, merged); the `atlas_context`
  seam from `cfa5240` is extended additively, its tests untouched.
- `feat/fast-research-foundation` adds only `qlab/research/fast_mode.py` +
  tests — no file overlap.
- `granite-model-picker`, `alpaca-oauth-desk-mode` — no overlap.
- The Ratatui client merged in PR #18 is in soak; the new card is additive
  and golden-pinned.

## Out of scope (deliberately)

Return prediction (roadmap: risk quantities first; returns last and
DSR-accounted), real-circuit quantum kernels (offline lane), scheduled or
heartbeat-driven board refresh (on-demand only for v1), new registry tables,
sklearn or tree models, any change to `atlas_facts`, templates, approvals, or
execution.

## Assumptions taken (flag any you want changed)

1. **Target stays 21-day realized vol** (`target_vol_21`) — per roadmap §7.
2. **Atlas gets both** the compute tool (budget-charged, advisory) and the
   persisted-board context section — not just one.
3. **Model set** as in §2; no gradient boosting.
4. Branch can be renamed to `feat/quantum-predictor-surface` at PR time.
