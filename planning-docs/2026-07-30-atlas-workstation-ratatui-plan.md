# Atlas Workstation — Ratatui Rewrite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Textual (Python) workstation `qlab/tui/` with a single Rust/Ratatui client grown out of `clients/atlas-tui`, adding live-motion visuals (tachyonfx effects, flash decay, scrolling ticker, animated pipelines) and a FinceptTerminal-grade portfolio/markets surface, while preserving every governance invariant.

**Architecture:** The owner HTTP runtime (`qlab/ui/server.py`) stays exactly as it is — the only DuckDB writer, spawned as a subprocess by `qlab tui`. The Rust binary is a pure HTTP/SSE client with two postures: **glass** (default, read-only by construction — no write client is even constructed) and **operator** (`--operator`, allowlisted POSTs with typed confirm modals). A tokio event bus feeds an adaptive render loop (60 fps while effects run, 10 Hz idle); all state lives in one `Store` diffed each snapshot to drive a rule-based motion vocabulary.

**Tech Stack:** Rust 2021, ratatui 0.29, crossterm 0.28, tokio 1.x, reqwest 0.12 (blocking feature off; `stream` on), serde/serde_json, tachyonfx (latest, ~0.25), tui-big-text, throbber-widgets-tui, insta (snapshot tests), color-eyre. Dev loop: bacon, cargo-insta, VHS, tmux capture-pane.

## Global Constraints

Copied from `CLAUDE.md` — every task inherits these:

- **Never add a second DuckDB writer.** The Rust client reaches the registry only through the owner API. No registry handle, ever.
- **Tests never open `.lab/registry.duckdb`**; Python tests use `Registry(":memory:")`; Rust tests use checked-in JSON fixtures and local mock TCP servers. Everything passes fully offline.
- **Referee PASS is bound to `targets_hash`; plan execution requires a persisted checked plan + explicit human confirmation.** The operator posture's execute path must POST `/api/plans/execute` with `human_confirmed: true` only from a typed confirm modal. No raw-order tool, no agent-reachable execution path.
- **Fail loud.** Missing owner, malformed SSE, absent fields → visible error states (`NO OWNER RUNTIME`, `--`, error toast), never silent fallbacks.
- **Resolve files through `qlab/paths.py`** on the Python side; the Rust side takes the port from `QLAB_UI_PORT` (default 8765) as `clients/atlas-tui/src/client.rs` already does.
- **Restart the owner after changing code it serves.**
- **The owner is threaded** — any Python-side change to `UISession` state needs a lock.
- **Anything reachable must have a caller** — every new module/seam in this plan lands with a call site and a test in the same task.
- **A negative result is a deliverable** — abandoned approaches get a dated note in `planning-docs/`.
- Commit style: imperative, conventional prefix + scope, **no AI-attribution trailers**.
- Comments state constraints the code cannot show; match existing density.
- Rust code lives in `clients/atlas-tui` (existing crate, evolved in place — history preserved). `cd clients/atlas-tui && cargo test` must stay green after every task.
- AGPL boundary: FinceptTerminal is **mechanism reference only**. Reimplement from the descriptions in this plan; never open its source while writing code for this repo.
- **Time flows in as data.** No `Instant::now()` or wall-clock reads inside render, style, or effect code — `now`/`elapsed` arrive as parameters from the main loop. This is the systemic fix for the flaky-test class the Textual client suffered (`test_quote_event_repaints_only_market_pulse_and_universe` racing a 50 ms timer margin): every animation test runs on a mocked clock, zero sleeps, fully deterministic.
- **Truecolor is detected, not assumed.** `theme::detect()` reads `COLORTERM`/`TERM`; without truecolor the 4-level background ramp maps to xterm-256 greys 232–237 and semantic colors to their nearest 256-color indices (Terminal.app has no truecolor — the Obsidian depth ramp would otherwise collapse to one grey). One switch point; the no-hardcoded-color test keeps it that way.

---

## Part I — Vision and creative direction

### The aesthetic contract ("Obsidian desk")

One theme struct, no hardcoded color anywhere else (enforced by a test that greps `src/ui` for `Color::Rgb`):

| Token | Value | Role |
|---|---|---|
| `bg_base` / `bg_surface` / `bg_raised` / `bg_hover` | `#080808 #0a0a0a #111111 #161616` | 4-level depth ramp |
| `border_dim` / `border_med` / `border_bright` | `#1a1a1a #222222 #333333` | 3-level line ramp |
| `text_primary` / `text_secondary` / `text_tertiary` / `text_dim` | `#e5e5e5 #808080 #525252 #404040` | 4-level text ramp |
| `accent` / `accent_dim` | `#d97706 #78350f` | amber — the only theme-defining color |
| `positive` / `positive_dim` | `#16a34a #14532d` | semantic, constant across themes |
| `negative` / `negative_dim` | `#dc2626 #7f1d1d` | semantic, constant across themes |
| `warning` / `info` / `cyan` | `#ca8a04 #2563eb #0891b2` | caution / info / links+symbols |
| `chart[6]` | `#d97706 #0891b2 #16a34a #dc2626 #2563eb #ca8a04` | multi-series |

Conventions carried everywhere: `▌TITLE` amber panel headers; arrow-as-sign (`▲ 1.23`, magnitude absolute); missing = `--`, pending = `…` (distinct); explicit `+` on signed values; uppercase panel titles; `«‹›»` pagination.

### The motion vocabulary (rules, not decoration)

Motion always *means* something. Each rule is a (trigger → effect) pair evaluated from store diffs; no animation runs without a state change except the three "alive" indicators (*ticker, throbber, Atlas glyph*).

| Trigger | Effect | Budget |
|---|---|---|
| App start / view switch | tachyonfx `coalesce` dissolve-in of the new view | 300 ms |
| Quote tick on a visible row | cell bg flash, linear decay | 600 ms |
| Regime `robust_state` change | radial `sweep_in` across the regime strip, colored by new state | 800 ms |
| Drawdown tier worsens | red `hsl_shift` pulse on the book pane border | 2 × 400 ms |
| `halted: true` | full-frame desaturate + slow red border pulse until resumed | continuous |
| Approval created | toast slide-in + amber pulse on approvals badge | 250 ms + 4 s dwell |
| Workflow phase advance | traveling `●` along the phase pipeline; completed nodes settle solid | 400 ms/hop |
| Plan executed | green sweep across the plan card | 500 ms |
| SSE disconnect | connection chip decays amber→red; `RETRY n` counter ticks | continuous |
| Ticker bar | rotate 1 cell / 120 ms, pauses on hover-focus | continuous |
| Data in flight | braille throbber `⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏` in the panel header | continuous |
| Atlas heartbeat | existing `glyph.rs` mood automaton, kept as the identity element | continuous |
| New Atlas read (`atlas_read.as_of` change) | typewriter reveal — the brief uncovers left-to-right, oldest line first | 600 ms |
| Stress-gauge value change | needle tweens with ease-out instead of jumping | 400 ms |
| View entry (charts) | sparkline/curve draw-in reveals points left→right | 300 ms |

Full-frame effects (the HALT treatment) are capped at 30 fps — a 120×36 truecolor full rewrite per frame is pty-bandwidth-bound on slower emulators; per-pane effects run at 60.

