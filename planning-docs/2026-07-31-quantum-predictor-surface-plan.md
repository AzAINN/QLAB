# Quantum Predictor Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the two diagnosed rescue paths for the quantum-inspired ML lane (group-wise ridge, closed-form quantum-kernel ridge), evaluate them paired against the existing baseline on identical purged folds, and surface the resulting predictor board to Atlas and both TUIs as read-only advisory evidence.

**Architecture:** Pure-numpy models extend `qlab/research/` (no new deps); a board module runs all models on shared folds and is persisted as one `runs` row through the owner; one new owner-only MCP tool + an `atlas_context` section + a card in each client. Spec: `planning-docs/2026-07-31-quantum-predictor-surface-design.md`.

**Tech Stack:** numpy/pandas (existing core), FastMCP registration pattern, DuckDB via owner only, Textual, Rust ratatui + insta.

## Global Constraints

- No new Python dependencies; no sklearn. Core deps stay the six in `pyproject.toml:24-31`.
- Tests: `Registry(":memory:")` only; fully offline; run with `/Users/azainmac/codebases/quant-trading-agent/.venv/bin/python -m pytest` from the worktree root.
- Never weaken an existing assertion; property agreement tolerance 1e-10.
- `atlas_facts` must not gain a key (`test_the_gate_input_stays_narrow` must pass untouched).
- Commit style: conventional prefix + scope, imperative, no AI-attribution trailers.
- Board runs are `dsr_trial_counted: False` and never write `backtests`.
- After editing `agents/*.md`: `python -m qlab.agents.loader sync`, commit regenerated adapters.

---

### Task 1: Closed-form quantum kernels (`qlab/research/kernels.py`)

**Files:**
- Create: `qlab/research/kernels.py`
- Test: `tests/test_kernels.py`

**Interfaces (Produces):**
- `angle_kernel(a_unit, b_unit) -> np.ndarray` — Gram of `angle_map` features: `cos(Θa)@cos(Θb).T + sin(Θa)@sin(Θb).T`, `Θ = unit * π/2`.
- `zz_kernel(a_unit, b_unit) -> np.ndarray` — Gram of `zz_map` features via the pairwise-phase matrix `P[r, (i,j)] = (π−θi)(π−θj)` for `i<j`: `cos(Pa)@cos(Pb).T + sin(Pa)@sin(Pb).T`. Zero matrix for n<2 features.
- `quantum_gram(a_std, b_std, a_unit, b_unit, kind, *, w_raw=1.0, w_map=1.0) -> np.ndarray` — `w_raw*(a_std@b_std.T) + w_map*k_kind(a_unit, b_unit)`; `kind in ("linear","angle","zz")`, `linear` ignores the map term. Unknown kind → ValueError listing available.
- `kernel_ridge_predict(k_train, train_y, k_cross, alpha) -> np.ndarray` — center y, solve `(K+αI)c = y_c` (pinv fallback), return `mean_y + k_cross@c`.
- All entry points validate 2-D finite input (reuse the `_as_matrix` fail-loud pattern).

**Steps:**
- [ ] Write failing tests in `tests/test_kernels.py`:
  - `test_angle_kernel_agrees_with_the_explicit_feature_map` — `angle_kernel(Ua,Ub)` vs `angle_map(Ua)@angle_map(Ub).T`, atol 1e-10.
  - `test_zz_kernel_agrees_with_the_explicit_feature_map` — same vs `zz_map`.
  - `test_zz_kernel_is_zero_width_for_a_single_feature` — n=1 → zeros.
  - `test_quantum_gram_with_unit_weights_agrees_with_stacked_features` — vs inner product of `[a_std | map(a_unit)]`.
  - `test_the_gram_is_symmetric_and_psd_on_itself` — eigvalsh ≥ −1e-8.
  - `test_kernel_ridge_equals_primal_ridge_for_the_linear_kernel` — vs `_ridge_predict` on the same standardized matrix, atol 1e-8.
  - `test_non_finite_input_fails_loud`, `test_an_unknown_kernel_is_refused`.
- [ ] Run: `pytest tests/test_kernels.py -q` → FAIL (module missing).
- [ ] Implement `qlab/research/kernels.py` (module docstring states the narrow claim + pointer to the ml-lane doc, per lane convention).
- [ ] Run: `pytest tests/test_kernels.py tests/test_quantum_features.py -q` → PASS.
- [ ] Commit: `feat(research): closed-form angle and zz kernels with a kernel ridge solver`

### Task 2: Group-wise ridge (`qlab/research/prediction.py`)

**Files:**
- Modify: `qlab/research/prediction.py` (after `_choose_alpha`)
- Test: `tests/test_prediction.py` (append)

**Interfaces (Produces):**
- `_groupwise_ridge_predict(train_x, train_y, test_x, alpha_raw, alpha_map, n_raw) -> np.ndarray` — identical standardization/centering to `_ridge_predict`, penalty `np.diag([alpha_raw]*n_raw + [alpha_map]*(d-n_raw))`.
- `_choose_groupwise_alphas(train_x, train_y, alphas, n_raw) -> tuple[float, float]` — inner purged 3-fold CV (same machinery as `_choose_alpha`) over the `alphas × alphas` grid, deterministic tie-break preserving grid order; fallback middle pair when inner splits are infeasible.

