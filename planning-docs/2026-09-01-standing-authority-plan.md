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

**Freshness (ruled, binding).** The automatic path refuses a plan older than `MAX_AUTO_BOOK_AGE_S = 120`, strictly tighter than the approval's 900-second TTL. The click is a freshness proof — a human looks at a card seconds old and refuses by not pressing — and a grant checks ceilings, never recency; without this bound the desk books a correctly-sized fill against a stale analysis every 30 seconds. Define the constant beside the gate, state the reason, and pin both sides of the boundary.

**One execution path (ruled, binding).** The automatic path calls the same `decide_approval(..., "approve")` the click calls; it never books around the approval record. Two ways to reach `approved`, one way to execute.

- [ ] **Step 1:** failing tests — a covered proposal books and records the grant id; a plan older than `MAX_AUTO_BOOK_AGE_S` refuses while the same plan a second younger books; every refusal reason from `check_grant_covers` refuses; the day's limit refuses once reached and resets on the next trading date; an anomaly suspends without revoking; a referee PASS that does not match the plan's own hash refuses *even under a grant*; the clicked path is unchanged (regression pin, byte-identical refusals).
- [ ] **Step 2–4:** red → green. The extraction must not change one word of the clicked path's refusals.
- [ ] **Step 5:** commit `feat(authority): the owner books what a live grant covers`.

### Task A4: The bug in the same handler

**Files:** `qlab/ui/server.py`, `tests/test_ui.py`.

- [ ] **Step 1:** failing test — when `execute_plan_with_approval` raises *after* the plan is `submitted`, the approval survives (the resume path needs it) and the raised error says so; when it raises before, the approval is invalidated exactly as today.
- [ ] **Step 2–4:** red → green. Mirror `withdraw_orphans`' skip and cite it.
- [ ] **Step 5:** commit `fix(desk): a plan that reached the broker keeps its authority`.

### Task A5: The kill switch halts the book it fired for

**Files:** `qlab/trader/plan.py`, `qlab/mcp/quant_trader.py`, `qlab/autopilot/loop.py`, `tests/test_trader.py` (or the module that covers the halt path).

`Registry.set_halt(halted, book=DEFAULT_BOOK)` is per-book by design — its docstring says "A halt on one venue is not a halt on all" — and `DEFAULT_BOOK` is `"simulated_paper"` while the Alpaca broker is `"alpaca_paper"`. Two mismatches follow, both verified:

1. **The kill switch does not stop the book it fired for.** `qlab/trader/plan.py:148` and `:310` latch `set_halt(True, book=broker.name)`, but the pre-trade check at `:296` reads `registry.get_account().get("halted")` — the *default* book. On an Alpaca paper book the drawdown kill switch fires, latches `alpaca_paper`, and the next plan's check reads `simulated_paper`, finds it clear, and executes.
2. **An operator's explicit halt may halt the wrong book.** `qlab/mcp/quant_trader.py:167`/`:188` and `qlab/autopilot/loop.py:313` call `set_halt` with no book, and `quant_trader.py:184` reports `get_account()` with no book — so on an Alpaca desk the halt tool writes and reads a book nobody is trading.

This is in scope because the whole automatic path rests on the kill switch suspending a grant: an auto-booker that keeps booking after a drawdown halt is the worst failure this stream could ship. Task A2 already reads `broker.portfolio_state()["halted"]`, which is correct for both books — that path is fine and is the shape to follow.

- [ ] **Step 1:** failing tests — a halt latched on `alpaca_paper` refuses the next non-liquidating plan on that book (today it executes); a halt on one book does NOT halt the other (the per-book design must survive the fix); the halt tool and the autopilot latch the book actually in use; `get_account` reporting names its book.
- [ ] **Step 2–4:** red → green. Thread the book through rather than changing `DEFAULT_BOOK` — the default is right for a single-book desk and wrong only where the caller knows better.
- [ ] **Step 5:** commit `fix(trader): the kill switch halts the book it fired for`.

### Task A6: A book is halted if anyone halted it

**Files:** `qlab/trader/broker.py`, `tests/test_alpaca_broker.py` (and any module covering `portfolio_state`).

Task A5 fixed the execute gate; this is the reporting half of the same defect, and it is what makes the desk *honest* rather than merely safe. `qlab/trader/broker.py:121` (simulated) reports `"halted"` from qlab's own registry latch — correct. `:271` (Alpaca) reports `bool(acct.trading_blocked)`, the **venue's** flag, and never reads qlab's latch. So on an Alpaca desk the drawdown kill switch latches `alpaca_paper`, and `portfolio_state()["halted"]` still says `False`.

Everything downstream inherits that lie: Task A2's `_grant_anomalies` sees no halt and reports the grant **live**, `daily_ops`, `risk_report` and the desk cards all say the book is trading. A5's fix means no fill actually happens — the plan gate refuses — but the operator is told a grant is live while every attempt is refused, which is the worst kind of correct: safe and inexplicable.

- [ ] **Step 1:** failing tests — with qlab's latch set on `alpaca_paper` and the venue clear, `portfolio_state()["halted"]` is True; with the venue blocked and qlab's latch clear it is also True; with both clear it is False; the simulated book is unchanged (regression pin).
- [ ] **Step 2–4:** red → green. `halted` becomes "halted by anyone" — the OR of the venue flag and qlab's latch for **this broker's own book**, never the default book. Say in a comment which source each disjunct is, because they fail differently: the venue's flag is the broker refusing, qlab's latch is the mandate refusing.
- [ ] **Step 5:** commit `fix(broker): a book is halted if the venue or the mandate says so`.

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
