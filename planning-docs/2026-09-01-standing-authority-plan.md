# Standing Authority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A completed governed analysis books itself, within ceilings the operator set once, under a persisted grant the owner checks and no agent can reach.

**Architecture:** `qlab/governance/authority.py` already holds the model — it is wired, not rebuilt. The grant gains one ceiling (`max_books_per_day`) by registry migration. The owner computes `detect_anomalies`' four inputs from live desk state, and the heartbeat books at most one covered proposal per tick through the *same* steps 2–6 the clicked path uses. Two routes create and revoke a grant, refusing chat origin. Settings gains an AUTHORITY card. The book route's `submitted`-plan bug is fixed on the way past.

**Spec:** `planning-docs/2026-09-01-standing-authority-design.md` — binding. Read it, and the module docstring it reviews, before Task 1.

## Global Constraints

- **One DuckDB writer.** Everything reaches the registry through the owner (`qlab/ui/server.py`); no second writer, no new process.
- **The owner is threaded** (invariant 9). New shared mutable state on `UISession` needs a lock; the existing order is `_LOCK → _universe_lock → _mandate_lock`, with `_rights_lock`/`_posture_lock` as leaves. A grant read on the `/api/tui` hot path must not add a per-request file or table scan without a cache keyed on `Registry.run_revision`, the way the existing caches are.
- **Fail loud** (invariant 4). Absence refuses: no grant, an unreadable grant, an anomaly input the owner cannot compute, or a ceiling it cannot evaluate all refuse the automatic path with a sentence naming which.
- **Invariant 3 stands.** A grant replaces the per-plan human confirmation and nothing else. Referee PASS pinned to the plan's own `targets_hash`, the mandate, the cost gate, reconcile and execution-time revalidation all still run, in today's order. Steps 2–6 of `book_current_proposal` are shared verbatim — extract them rather than copy them.
- **No agent-reachable path.** No MCP tool (`qlab/mcp/server.py`, `qlab/mcp/quant_trader.py`, `qlab/mcp/tui_proxy.py`), no chat action tool, no proxy verb touches a grant. The create/revoke route refuses `X-Qlab-Origin: chat`, as `POST /api/atlas/rights` does. A census test must pin the absence across all three surfaces.
- **Invariant 10.** Every seam needs a production caller and a test that fails if reverted. This whole stream exists because a well-tested module had neither.
- Tests never open `.lab/registry.duckdb`; use `Registry(":memory:")`. Run them with the repo's own interpreter, `.venv/bin/python -m pytest <targets> -p no:cacheprovider`, no `-q` (the repo's addopts has it).
- Rust: ARMED is `cargo test`, GLASS is ONLY `cargo test --no-default-features`; `tests/operator_gate.rs` gains assertions, never exemptions. The pty tests hold a shared mutex — do not add `--test-threads` anywhere.
- Comment density: constraints the code cannot show. No AI-attribution trailers. Commit by pathspec.

---

### Task A1: The ceiling the model was missing

**Files:** `qlab/governance/authority.py`, `qlab/state/registry.py` (migration + `create_authority_grant`), `tests/test_authority.py`.

**Interfaces produced:** `build_grant(..., max_books_per_day: int)` — positive, no default, refused when absent like every other ceiling; `authority_grants.max_books_per_day INTEGER` added by migration in the style of the `kind`-column migration; a grant row round-trips it.

- [ ] **Step 1:** failing tests — a grant without `max_books_per_day` is refused by name; a zero or negative one is refused; a valid one round-trips through the registry; an existing row predating the column reads as *refused*, not as unlimited (a pre-migration grant must never be more powerful than a new one).
- [ ] **Step 2:** run them, watch them fail. **Step 3:** implement. **Step 4:** green.
- [ ] **Step 5:** commit `feat(authority): a grant says how many books a day it covers`.

### Task A2: The anomaly inputs, computed from the live desk

**Files:** `qlab/ui/server.py` (a `_grant_anomalies()` helper), `tests/test_ui.py`.

**Interfaces produced:** `UISession._grant_anomalies(offline) -> list[str]` calling `detect_anomalies` with `halted`, `reconcile_clean`, `data_execution_eligible`, `recent_order_anomaly` read from live state.

- [ ] **Step 1:** failing tests — each of the four conditions produces its own anomaly string; a clean desk produces none; **an input the owner cannot compute is itself an anomaly** (an exception reading reconcile suspends rather than proceeding), each pinned separately.
- [ ] **Step 2–4:** red → green. Find the real sources: the halt flag, the reconcile the owner runs, the data permit the execute gate reads, and a rejected/expired order in the recent window. Cite each in a comment.
- [ ] **Step 5:** commit `feat(authority): the desk computes what suspends a grant`.