### The shell

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ ACWI 112.34 ▲0.42%   SPY 512.10 ▼0.18%   GLD 201.77 ▲1.02%  … (scrolling)   │ ticker (1)
├───────┬──────────────────────────────────────────────────┬───────────────────┤
│ 1 DESK│                                                  │ ▌PULSE            │
│ 2 MKTS│                                                  │  stress gauge     │
│ 3 BOOK│                 active view                      │  breadth bar      │
│ 4 RSCH│                                                  │  movers ▲▼        │
│ 5 WORK│                                                  │ ▌ATLAS READ       │
│ 6 AUDIT│                                                 │  conviction …     │
│ 7 SETT│                                                  │  (atlas glyph)    │
├───────┴──────────────────────────────────────────────────┴───────────────────┤
│ /command …                              ● SSE  ● OWNER  research·auto  GLASS │ cmd/status (1)
└──────────────────────────────────────────────────────────────────────────────┘
```

Views: **DESK** (the Atlas read + tiles + tui-big-text equity hero), **MARKETS** (ETF grid, SPY hero chart, SPDR sector heatmap), **BOOK** (stats ribbon, blotter, holdings heatmap, perf chart), **RESEARCH** (leaderboard, runs, algorithm catalog), **WORKFORCE** (phase pipeline + console), **AUDIT** (event stream + approvals), **SETTINGS**. Keys `1–7`, `Tab`/`BackTab` cycle, `q` quits, `/` focuses command line, `z` zen mode (rails collapse, content maximizes), `f` fullscreens the focused pane.

---

## Part II — Toolchain, and the "is there a tool/MCP/plugin" answer

**Crates** (add via `cargo add` so the latest compatible versions resolve; pins below are the floor):

| Crate | Why |
|---|---|
| `ratatui = "0.29"` (already present) | core; `Sparkline BarChart Gauge Table Tabs Canvas` all built-in |
| `crossterm = "0.28"` + `event-stream` feature | async key/resize events |
| `tokio = { version = "1", features = ["rt-multi-thread","macros","sync","time"] }` | runtime + channels |
| `reqwest = { version = "0.12", features = ["json","stream"] }` | HTTP + SSE byte stream |
| `futures-util = "0.3"` | stream combinators for SSE |
| `serde`/`serde_json` (present) | typed snapshot model |
| `tachyonfx` | the effects engine — verify current API against docs before Task 15 |
| `tui-big-text` | equity hero number |
| `throbber-widgets-tui` | in-flight spinners |
| `color-eyre` | panic/report hooks that restore the terminal |
| dev: `insta` | golden-frame snapshot tests over `TestBackend` |

**Dev tools & aids (the direct answer to the tooling question):**

- **context7 MCP** — already connected in this workspace. Every implementation task that touches an unfamiliar crate API starts with `resolve-library-id` + `query-docs` for ratatui / tachyonfx / crossterm. This is the single highest-value aid: Ratatui's API moved fast (0.26→0.29 renamed core traits) and training-data recall is unreliable.
- **tmux capture-pane QA loop** — the trick that lets a coding agent *see* the TUI: `tmux new-session -d -s atlas 'cargo run'`, then `tmux send-keys -t atlas 2` and `tmux capture-pane -pet atlas` to read the actual rendered frame as text. Wire this as `clients/atlas-tui/scripts/qa.sh`. No MCP server for terminals is needed — this covers it.
- **VHS (charmbracelet)** — scripted `.tape` files render deterministic GIFs of the running TUI: demo artifacts for PRs and a human-reviewable visual-regression record. `clients/atlas-tui/demo/atlas.tape` lands in Task 21.
- **tachyonfx FTL editor** (browser playground on the tachyonfx repo) — iterate on effect DSL snippets visually before porting them into `fx.rs`.
- **bacon** — background `cargo check`/`test`/`clippy` watcher while iterating.
- **cargo-insta** — `cargo insta review` to accept/reject golden-frame diffs.
- **awesome-ratatui** — the catalog to consult before hand-rolling any widget; study `tickrs` and `alphai-tui` for prior art on stock dashboards.
- There is **no purpose-built "TUI MCP"**; context7 (docs) + tmux capture-pane (eyes) + insta (regression) + VHS (demos) together are the full aid stack.

**Reference material** (mechanisms, not code): the FinceptTerminal teardown summarized in Part I/III of this plan and in the 2026-07-30 session notes; `qlab/tui/formatting.py` and `qlab/tui/theme.py` as the Python behavior being replaced; `qlab/ui/server.py:2268-2630` as the API contract.

---

## Part III — Architecture

### Process model (unchanged owner, swapped client)

`qlab tui` keeps everything in `qlab/autopilot/cli.py:_cmd_tui` up to and including owner spawn + readiness wait, then replaces "run `QlabTui`" with "exec the `atlas` binary". Owner discovery order for the binary: `$QLAB_ATLAS_BIN` → `atlas` on PATH → `clients/atlas-tui/target/release/atlas` → fail loud with build instructions.

### Module map (all inside `clients/atlas-tui/src/`)

```
main.rs            terminal setup, color-eyre hooks, tokio runtime, run loop
bus.rs             AppEvent enum + mpsc plumbing
store.rs           Store { snapshot, regime_panel, quotes, conn, nav, fx_flags } + diffing
model.rs           typed serde structs for /api/tui, /api/regime/panel, SSE events
theme.rs           ThemeTokens + the one OnceLock<Theme>
format.rs          money/pct/compact-volume/arrow-sign/age helpers
net/http.rs        reqwest client: readyz, snapshot, regime_panel (+ WriteClient, operator only)
net/sse.rs         /api/stream reader: cursor resume, ping tolerance, reconnect backoff
fx.rs              FlashTracker + tachyonfx EffectManager + motion-rule table
input.rs           key routing: global keys → nav; focused view gets the rest
cmd.rs             slash-scoped command line: pure parser → Command enum
ui/shell.rs        frame layout: ticker/nav/content/pulse/status
ui/widgets/…       panel_header, tristate_spark, heat_cell, toast, pager, chips
ui/views/desk.rs   … one file per view (markets, book, research, workforce, audit, settings)
glyph.rs           (existing) Atlas mood automaton — kept
app.rs, client.rs, ui.rs   (existing) absorbed into the above over Tasks 1–5
```

### Event flow

```
crossterm EventStream ─┐
tokio interval (poll)  ─┤→ mpsc<AppEvent> → main loop: store.apply(event)
net::sse task          ─┤                      ↓ diff → fx rules enqueue effects
net::http task results ─┘                  render if (dirty || effects_active || frame_due)
```

`AppEvent`: `Key(KeyEvent)`, `Resize`, `Tick` (120 ms — ticker/throbber/glyph), `Snapshot(Box<Snapshot>)`, `RegimePanel(RegimePanel)`, `Sse(SseEvent)`, `Http(HttpResult)`, `ConnUp/ConnDown(Channel)`. Render pacing: a frame renders when state changed **or** any effect is running (then at 16 ms) **or** 100 ms elapsed (idle heartbeat for the three "alive" indicators).

### Owner API surface consumed

GET: `/readyz`, `/api/tui?offline=N&event_limit=100` — polled **adaptively**: 10 s idle, 2 s while a coordinator is driving or an operator action is in flight, and an immediate refetch when an SSE kind implies aggregate state changed (`workflow_phase`, `plan_executed`, `approval_*`, `halt`, `resume`, `atlas_mode`). Hammering a ~200 KB aggregate every 2 s around the clock (the Textual default) buys nothing the SSE bus doesn't already announce. Also `/api/regime/panel` (poll 30 s), `/api/stream` (SSE: `quote` + the durable audit kinds).
POST (operator posture only, each behind a modal): `/api/approvals/<id>/…` approve/reject, `/api/plans/execute` (`human_confirmed: true`), `/api/atlas/{mode,pause,resume,autonomy,message}`, `/api/workforce/fast`, `/api/workflows/start`, `/api/desk_mode`, halt/resume via their existing routes.

---

## Part IV — Governance posture

1. **Glass is the default and is read-only by construction**: `WriteClient` is only constructed under `--operator`; the type doesn't exist in the glass code path (an `Option<WriteClient>` that is `None` is *not* acceptable — construction is gated at the composition root so the glass binary path provably holds no writer). The status line always displays the posture (`GLASS` / `OPERATOR`), preserving the spirit of `ui.rs:339`'s existing test.
2. **Execution UX parity**: the confirm modal replicates `PaperConfirmScreen` — shows plan id, `targets_hash`, turnover, legs; requires typing `CONFIRM`; POSTs `human_confirmed: true`. Nothing else in the binary can reach `/api/plans/execute`.
3. **CLAUDE.md update is a deliberate task** (Task 21), not a side effect: the "read-only by construction" sentence about `clients/atlas-tui` becomes a statement about the glass posture, and invariant 3's TUI reference repoints to the Rust confirm modal.
4. Widening what Atlas *renders* never widens what it can *execute* — no new owner POST routes are added anywhere in this plan.

---

## Part V — Tasks

### Phase 1 — Foundation

### Task 1: Crate scaffold and module skeleton

**Files:**
- Modify: `clients/atlas-tui/Cargo.toml` (deps per Part II; binary renamed `atlas` via `[[bin]] name = "atlas" path = "src/main.rs"`)
- Create: `src/bus.rs`, `src/store.rs`, `src/model.rs`, `src/theme.rs`, `src/format.rs`, `src/fx.rs`, `src/input.rs`, `src/cmd.rs`, `src/net/mod.rs`, `src/net/http.rs`, `src/net/sse.rs`, `src/ui/mod.rs`, `src/ui/shell.rs`, `src/ui/widgets/mod.rs`, `src/ui/views/mod.rs`
- Modify: `src/main.rs` (module declarations; existing behavior unchanged this task)

**Interfaces:**
- Produces: module tree; `bus::AppEvent` enum exactly as in Part III; `cargo test` green.

- [ ] **Step 1:** Add dependencies with `cargo add ratatui crossterm --features crossterm/event-stream` etc. per Part II table (use `cargo add` for each; do NOT hand-pin patch versions). **Resolve the ratatui/tachyonfx version pair here**: `cargo add tachyonfx` decides — if the current tachyonfx requires ratatui 0.30, upgrade ratatui in the same commit (0.30 breaking changes that touch this plan: `TestBackend` errors become `Infallible`, `Buffer::filled` takes `Cell` by value). Also add `tracing` + `tracing-subscriber` (env-filter) + `tracing-appender`: a fullscreen TUI cannot println — warnings and up go to a log file (`ATLAS_LOG` path override; `-v` raises verbosity), and errors additionally surface as toasts once Task 16 lands.
- [ ] **Step 2:** Create every module file containing only its doc comment (one line: the responsibility from the module map) and, in `bus.rs`, the full `AppEvent` enum + `pub type Tx = tokio::sync::mpsc::UnboundedSender<AppEvent>;`.
- [ ] **Step 3:** `cargo build && cargo test` — existing tests still pass (existing `app.rs`/`client.rs`/`ui.rs` untouched).
- [ ] **Step 4:** Commit: `feat(atlas): scaffold module tree and event bus for the workstation rewrite`

### Task 2: Theme tokens and formatting helpers (TDD)

**Files:**
- Create: `src/theme.rs` (replace stub), `src/format.rs` (replace stub)
- Test: unit tests in-module (`#[cfg(test)]`)