**Steps:**
- [ ] Failing tests: `test_groupwise_ridge_with_equal_alphas_is_plain_ridge` (atol 1e-10 vs `_ridge_predict`), `test_groupwise_alpha_search_is_deterministic`, `test_groupwise_penalty_actually_differs_by_group` (distinct alphas change predictions vs plain ridge on an augmented matrix).
- [ ] Run → FAIL. Implement. Run `pytest tests/test_prediction.py -q` → PASS.
- [ ] Commit: `feat(research): group-wise ridge penalties for the augmented lane`

### Task 3: The predictor board (`qlab/research/board.py`)

**Files:**
- Create: `qlab/research/board.py`
- Test: `tests/test_board.py`

**Interfaces (Produces):**
- `MODEL_IDS = ("ridge:none", "groupwise:angle", "groupwise:zz", "groupwise:angle_zz", "kernel:linear", "kernel:angle", "kernel:zz")`; `BASELINE_MODEL_ID = "ridge:none"`.
- `run_predictor_board(panel, *, models=MODEL_IDS, alphas=(0.1, 1.0, 10.0), map_weights=(0.25, 1.0, 4.0), n_splits=5) -> dict` with keys: `n_obs, n_folds, target, horizon_days, embargo_days, features, baseline, models (list of per-model dicts), ranking (ids), champion (id|None), admitted_any (bool), admission {mean_ic_strictly_above, ic_stability_strictly_above}`.
- Per-model dict: `model_id, family, augmentation|kernel, mean_ic, ic_std, ic_stability, usable, per_fold (fold, ic, hyperparams), delta_mean_ic_vs_baseline, wins_vs_baseline, paired_t_vs_baseline (None for baseline; 0.0 when fold diffs are all zero)`.
- Every model sees the **same** `purged_walk_forward_splits` folds. Per fold: augment/scale bounds and standardization fitted on train only. Kernel models tune `(alpha, w_map)` by the same inner-CV pattern; `kernel:linear` pins `w_map=0.0`.
- Validation: models must be unique, non-empty, a subset of `MODEL_IDS`, and include the baseline — each violation raises ValueError.
- Ranking key: `(-mean_ic, ic_std, model_id)`. `champion` = first admitted in ranking.

**Steps:**
- [ ] Failing tests: `test_the_board_requires_the_baseline`, `test_an_unknown_model_is_refused`, `test_duplicate_models_are_refused`, `test_kernel_linear_matches_the_primal_baseline_per_fold` (the dual/primal identity — same folds, same inner-CV scores → identical fold ICs, atol 1e-8), `test_the_board_is_deterministic_across_runs`, `test_admission_and_ranking_are_propagated`, `test_champion_is_none_when_nothing_admits`.
- [ ] Run → FAIL. Implement. Run `pytest tests/test_board.py -q` → PASS.
- [ ] Commit: `feat(research): the predictor board — paired champion/challenger evaluation on shared purged folds`

### Task 4: Owner MCP tool + proxy + allowlists

**Files:**
- Modify: `qlab/mcp/quant_lab.py` (new tool beside `research.predict_vol`, inside `if owner_only:`)
- Modify: `qlab/ui/server.py:154` (`OWNER_LAB_TOOLS` + membership)
- Modify: `qlab/mcp/tui_proxy.py` (proxy wrapper beside `research_predict_vol`)
- Modify: `qlab/tui/claude.py:60` (`_LAB_TOOL_BASES`)
- Test: `tests/test_mcp_server.py`, `tests/test_ui.py` (append)

**Interfaces (Produces):**
- Owner tool `research.predictor_board(as_of: str = "", universe: str = "core", lookback_days: int = 756) -> dict` — mirrors `research.predict_vol`'s wrapper: `st.budget.charge`, `check_as_of`, universe load, `market.snapshot(..., offline=st.offline, seed=st.seed)`, `snap.log_returns()` panel → `run_predictor_board`, then `registry.log_run("predictor_board", spec)` where spec = board + `{as_of, universe, lookback_days, source, snapshot_id, dsr_trial_counted: False}`. Returns `{run_id, board, caveats}`; caveats name the ranking rule, the DSR exemption, and research-stage status.
- Proxy tool `research_predictor_board` with the same signature delegating via `lab("research.predictor_board", {...})`.

**Steps:**
- [ ] Failing tests: extend the owner-only asymmetry test in `tests/test_mcp_server.py` (tool present on owner path, absent from combined server) and add an owner-path invocation test asserting a `runs` row of kind `predictor_board` with `dsr_trial_counted is False` and no `backtests` row, on `Registry(":memory:")` with an offline snapshot.
- [ ] Run → FAIL. Implement all four wiring points. Run `pytest tests/test_mcp_server.py tests/test_ui.py -q` → PASS.
- [ ] Commit: `feat(mcp): research.predictor_board — owner-only board tool, proxied and allowlisted`

### Task 5: `atlas_context["predictors"]`