### Task A3: The gate, shared with the clicked path

**Files:** `qlab/ui/server.py`, `tests/test_ui.py`.

**Interfaces produced:** steps 2–6 of `book_current_proposal` extracted to one private method both paths call; `UISession.book_under_grant(offline) -> dict | None` returning `None` when nothing is covered, and the execution result when a fill happened; `grant_refusals(...)` composing `check_grant_covers` with the day's count.

- [ ] **Step 1:** failing tests — a covered proposal books and records the grant id; every refusal reason from `check_grant_covers` refuses; the day's limit refuses once reached and resets on the next trading date; an anomaly suspends without revoking; a referee PASS that does not match the plan's own hash refuses *even under a grant*; the clicked path is unchanged (regression pin, byte-identical refusals).
- [ ] **Step 2–4:** red → green. The extraction must not change one word of the clicked path's refusals.
- [ ] **Step 5:** commit `feat(authority): the owner books what a live grant covers`.

### Task A4: The bug in the same handler

**Files:** `qlab/ui/server.py`, `tests/test_ui.py`.

- [ ] **Step 1:** failing test — when `execute_plan_with_approval` raises *after* the plan is `submitted`, the approval survives (the resume path needs it) and the raised error says so; when it raises before, the approval is invalidated exactly as today.
- [ ] **Step 2–4:** red → green. Mirror `withdraw_orphans`' skip and cite it.
- [ ] **Step 5:** commit `fix(desk): a plan that reached the broker keeps its authority`.

### Task B1: Granting and revoking, refused to the chat

**Files:** `qlab/ui/server.py` (`GET/POST /api/desk/authority`, `POST /api/desk/authority/revoke`), `tests/test_ui.py`, `tests/test_mcp_server.py`.

**Interfaces produced (binding for B2):** `GET` → `{"grant": null | {grant_id, mode, allowed_universe, max_notional, max_turnover, max_orders, max_books_per_day, valid_from, expires_at, granted_by, books_today, days_left}, "anomalies": [str]}`; `POST` takes every ceiling explicitly and returns the composed grant or a 400 naming the missing one; revoke takes a reason and returns the revoked grant. Chat origin is refused on both writes.

- [ ] **Step 1:** failing tests — a grant round-trips through the routes; a missing ceiling is a 400 naming it; chat origin is refused by name on create and revoke; the census test proves no MCP surface, no chat action tool and no proxy verb names a grant.
- [ ] **Step 2–5:** red → green; commit `feat(desk): standing authority is granted and revoked on the desk`.

### Task B2: The AUTHORITY card

**Files:** `clients/atlas-tui/src/model.rs`, `src/net/http.rs`, `src/net/write.rs`, `src/ui/views/settings.rs`, `src/input.rs`, goldens.

- [ ] **Step 1:** failing tests/goldens — with no grant the card says so and offers the grant key; with one it shows the ceilings, books left today and days left; an anomaly is named on the card; `R` revokes with no typed confirmation; the glass build draws the card read-only and posts nothing; every key owes a `KEYMAP` row and a help row.
- [ ] **Step 2–5:** red → green, goldens read before acceptance, both legs; commit `feat(atlas-tui): the authority card shows what is left of a grant`.

### Task B3: The beat books it

**Files:** `qlab/operator/heartbeat.py`, `tests/test_ui.py` or a new `tests/test_standing_authority.py`.

- [ ] **Step 1:** failing tests — a tick with a covered proposal books exactly once; a second tick does not re-book the same plan; a tick with no grant, a suspended grant or an exhausted daily count books nothing and says why in an event; an exception inside the book leaves the tick alive (a beat that dies takes the desk's autonomy with it).
- [ ] **Step 2–5:** red → green; commit `feat(operator): the beat books what the grant already covers`.

### Task C1: Docs, record, suites, build

**Files:** `README.md`, `CLAUDE.md` (invariant 3 gains the third form; the Commands block), `docs/atlas.md`, `docs/cli.md` if it names the booking paths, `AGENTS.md`, `planning-docs/2026-09-01-standing-authority-completion.md`.

- [ ] Full `python -m pytest` (no `-q`); both cargo legs at full parallelism; clippy both legs; `cargo fmt --check`; `cargo build --release`. Exact counts in the record, plus what has NOT run live and every follow-up.
- [ ] Commit `docs: standing authority — what it books, and what still refuses`.