**Interfaces:**
- Produces: `Theme` struct with the 24 color fields + `chart: [Color; 6]` from Part I, `theme() -> &'static Theme` via `OnceLock`, `Theme::change(v: f64) -> Color` (>= 0.0 → positive — zero counts positive, matching reference behavior; document it);
  `format::money(f64) -> String` (`$1,234.56`), `signed_money`, `signed_pct` (`+1.23%`), `pct1` (1 dp, weights), `compact_volume(i64) -> String` (`--`/`1.23K/M/B`, always 2 dp), `arrow_chg(f64) -> (String, Color)` → (`"▲ 1.23"`, positive) with absolute magnitude, `price(f64) -> String` (2 dp if > 1.0 else 4 dp), `MISSING: &str = "--"`, `PENDING: &str = "…"`.

- [ ] **Step 1:** Write failing tests first — the full table:

```rust
#[test]
fn arrow_carries_sign_and_magnitude_is_absolute() {
    let (s, c) = arrow_chg(-1.234);
    assert_eq!(s, "▼ 1.23");
    assert_eq!(c, theme().negative);
}
#[test]
fn compact_volume_bands() {
    assert_eq!(compact_volume(0), "--");
    assert_eq!(compact_volume(1_234), "1.23K");
    assert_eq!(compact_volume(5_600_000_000), "5.60B");
}
#[test]
fn price_precision_flips_under_one() {
    assert_eq!(price(512.1), "512.10");
    assert_eq!(price(0.4321), "0.4321");
}
#[test]
fn zero_change_is_positive_by_contract() {
    assert_eq!(theme().change(0.0), theme().positive);
}
```

- [ ] **Step 2:** `cargo test` → FAIL (unresolved names).
- [ ] **Step 3:** Implement `Theme` (all hex values from Part I as `Color::Rgb`), `format` helpers. No other file may call `Color::Rgb` — add the enforcement test:

```rust
#[test]
fn no_hardcoded_rgb_outside_theme() {
    let out = std::process::Command::new("grep")
        .args(["-rl", "Color::Rgb", "src/ui", "src/fx.rs"]).output().unwrap();
    assert!(out.stdout.is_empty(), "hardcoded colors: {}", String::from_utf8_lossy(&out.stdout));
}
```

- [ ] **Step 4:** `cargo test` → PASS.
- [ ] **Step 5:** Commit: `feat(atlas): obsidian theme contract and formatting vocabulary`

### Task 3: Typed snapshot model with offline fixture

**Files:**
- Create: `src/model.rs` (replace stub), `tests/fixtures/tui_snapshot.json`, `tests/model_roundtrip.rs`

**Interfaces:**
- Produces: `Snapshot` with typed sub-structs mirroring `/api/tui` (Part III of the 2026-07-30 payload map): `portfolio`, `live_portfolio { blocked, equity, cash, drawdown, gross_exposure, net_exposure, unrealized_pnl, halted, positions: Vec<Position>, marks }`, `Position { ticker, qty, avg_price, price, value, weight, unrealized_pnl, unrealized_pnl_pct }`, `market { source, as_of, bar_age_days, regime: Regime, assets: Vec<Asset> }`, `Asset { ticker, price, change_1d, change_20d, realized_vol, history: Vec<f64> }`, `Regime { regime, robust_state, confidence, effective_risk_fraction, posterior }`, `stress`, `performance { series: Vec<(String, f64)>, metrics }`, `quotes`, `approvals`, `plans`, `orders`, `workflows`, `atlas`, `atlas_heartbeat`, `atlas_read`, `events`. Every field `Option`-al or defaulted (`#[serde(default)]`) — the owner omits sections freely and **absent must render `--`, never zero**. Keep `#[serde(flatten)] extra: serde_json::Value` on `Snapshot` for unmodeled keys.
- Produces: `RegimePanel { robust_state, readings: Vec<Reading> }`, `Reading { indicator_id, state, signal, threshold, percentile, reasoning }` for `/api/regime/panel`.

- [ ] **Step 1:** Build the fixture from the real owner once (`curl -s localhost:8765/api/tui?offline=1 > tests/fixtures/tui_snapshot.json` with the owner running), then **redact/shrink by hand** to ~200 lines covering every modeled section. The fixture is committed; tests never touch the network.
- [ ] **Step 2:** Failing test:

```rust
#[test]
fn snapshot_fixture_deserializes_and_regime_is_nested_under_market() {
    let s: atlas::model::Snapshot =
        serde_json::from_str(include_str!("fixtures/tui_snapshot.json")).unwrap();
    assert!(s.market.as_ref().unwrap().regime.is_some(), "regime lives under market — the old client read it top-level and always showed unknown");
    assert!(!s.market.unwrap().assets[0].history.is_empty());
}
```

- [ ] **Step 3:** Implement the structs; run → PASS. This also **fixes the standing bug** (old `app.rs` read top-level `regime`).
- [ ] **Step 4:** Commit: `feat(atlas): typed snapshot model; fix regime path (was read top-level, served under market)`

### Task 4: Tokio runtime, store, and adaptive render loop