**Files:**
- Modify: `qlab/ui/server.py` (`atlas_context`, ~:1153)
- Test: `tests/test_ui.py` (append)

**Interfaces (Produces):**
- New context key `predictors`: newest `predictor_board` run summarised as `{status: "ok", as_of, source, age_days (int|None), champion {model_id, mean_ic, ic_stability, usable} | None, baseline {model_id, mean_ic}, best_delta_vs_baseline, admitted_any, run_id}` or `{"status": "never_ran"}`. Read via the registry's existing recent-runs accessor filtered to kind `predictor_board`; malformed spec → `{"status": "unreadable", "run_id": ...}` (absence named, never faked).
- `atlas_facts` untouched.

**Steps:**
- [ ] Failing tests: `test_atlas_context_carries_the_predictor_board`, `test_atlas_context_predictors_names_never_ran`, and re-run `test_the_gate_input_stays_narrow` unchanged.
- [ ] Run → FAIL. Implement. Run `pytest tests/test_ui.py -q` → PASS.
- [ ] Commit: `feat(atlas): the predictor board joins the reasoning surface, not the gate`

### Task 6: Atlas tool grant

**Files:**
- Modify: `agents/atlas.md` (add `mcp__qlab__research.predictor_board`, dotted like peer files)
- Regenerate: `.claude/agents/atlas.md`, `.bob/personas/atlas.yaml` via `python -m qlab.agents.loader sync`
- Test: `tests/test_agents.py` (existing sync/byte-compare tests)

**Steps:**
- [ ] Edit the role file's front-matter tool list and prose (one sentence: the board is advisory evidence, never a number Atlas invents).
- [ ] Run `python -m qlab.agents.loader sync`; then `pytest tests/test_agents.py -q` → PASS.
- [ ] Commit: `feat(agents): atlas may read the predictor board`

### Task 7: Textual research card

**Files:**
- Modify: `qlab/tui/app.py` (`_render_research`, ~:3540)
- Test: `tests/test_tui.py` (append)

**Interfaces:** Consumes `snapshot["runs"]`; picks the newest `kind == "predictor_board"` run; renders a compact table (model, mean IC, stability, Δ vs baseline, admitted) plus a never-ran line. Guards non-dict spec exactly like the existing prediction guard.

**Steps:**
- [ ] Failing test: `test_research_view_renders_the_predictor_board` (fixture snapshot with one board run; assert champion id and an admission word appear in the Static content) + a never-ran assertion.
- [ ] Run → FAIL. Implement. Run `pytest tests/test_tui.py -q` → PASS.
- [ ] Commit: `feat(tui): the predictor board card on the research view`

### Task 8: Ratatui card

**Files:**
- Modify: `clients/atlas-tui/src/model.rs` (Option-typed board fields on `RunSpec` or a sibling struct), `src/store.rs` (accessor `predictor_board()`), `src/ui/views/research.rs` (`draw_predictors` card following `draw_forecast`: three-state never-ran / unreadable / table, `refuse()` below width floor)
- Modify: `clients/atlas-tui/tests/fixtures/tui_snapshot.json` (add a `predictor_board` run row)
- Test: `clients/atlas-tui/tests/golden_research.rs` (+ regenerated insta snapshots), `tests/model_roundtrip.rs`

**Steps:**
- [ ] Add fixture row; write the golden expectation; `cargo test` → FAIL.
- [ ] Implement model/store/view; regenerate snapshots deliberately (`INSTA_UPDATE=always cargo test`, then review the diff by eye before accepting).
- [ ] `cd clients/atlas-tui && cargo test` → PASS.
- [ ] Commit: `feat(atlas-tui): the predictor board card, golden-pinned`

### Task 9: Measurement + docs

**Files:**
- Create: scratchpad sweep script (not committed) running the board over 12 offline seeds, paired vs baseline.
- Create: `planning-docs/2026-07-31-quantum-predictor-board-measurement.md` (the table, wins, paired t, verdict — positive or negative).
- Modify: `TODO.md` (the ML-revival line points at the board), `README.md` only if the verdict changes the front-page claim.

**Steps:**
- [ ] Run the sweep offline; record per-seed champion/ICs; sanity-check: nonzero variance across seeds (a zero-variance sweep is a broken sweep).
- [ ] Write the dated doc; update TODO.md.
- [ ] Commit: `docs(planning): the predictor board measured — 12-seed paired sweep`

### Task 10: Full-suite gate

- [ ] `.venv/bin/python -m pytest` from the worktree root → full offline suite green.
- [ ] `cd clients/atlas-tui && cargo test` → green.
- [ ] Final review of `git log`; no stray files; worktree clean.

## Self-review

Spec coverage: design §1→Tasks 1-2, §2→Task 3, §3→Task 4 (snapshot path), §4→Task 4, §5→Tasks 5-6, §6→Tasks 7-8, §7→Task 9, §8→spread across all tasks. No gaps found. Types checked: `run_predictor_board` return shape used by Tasks 4/5/7/8 matches Task 3's definition; `quantum_gram` signature in Task 3's kernel adapters matches Task 1.