**Files:**
- Modify: `src/main.rs` (rewrite around tokio + bus), Create: `src/store.rs` (replace stub)
- Test: `tests/render_pacing.rs`

**Interfaces:**
- Consumes: `AppEvent` (Task 1), `Snapshot` (Task 3).
- Produces: `Store { pub snapshot: Option<Snapshot>, pub regime_panel: Option<RegimePanel>, pub nav: Nav, pub conn: Conn, dirty: bool }` with `apply(&mut self, ev: AppEvent) -> Vec<Trigger>` returning motion triggers (empty until Task 15; the enum exists now: `Trigger { RegimeChanged, DrawdownTierWorse, Halted, Resumed, ApprovalCreated, PhaseAdvanced, PlanExecuted, QuoteTick(String), ReadChanged }`). `Nav { view: ViewId, focus: Focus }`, `ViewId` = the 7 views.
- Produces: `main.rs` run loop: crossterm raw mode + alternate screen, EventStream forwarded to bus, 120 ms `Tick` interval, `should_render(&store, fx_active, last_frame) -> bool` implementing the Part III pacing rule as a **pure function**.

- [ ] **Step 1:** Failing test for pacing (pure function — no terminal needed):

```rust
// The clock is an argument (global time-as-data constraint): the 3-arg form
// that computed elapsed() internally raced the scheduler — the exact flake
// class this plan exists to kill.
#[test]
fn renders_on_dirty_or_fx_or_idle_heartbeat() {
    let t0 = Instant::now();
    let now = t0 + Duration::from_millis(50);
    assert!(should_render(true,  false, t0, now));  // dirty
    assert!(should_render(false, true,  t0, now));  // effects running
    assert!(!should_render(false, false, t0, now)); // idle, frame fresh (50ms < 100ms)
    assert!(should_render(false, false, t0, t0 + Duration::from_millis(150))); // heartbeat
}
```

- [ ] **Step 2:** Implement store + loop with three robustness rules baked in:
  - **Drain-then-render**: the loop drains *every* pending `AppEvent` (`try_recv` until empty) before drawing at most one frame — a burst of 50 SSE quote events coalesces into one repaint instead of 50. This is the client-side analogue of the reference terminal's `coalesce_within_ms` bus policy.
  - **RAII `TerminalGuard`**: raw-mode/alternate-screen teardown lives in `Drop`, *and* in the color-eyre panic hook, *and* on `tokio::signal` SIGINT/SIGTERM — three exits, one restore path; a dead TUI must never wedge the shell.
  - `tracing_subscriber` initialized before the terminal is entered (Task 1's file appender), so early failures are diagnosable.
- [ ] **Step 3:** `cargo test` → PASS; manual smoke: `cargo run` against a running owner shows the old single screen still (shell swap is Task 5).
- [ ] **Step 4:** Commit: `feat(atlas): tokio event bus, store with trigger diffing, adaptive render pacing`

### Task 5: Shell layout, tabs, and the golden-frame harness

**Files:**
- Create: `src/ui/shell.rs`, `src/ui/views/desk.rs` (placeholder tile grid), `src/ui/widgets/panel_header.rs`
- Modify: `src/main.rs` (render via shell), delete-absorb `src/ui.rs` (keep `glyph.rs`)
- Test: `tests/golden_shell.rs`

**Interfaces:**
- Consumes: `Store`, `Theme`.
- Produces: `shell::draw(f: &mut Frame, store: &Store)` computing the Part I layout (`Length(1)` ticker, `Length(8)` nav rail, `Min` content, `Length(34)` pulse rail, `Length(1)` status); `trait View { fn draw(&self, f: &mut Frame, area: Rect, store: &Store); fn on_key(&mut self, k: KeyEvent, store: &mut Store) -> Option<Command>; }`; `widgets::panel_header(title: &str) -> Line` rendering `▌ TITLE` (amber `▌`, bold uppercase title, `border_dim` bottom rule handled by the caller's Block); key routing `1..=7` → `ViewId`, `q` quit, `Tab` cycle.
- Produces: the **golden-frame harness** every later view task reuses:

```rust
pub fn frame_to_string(store: &Store, w: u16, h: u16) -> String {
    let backend = ratatui::backend::TestBackend::new(w, h);
    let mut term = ratatui::Terminal::new(backend).unwrap();
    term.draw(|f| atlas::ui::shell::draw(f, store)).unwrap();
    format!("{}", term.backend())
}
```

- [ ] **Step 1:** Failing test: build a `Store` from the Task 3 fixture, `insta::assert_snapshot!(frame_to_string(&store, 120, 36))` plus targeted asserts (`contains("▌ PULSE")`, `contains("GLASS")`, nav shows `1 DESK` highlighted).
- [ ] **Step 2:** Implement shell + placeholder desk view (tiles showing `--`); `cargo insta review` to bless the first snapshot.
- [ ] **Step 3:** The status line must state the posture: assert `contains("GLASS")` — this supersedes the old `the_read_only_boundary_is_stated_on_screen` test; port its intent, then delete `src/ui.rs`.
- [ ] **Step 4:** Manual QA via the tmux loop: `scripts/qa.sh` = new tmux session, run, capture-pane, print. Verify keys 1–7 switch highlighted nav entries.
- [ ] **Step 5:** Commit: `feat(atlas): workstation shell, view trait, golden-frame test harness`

### Phase 2 — Live data

### Task 6: HTTP poller

**Files:**
- Create: `src/net/http.rs` (replace stub; absorb `src/client.rs` behaviors: readyz gate, `QLAB_UI_PORT`, 8 s timeout, offline flag)
- Test: `tests/http_poll.rs` (std `TcpListener` mock serving canned bytes; no network)

**Interfaces:**
- Produces: `spawn_poller(base: String, offline: bool, tx: Tx)` — tokio task: `GET /readyz` until ok (2 s retry, emit `ConnDown(Owner)` while failing), then loop `GET /api/tui` every 2 s → `AppEvent::Snapshot`, `GET /api/regime/panel` every 30 s → `AppEvent::RegimePanel`. JSON that fails to deserialize emits `AppEvent::Http(HttpResult::Malformed(url, err))` — surfaced as an error toast, never swallowed.

- [ ] **Step 1:** Failing test: bind a `TcpListener` on port 0, respond to `/readyz` then `/api/tui` with the fixture; assert the bus receives `ConnUp(Owner)` then a `Snapshot` within 5 s.
- [ ] **Step 2:** Implement with reqwest; run → PASS. **Step 3:** Commit: `feat(atlas): async owner poller with fail-loud malformed handling`

### Task 7: SSE stream with cursor resume

**Files:**
- Create: `src/net/sse.rs` (replace stub)
- Test: `tests/sse_stream.rs` (TcpListener writing a canned SSE byte script: event, `: ping`, split-across-packets event, disconnect)

**Interfaces:**
- Produces: `spawn_sse(base: String, tx: Tx)` — connects `GET /api/stream`, parses `data: <json>\n\n` frames tolerating `: ping` comments and frames split across reads; tracks `(after, after_id)` from each event and reconnects with them after 2 s backoff (mirrors `qlab/tui/client.py:66-131` semantics); emits `AppEvent::Sse(SseEvent)` where `SseEvent { kind: String, payload: Value, ts, id }`; malformed frames emit `SseEvent { kind: "stream.malformed", .. }` (parity with the Python client). Emits `ConnUp/ConnDown(Stream)`.
- The **parser is a pure function** `feed(&mut SseBuf, bytes: &[u8]) -> Vec<SseEvent>` so tests need no async.

- [ ] **Step 1:** Failing tests: ping-only chunk yields nothing; split frame reassembles; bad JSON yields `stream.malformed`; cursor advances.
- [ ] **Step 2:** Implement parser then the tokio task. **Step 3:** PASS → Commit: `feat(atlas): resumable SSE client for the owner audit/quote bus`

### Task 8: Quote merge, flash tracker, scrolling ticker

**Files:**
- Modify: `src/store.rs` (quote merge), Create: `src/fx.rs` part 1 (`FlashTracker`), `src/ui/widgets/ticker.rs`
- Test: in-module + `tests/golden_shell.rs` addition

**Interfaces:**
- Produces: `Store.quote_overlay: HashMap<String, QuoteMark>` where `QuoteMark { price, change_1d, at: Instant }` — SSE quotes land in the **overlay and never mutate the snapshot**. All price reads go through `Store::asset_view(ticker) -> AssetView`, which applies the overlay when its stamp is newer than the snapshot's arrival. This exists because the naive merge has a regression bug: the periodic `/api/tui` poll rebuilds `market.assets` from the owner's cached valuations (quote TTL up to 30 s), so an in-place merge gets silently overwritten by *older* prices seconds later — the Python client (`_apply_quote_event`, `qlab/tui/app.py:1887`) accepts that brief regression; we don't. Returns `Trigger::QuoteTick(ticker)` per changed row.
- Produces: `FlashTracker { map: HashMap<FlashKey, Instant> }` with `flash(key, now: Instant)`, `style_for(key, now: Instant, base: Style) -> Style` (bg = `accent_dim` while `now < start + 600ms`, stepped decay at 200/400 ms via the dim tokens — discrete, deterministic, testable with a mocked clock per the global time-as-data constraint).
- Produces: `ticker::draw(area, assets, offset_cells)` — one row, `SYM price ▲x.xx%` triplets separated by 3 spaces, tiled so it wraps seamlessly; `offset_cells` advances 1 per `Tick` (120 ms) in `main.rs`.

- [ ] **Step 1:** Failing tests: quote event fills the overlay and `asset_view` prefers it over the stale snapshot price; a fresh snapshot arrival does **not** clobber a newer overlay mark; `style_for` with `now = start + 700ms` returns the base style (no sleeps — clock is a parameter); ticker string tiles (render at width 40 with 2 assets → content repeats). Ticker rotation advances by display cell, not byte (`unicode-width`), so `▲` never splits.
- [ ] **Step 2:** Implement; golden snapshot updated (ticker now populated from fixture).
- [ ] **Step 3:** Commit: `feat(atlas): live quote merge, 600ms flash decay, scrolling ticker bar`

### Phase 3 — Markets view (ETF / S&P 500 focus)

### Task 9: Markets grid and SPY hero chart

**Files:**
- Create: `src/ui/views/markets.rs`, `src/ui/widgets/braille_chart.rs`
- Test: `tests/golden_markets.rs`

**Interfaces:**
- Consumes: `market.assets` (7-row core universe today; more when `mandate.yaml` flips `universe_tier: extended` — render whatever arrives).
- Produces: left 60%: `Table` — columns `SYMBOL LAST CHG% 20D VOL SPARK WT TGT`; `SYMBOL` cyan, `LAST` amber via `format::price`, `CHG%` via `arrow_chg` with flash styling from `FlashTracker`, `SPARK` = ratatui `Sparkline` over `history[-12:]` per row is not possible inside a `Table` cell — use a `Line` of `▁▂▃▄▅▆▇█` glyphs (8-level quantize of history tail), colored by slope; `WT/TGT` from `portfolio.weights/target_weights`. Row selection ↑/↓ drives the hero chart.
- Produces: right 40%: `braille_chart::draw(f, area, &asset.history, selected_idx)` — `Canvas` with `Marker::Braille`, money y-axis gutter (4 labels), amber line, `←/→` moves a crosshair index rendered as a vertical `border_bright` rule + a value chip `dd MMM  $val` (the keyboard translation of FinceptTerminal's mouse crosshair). Header: `▌ SPY — SPDR S&P 500` when SPY selected (name from universe metadata when present, ticker otherwise).
- Produces: bottom strip: **sector heatmap** — for every asset whose ticker is in the SPDR set `XLK XLV XLF XLE XLY XLI XLB XLU XLRE XLC XLP SOXX` (subset present depends on prewarmed tier), draw a `heat_cell`: bg = positive/negative quantized into 6 alpha steps at `|chg| = 0.5/1/1.5/2/2.5/3.3%`, label `SYM +x.xx%`. When none are present render one `text_dim` line: `sector map needs the extended universe — qlab prewarm --universe candidates` (fail-loud guidance, not silence).

- [ ] **Step 1:** Extend the fixture with SPY + two SPDRs in `assets`; failing golden test asserting `▲`/`▼` arrows, amber LAST, the heatmap row, crosshair chip after simulating `→` twice.
- [ ] **Step 2:** Implement view + widgets. **Step 3:** insta review, tmux QA. **Step 4:** Commit: `feat(atlas): markets view — ETF grid, SPY braille hero with crosshair, SPDR sector heatmap`

### Task 10: Pulse rail — desk stress gauge, breadth, movers

**Files:**
- Create: `src/ui/widgets/pulse.rs` (rendered by shell's right rail)
- Test: golden additions + in-module unit tests

**Interfaces:**
- Consumes: `market.assets`, `market.regime`, `RegimePanel` (Task 6 polls it — this is its first renderer; the seam now has its caller).
- Produces, top to bottom:
  - **Desk stress gauge** (the fear/greed adaptation, honest to our data): `score = 50 + 50·(calm − stress)` from `regime.posterior`, then `−15` if the turbulence reading's `percentile > 0.9`, `−10` if `drawdown` reading state is stressed; clamp 0–100. Bands: ≤20 `STRESSED` negative, ≤40 `TENSE` warning, ≤60 `NEUTRAL` warning, ≤80 `CALM` positive, else `SERENE` positive. Render: `Gauge` widget + band word; `…` while `regime_panel` is `None`.
  - **Breadth bar**: advancers = assets with `change_1d > 0`; two-segment bar (`positive`/`negative` filled `█`) split by percentage, caption `adv 5 / dec 2`.
  - **Movers**: `▲ best  +x.xx%` / `▼ worst  −x.xx%` by `change_1d` (arrow colors, sign-prefix helper).
  - **Regime strip**: 5 rows, one per `RegimePanel` reading: `indicator glyph  state  percentile-bar` (10-cell `▰▱`), state-colored.
- [ ] **Step 1:** Unit-test the score function on 5 posterior/percentile cases incl. missing panel → `None`. Failing golden with the rail populated.
- [ ] **Step 2:** Implement. **Step 3:** PASS, commit: `feat(atlas): pulse rail — stress gauge, breadth, movers, five-indicator regime strip`

### Phase 4 — Book (portfolio) view

### Task 11: Stats ribbon

**Files:**
- Create: `src/ui/views/book.rs` (ribbon section), `src/ui/widgets/ribbon.rs`
- Test: `tests/golden_book.rs`

**Interfaces:**
- Consumes: `live_portfolio`, `performance.metrics`, `portfolio`.
- Produces: 3-row ribbon, 4 cells (`Ratio` 28/20/16/36): `PORTFOLIO VALUE` (hero `money(equity)`, sub `N positions · cash $x`), `UNREALIZED P&L` (hero signed+colored, sub `+x.xx% · ▲g ▼l` counting positive/negative `unrealized_pnl`), `TODAY` (from `performance.window_change`), `RISK & POSITIONING` 2×3 chips: `SHARPE`/`GROSS`, `VOL`/`NET`, `MDD`/`CVAR95` — every chip `--` when its `Option` is `None`. This ribbon is the **single source of truth**: no other Book panel repeats these numbers (assert the frame contains `equity` exactly once).
- [ ] Steps: failing golden (fixture has metrics; also a stripped-fixture case rendering `--`) → implement → PASS → commit `feat(atlas): book stats ribbon — hero KPIs with graceful degradation`.

### Task 12: Positions blotter with tri-state sparkline

**Files:**
- Modify: `src/ui/views/book.rs`, Create: `src/ui/widgets/tristate_spark.rs`, `src/ui/widgets/pager.rs`
- Test: golden + unit

**Interfaces:**
- Consumes: `live_portfolio.positions`, `market.assets[].history`, `FlashTracker`.
- Produces: `Table` — `SYMBOL QTY LAST AVG WT% MKTVAL P&L P&L% TREND` (9 of the 11 reference columns; COST-BASIS/CHG% dropped — not in our payload; documented in the header comment). Color contract: `SYMBOL` cyan, `MKTVAL` warning-amber, **`P&L` and `P&L%` share one color decision**, `LAST` flashes on `QuoteTick`. `TREND` = `tristate_spark(history_tail: Option<&[f64]>)`: `Some(non-empty)` → 8-glyph block sparkline colored by slope; `Some(empty)`/stale → dashed `╌╌╌╌` in `border_med`; `None` (ticker absent from market assets) → flat `────` in `text_tertiary`. Sort keys numeric (`s` cycles column, stable sort); `pager` widget `« ‹ 1/3 › »` when > visible rows, preserving the top row across resizes (recompute page from top index).
- [ ] Steps: unit tests for the three spark states + stable sort; failing golden; implement; PASS; commit `feat(atlas): positions blotter — paired P&L color axes, tri-state sparklines, pager`.

### Task 13: Holdings heatmap + performance chart

**Files:**
- Modify: `src/ui/views/book.rs`, Create: `src/ui/widgets/heat_cell.rs` (shared with Task 9 — extract now, retrofit markets import)
- Test: golden + unit for the ramp

**Interfaces:**
- Produces: right rail of Book: 2-col grid of holding cells (`SYM` + `+x.x%` pnl_pct), bg from `heat_cell::ramp(value_pct: f64, mode)` — quantized `intensity = min(|v|/20, 1)` into 6 steps between the dim and bright semantic colors; `TOP MOVERS` footer (▲ max / ▼ min by `unrealized_pnl_pct`). Toggle `h` cycles PNL/WT (WT mode: amber ramp `t = min(w/40, 1)`).
- Produces: bottom: equity curve from `performance.series` — `Canvas` braille, `p` cycles period slices (ALL/1Y/3M/1M as index windows on the series; a slice with < 2 points renders `needs more history — daily marks only`, the honest analogue of FinceptTerminal's disabled-period tooltip).
- [ ] Steps: unit-test ramp band edges (0.4%→step1, 21%→step6 clamp); failing golden; implement; PASS; commit `feat(atlas): holdings heatmap with quantized ramps + period-sliced equity curve`.

### Phase 5 — Motion layer

### Task 14: Desk view with big-text hero and throbbers

**Files:**
- Modify: `src/ui/views/desk.rs` (replace placeholder)
- Test: golden

**Interfaces:**
- Produces: DESK is a 40/60 split. **Left: THE READ** — the desk's soul, not a rail afterthought: conviction + agreement chips, `quantitative_state` line, then `tensions[]` (amber `▌`), `would_change_my_mind[]` (cyan `▌`), `news{tone, headlines[]}` with `news_source`, grounding-hash footer in `text_dim` — all from `atlas_read`. A changed `atlas_read.as_of` emits `Trigger::ReadChanged` and the pane re-reveals typewriter-style: `reveal_chars = elapsed/600ms × total` sliced top-to-bottom (hand-rolled — it is a substring render, no crate needed). **Right:** 2×3 `Tile` grid (equity hero via `tui-big-text` `PixelSize::Quadrant` rendering `money(equity)`, regime tile, allocation tile with `▰▱` weight bars current-vs-target, alerts tile from `stress` fields, verdict tile from latest `decisions` verdict, replay tile 2008/2020/2022) + throbber in any tile whose section is absent while `conn.owner` is up (in-flight, not missing).
- [ ] Steps: failing golden with hero digits, a tension line, and a mid-reveal frame (mocked clock at 300 ms → assert exactly half the read is visible) → implement → PASS → commit `feat(atlas): desk view — the Atlas read with typewriter reveal, big-text equity hero, status tiles`.

### Task 15: tachyonfx effect manager and motion rules

**Files:**
- Modify: `src/fx.rs` (EffectManager), `src/main.rs` (post-draw effect pass), `src/store.rs` (emit all `Trigger`s)
- Test: `tests/fx_rules.rs`

**Interfaces:**
- Consumes: `Trigger` diffs (Task 4). API verified against tachyonfx docs 2026-07-30: the crate **ships its own `EffectManager<K>`** — do not hand-roll one. Effects are stateful, created once, applied after widgets render, via `manager.process_effects(elapsed.into(), frame.buffer_mut(), area)`. Keyed adds replace: `add_unique_effect(key, fx)` cancels the previous effect under that key — which is exactly the HALT lifecycle (a `Resumed` trigger replaces the repeating HALT effect under `FxKey::Halt` with a short restore fade).
- Produces: `FxKey { ViewSwitch, Regime, Halt, Read, PlanCard, Toast }` and `fx::rules(t: &Trigger, rects: &ShellRects, mgr: &mut EffectManager<FxKey>)` mapping per the Part I vocabulary table — e.g. `Trigger::RegimeChanged → fx::sequence(&[fx::sweep_in(..), fx::fade_from_fg(..)])` over the regime strip rect with `(800, Interpolation::CubicInOut)`; view switch → `fx::coalesce(300)` keyed `ViewSwitch` over the content rect; `ReadChanged` → the Task 14 typewriter (hand-rolled slice reveal — not a tachyonfx effect — so DESK owns it; the rule here only fires a subtle `fade_from_fg` behind it). `mgr.is_running()`-equivalent (check the crate's method name at implementation) feeds `should_render`; per-pane effects tick at 60 fps, the full-frame HALT effect at 30 (Part I cap).
- Produces: easing for *values* (the gauge needle tween, Part I): `fx::ease_out_cubic(t: f32) -> f32` hand-rolled in `fx.rs` — tachyonfx `Interpolation` eases *effects*, not app values; the gauge stores `(prev, target, started_at)` and renders the eased blend.
- Produces: rule coverage test — every `Trigger` variant maps to at least one rule arm (exhaustive `match`, compile-time); a mocked-clock test drives `process_effects` past the longest duration and asserts the manager reports idle.
- [ ] Steps: failing rules test → implement → tmux QA: toggle desk mode / start a workflow against a live owner and watch sweeps → commit `feat(atlas): tachyonfx motion vocabulary wired to store diffs`.

### Task 16: Toasts and connection chips

**Files:**
- Create: `src/ui/widgets/toast.rs`, Modify: `src/ui/shell.rs` (status chips + toast overlay pass)
- Test: golden + unit

**Interfaces:**
- Produces: `ToastQueue { push(level, title, msg) }` rendering up to 3 stacked 40×4 bordered boxes top-right (level-colored `●`, title, message, age), auto-expire 4 s, `Clear` under each; wired to: `approval_created`, `stream.malformed`, `HttpResult::Malformed`, `plan_executed`, `halt`. Status chips: `● SSE`/`● OWNER` in positive/warning/negative by `Conn` state with reconnect counter.
- [ ] Steps: unit expiry test → golden with a forced toast → implement → commit `feat(atlas): toast overlay and connection chips`.

### Phase 6 — Operator posture

### Task 17: WriteClient and the confirm-modal contract

**Files:**
- Modify: `src/net/http.rs` (WriteClient), `src/main.rs` (`--operator` flag; composition root), Create: `src/ui/widgets/confirm.rs`
- Test: `tests/operator_gate.rs`

**Interfaces:**
- Produces: `WriteClient` behind a **Cargo feature `operator`** (`#[cfg(feature = "operator")]` on `net/http.rs`'s write half, the confirm modal, and every operator key branch). The default glass build **contains no write code at all** — "read-only by construction … holds by absence" survives the rewrite as a compile-time fact, not a runtime `Option`. The `--operator` CLI flag additionally gates activation inside an operator-featured binary (feature gates existence, flag gates activation); `qlab tui` builds/invokes with the feature, a monitoring box runs the default build. Status chip: `GLASS` / `OPERATOR` amber. Methods, mirroring the owner routes verbatim: `approve(id, note)` / `reject(id, note)` → `POST /api/approvals/<id>/…`; `execute_plan(plan_id)` → `POST /api/plans/execute {plan_id, human_confirmed: true}`; `atlas_mode(mode)`, `atlas_autonomy(on)`, `atlas_message(text)`, `workforce_fast(on)`, `desk_mode(label)`, `start_workflow(template, goal)`, `halt()`/`resume()`.
- Produces: `confirm::Modal { title, facts: Vec<(label, value)>, challenge: String }` — centered 50×12, renders plan id / `targets_hash` / turnover / legs. For plan execution the challenge is the **last 6 characters of the plan's `targets_hash`**, displayed only inside the modal — the confirmation is thereby bound to the exact checked plan (echoing how the referee PASS binds to `targets_hash`) and a blind scripted keystroke replay cannot confirm the wrong plan. Halt/resume/mode use a static `CONFIRM`. `execute_plan` takes a `ConfirmToken` constructible only inside the modal module — capability-style guard.
- [ ] Steps: failing tests — build matrix `cargo test` (glass: `#[cfg(not(feature = "operator"))]` test asserts the write module does not exist via `cfg!`) and `cargo test --features operator` (modal challenge derives from fixture `targets_hash`; `ConfirmToken` unconstructible outside the modal module) → implement → both matrices green → commit `feat(atlas): operator posture — feature-gated writes, hash-bound confirm modals`.

### Task 18: Approvals + plans surfaces

**Files:**
- Create: `src/ui/views/audit.rs` (approvals pane + event stream), Modify: `src/ui/views/book.rs` (plan cards)
- Test: golden both postures

**Interfaces:**
- Produces: Book gains plan cards (`plan_id · state · turnover · created`) — in operator mode `x` on a checked plan opens the Task 17 modal; in glass mode cards render with a `text_dim` `view-only` tag. AUDIT view: left = pending approvals list (`a`/`r` approve/reject via small confirm modal), right = live event stream from SSE (kind-colored rows, newest first, `flash` on arrival), which also gives the durable audit bus its first renderer.
- [ ] Steps: failing goldens (operator fixture shows keys hint; glass shows view-only) → implement → commit `feat(atlas): approvals and plan execution surfaces, posture-aware`.

### Task 19: Workforce console

**Files:**
- Create: `src/ui/views/workforce.rs`, `src/ui/widgets/pipeline.rs`
- Test: golden + unit for pipeline states

**Interfaces:**
- Consumes: `workflows[]` (+ SSE `workflow_started`/`workflow_phase`), `atlas_heartbeat.coordinator`.
- Produces: `pipeline::draw(steps)` — `○──○──●──○` horizontal chart (done=solid positive, active=amber + traveling dot animated on `Tick`, pending=`border_med`), one per active workflow with goal + elapsed; console pane = `atlas_message`/workflow events log; input row (operator: Enter sends `atlas_message`, `S` opens template picker → `start_workflow`; glass: input hidden entirely).
- [ ] Steps: unit pipeline glyph states → failing golden → implement → tmux QA against a real dispatched workflow → commit `feat(atlas): workforce console with animated phase pipelines`.

### Phase 7 — Command line + settings

### Task 20: Slash-scoped command line

**Files:**
- Create: `src/cmd.rs` (replace stub), Modify: `src/ui/shell.rs` (command row renders scope-aware suggestions), `src/input.rs`
- Test: in-module parser table tests

**Interfaces:**
- Produces: pure parser `parse(buf: &str) -> CmdState` where `CmdState = Picker(Vec<Scope>) | Scoped(Scope, query) | Verb(cmd, args) | Empty` — mode **derived from the text** (`/` opens picker; accepting a scope rewrites the buffer to e.g. `/ticker `; backspacing past the space reverts to picker). Scopes: `/view <name>`, `/ticker <SYM>` (selects in markets/blotter), `/plan <id>`, `/mode <desk-mode>` (operator), `/halt` `/resume` (operator, still modal-gated). Bare uppercase token ≤ 6 chars parses as `Ticker(sym)` (function-code grammar). Suggestions render in a one-line strip above the input; Enter dispatches `Command` to the store/router — parser never executes. The input widget is **hand-rolled** (buffer + cursor + in-memory `↑/↓` history, ~60 lines) rather than tui-textarea: the parser *is* the input model — text-derived mode needs raw buffer access on every keystroke, which a rich editor widget hides.
- Produces: **help overlay** — `?` opens a modal listing every binding, generated from `input::KEYMAP: &[(key, context, action)]`, the same static table the router matches on. One source of truth: a binding cannot exist without appearing in help (test: router arms count == KEYMAP rows).
- Architecture invariant, enforced here and retroactively checked: **views never perform IO** — `View::on_key` returns `Option<Command>`; only the runtime dispatches Commands to the poller/WriteClient. `grep -rn "reqwest\|WriteClient" src/ui/` must return nothing (add as a test alongside the no-hardcoded-color grep).
- [ ] Steps: table-driven parser tests (12 cases incl. revert-on-backspace) failing → implement → golden of picker open → commit `feat(atlas): slash-scoped command line with text-derived mode`.

### Task 21: Settings + research views, cutover behind a `--classic` soak valve

**Files:**
- Create: `src/ui/views/settings.rs`, `src/ui/views/research.rs`, `clients/atlas-tui/scripts/qa.sh`, `clients/atlas-tui/demo/atlas.tape`
- Modify: `qlab/autopilot/cli.py:_cmd_tui` (spawn `atlas` binary by default; `--classic` keeps the Textual path), `README.md`
- Test: `tests/test_ui.py` addition (Python), `cargo test`

**Interfaces:**
- Produces: settings view rendering `desk_mode`, `policy` constraints, `system` provenance, mandate universe, theme name — read-only facts (mode switching lives behind `/mode`, operator only). Research view: leaderboard `Table` (champion `★`, `OVERLAY_METRICS` columns), runs list, **and the algorithm catalog** (`algorithms` payload: id + stage chip `operational`/`research`/`offline`, stage-colored) — parity with the Textual reference view so no surface silently disappears at cutover.
- Produces: `_cmd_tui` keeps owner spawn/readiness verbatim, then: default path resolves the binary (`$QLAB_ATLAS_BIN` → `shutil.which("atlas")` → `clients/atlas-tui/target/release/atlas`) and `os.execvpe`s it with `QLAB_UI_PORT` set — missing binary → `SystemExit` with `cargo build --release` instructions (fail loud); `--classic` runs `QlabTui` exactly as today. `--operator` passthrough flag added to the `tui` subparser (sets the feature-build binary's flag).
- **`qlab/tui/` is NOT deleted in this task.** The soak valve exists so a week of real desk use can catch parity gaps while rollback is one flag, not a revert.
- [ ] **Step 1:** Python-side failing test: `_cmd_tui` with `QLAB_ATLAS_BIN` pointing at a stub asserts exec is attempted with `QLAB_UI_PORT` in env (monkeypatch `os.execvpe`); `--classic` asserts `QlabTui` is instantiated.
- [ ] **Step 2:** Implement both views + cli change; `python -m pytest` and `cargo test` green offline.
- [ ] **Step 3:** Record `demo/atlas.tape` (VHS: launch, tab through all 7 views, trigger a flash and a modal) and check in the tape (not the GIF).
- [ ] **Step 4:** tmux QA full pass; commit `feat(atlas): research + settings views; qlab tui launches the Ratatui workstation (--classic keeps Textual during soak)`.

### Task 22: Python TUI removal (after soak)

**Files:**
- Delete: `qlab/tui/` (all files), TUI-only tests in `tests/test_ui.py` (server tests stay)
- Modify: `qlab/autopilot/cli.py` (drop `--classic`), `pyproject.toml` (drop textual from `[operator]`), `CLAUDE.md`, `README.md`

**Interfaces:**
- Entry condition: at least one week of daily `qlab tui` use on the Rust client with no parity blocker filed. Parity gaps found during soak become tasks *before* this one runs.
- Produces: CLAUDE.md rewritten deliberately — the `clients/atlas-tui` paragraph becomes the glass/operator posture description ("glass build is read-only by absence — the write code is not compiled in"); invariant 3's TUI sentence repoints at the Rust hash-bound confirm modal; the flaky quote-repaint test note is retired with a line in this plan's completion entry (the race class is extinct under the time-as-data constraint).
- [ ] **Step 1:** Delete `qlab/tui/`; `grep -rn "qlab.tui" qlab/ tests/` returns empty; fix `pyproject.toml`.
- [ ] **Step 2:** `python -m pytest` green offline; `cargo test` + `cargo test --features operator` green.
- [ ] **Step 3:** Docs edits; commit `feat(atlas)!: retire the Textual workstation` + separate `docs:` commit for CLAUDE.md/README.

### Task 23 (stretch): Flight-recorder replay

**Files:**
- Create: `src/replay.rs`
- Modify: `src/main.rs` (`--replay <from-ts>`), `src/net/http.rs` (events fetch)

**Interfaces:**
- The audit bus is durable and cursor-addressable (`/api/events` serves the same rows SSE streams). `atlas --replay <from-ts>` feeds historical events through the **same store + fx pipeline** at ×1/×10/×60 speed (`+`/`-` keys), status line shows `REPLAY dd MMM hh:mm ×10` in warning amber. The desk becomes a reviewable flight recorder — watch yesterday's regime flip, approvals, and HALT exactly as they animated live. Zero new rendering code: this is the payoff of routing *everything* through `AppEvent` — replay is just another event source. Glass-only (replay never constructs a writer; the flag conflicts with `--operator`, refuse loudly).
- [ ] Steps: failing test — a canned event script through `replay::source()` produces the same `Trigger` sequence as the SSE path; implement; tmux QA on a real day's events; commit `feat(atlas): flight-recorder replay over the audit bus`.

---

## Deep-review addendum (v2 — same day)

A second adversarial pass over v1 changed the plan in ten places. Recorded here because the *reasons* are the deliverable:

1. **Quote-overlay instead of in-place merge (Task 8).** v1 copied the Python client's merge design, which has a latent regression: the aggregate poll rebuilds `market.assets` from owner-side cached valuations (quote TTL up to 30 s) and silently overwrites fresher SSE prices. Overlay-with-timestamp wins by construction.
2. **Adaptive polling (Part III).** A flat 2 s poll of a ~200 KB aggregate is the Textual client's habit, not a requirement — SSE already announces every state change worth refetching for. Idle drops to 10 s; state-changing SSE kinds trigger immediate refetch.
3. **Time-as-data global constraint.** The flaky Textual test (`50 ms` repaint race) is a *class*, not an incident. Banning wall-clock reads inside render/effect code makes every animation test deterministic and retires the class.
4. **Truecolor detection.** Terminal.app has no truecolor; without a 256-color fallback the entire 4-level Obsidian depth ramp collapses to one grey. One detect point, enforced by the existing no-hardcoded-color test.
5. **Feature-gated writes (Task 17).** A runtime `--operator` flag leaves write code compiled into the glass binary — "read-only by absence" would quietly become "read-only by if-statement." A Cargo feature keeps the CLAUDE.md claim literally true of the default build.
6. **Hash-bound confirm challenge (Task 17).** Typing the last 6 chars of `targets_hash` (shown only in the modal) binds the human confirmation to the exact checked plan — the same binding philosophy as the referee PASS — and defeats blind scripted keystroke replay (a tmux-driven agent typing a static `CONFIRM` would otherwise succeed).
7. **The Atlas read is first-class (Task 14).** v1 demoted the desk's judgment surface — tensions, would-change-my-mind, conviction — to a side rail. It is the soul of a governed desk and now anchors DESK, with a typewriter reveal as its signature motion.
8. **Catalog parity (Task 21).** v1 dropped the Textual reference view silently; the algorithm catalog (with stage chips) now lands in RESEARCH so nothing disappears unannounced at cutover.
9. **Soak valve (Tasks 21/22).** Hard-deleting `qlab/tui/` in the cutover commit made rollback a revert. Split: cutover with `--classic` fallback, deletion only after a week of real use — parity gaps become tasks, not incidents.
10. **Flight-recorder replay (Task 23, stretch).** Routing everything through `AppEvent` makes replaying the durable audit bus through the same store/fx pipeline nearly free — a governance-native feature no reference terminal has: watch yesterday's HALT animate exactly as it happened.

## Deep-review addendum (v3 — architecture hardening after doc verification)

A third pass, this time against live documentation (context7: tachyonfx, ratatui 0.29/0.30; the official async-template Action pattern), changed:

1. **tachyonfx ships `EffectManager<K>`** — the hand-rolled manager in v1/v2's Task 15 was reinventing the crate. Keyed `add_unique_effect` (same key replaces) is precisely the HALT lifecycle. Verified call: `process_effects(elapsed.into(), frame.buffer_mut(), area)` after widgets render.
2. **Version-pair resolution moved into Task 1.** ratatui 0.30 is out (breaking: `TestBackend` → `Infallible` errors, `Buffer::filled` by value); `cargo add tachyonfx` decides the pair, and an upgrade happens in the scaffold commit, not mid-project.
3. **Drain-then-render** in the main loop — all pending events coalesce into one frame; the client-side analogue of the bus `coalesce_within_ms` policy. Without it, an SSE burst renders N times.
4. **Three-exit terminal restore** — RAII `Drop` guard + panic hook + SIGINT/SIGTERM handler share one restore path. A crashed TUI that wedges the shell erodes trust faster than any missing feature.
5. **`tracing` to a file from before terminal entry** — a fullscreen app can't println; without a log file, every field report becomes "it went black."
6. **Hand-rolled command input over tui-textarea** — reversal of an earlier instinct, with the reason recorded: text-derived mode needs the raw buffer on every keystroke; the parser is the input model.
7. **Help overlay from the keymap table** — `?` renders the same static table the router matches on; a binding cannot exist without documentation (arms count == rows test).
8. **Views-never-do-IO invariant** made greppable: `on_key` returns `Command`s; only the runtime dispatches. Enforced by a test, like the no-hardcoded-color rule.
9. Deferred as flagged polish, not scope: OSC52 yank (`y` copies plan id/hash — works over ssh), odometer-style rolling digits on the equity hero (gimmick risk — build only if the desk feels static after Phase 5).

## Self-review notes

- Every consumed field named in Tasks 9–19 exists in the `/api/tui` payload map verified against `qlab/ui/server.py:1926-1996` on 2026-07-30; `/api/regime/panel` route confirmed at `server.py:2371`; POST routes confirmed at `server.py:2320-2621`.
- Type-consistency: `Trigger` (Task 4, incl. `ReadChanged`) is the contract consumed by Tasks 14/15; `frame_to_string` (Task 5) is the harness for every golden test; `heat_cell` is created in Task 13 and explicitly retrofitted into Task 9's import; `FlashTracker` and the confirm `Modal` take `now`/challenge as data per the global constraints.
- Known open risk, called out rather than hidden: tachyonfx API names in Task 15 are indicative — the task's first step is a context7 doc pull, and the rule-coverage test is API-agnostic.
- Workforce parity decision: the TUI console shows event-level progress (SSE `workflow_*` + `atlas_message`); deep per-role streaming remains available via the existing `qlab workforce run "GOAL"` headless path — the Claude-CLI embedding (`qlab/tui/claude.py`, 1181 lines) is deliberately not ported, since the owner drives dispatched workflows itself as of c4b17ea.
- Scope deliberately excluded (YAGNI): symbol A–J group linking across panes (single-process app — the universe selection already links views), ratatui-image / kitty graphics protocol (fragmented terminal support; braille is universal), mouse support, a second theme.
