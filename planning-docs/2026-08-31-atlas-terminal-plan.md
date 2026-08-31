# Atlas Terminal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The real Claude CLI runs inside the ATLAS tab on a pseudoterminal, with the desk's own sidebar beside it, and the startup door can honestly ask which mind runs Atlas.

**Architecture:** A gated `src/pty.rs` owns the child (spawn `qlab cli` on a `portable-pty`, a blocking reader thread posting bytes onto the existing bus, write/resize/kill, exit as an event). The store holds the `vt100::Parser`, so a frame stays a pure function of `(store, fx, instant)`. `src/ui/widgets/terminal.rs` renders the parsed screen with `tui-term`'s `PseudoTerminal`. ATLAS's main column follows the *child*: the pane while one runs, today's chat otherwise; the configured mind decides only whether `/cli` may start one. The owner gains a `chosen` flag on `llm_config` so "nobody picked a mind" is expressible.

**Tech Stack:** Rust, ratatui 0.30, crossterm 0.29, tokio (no `process`/`io-util` features — the pty reader is a blocking thread), new deps `portable-pty = "0.9"`, `vt100 = "0.16"`, `tui-term = "0.3"` (resolution verified: one `ratatui 0.30.2` in the lock, no duplicate). Python 3.13 owner.

**Spec:** `planning-docs/2026-08-31-atlas-terminal-design.md` — binding. Read it before Task 1.

**Implementation map (read for anchors, not for authority):** `/private/tmp/claude-501/-Users-azainmac-codebases-quant-trading-agent/6dd5395d-5770-477d-9be4-417121f1329c/scratchpad/atlas-terminal-map.md` — 1428 lines, every claim carrying a `file:line`.

## Global Constraints

- **Two legs.** ARMED is `cargo test` (default features `["operator"]`); GLASS is ONLY `cargo test --no-default-features`. The monitoring build must contain no pty, no spawn, no forwarded keystroke — `tests/operator_gate.rs` is the census and gains the new module's name.
- **`ui/` never does IO** (`tests/operator_gate.rs:139`). The process-spawning module lives at the crate root beside `handoff.rs` (precedent: `operator_gate.rs:419`), trait-fronted like `handoff::Host` so it is testable without a real child. `src/ui/widgets/terminal.rs` renders and nothing else.
- **A frame is a pure function of `(store, fx, instant)`** (`src/ui/views/mod.rs:43-47`). The `vt100::Parser` lives in the store and advances only when an event arrives — never inside `draw`.
- **Bytes must set `store.dirty`**, or the pane lags the 100 ms `IDLE_FRAME` floor (`src/store.rs:32`).
- **`CommandBuilder` INHERITS this process's environment** — `CommandBuilder::new` calls `get_base_env()`, which seeds from `std::env::vars_os()` (`portable-pty-0.9.0/src/cmdbuilder.rs:74`). An earlier draft of this plan said the opposite; it was wrong, and Task A1 caught it. Inheritance is what the child wants (credentials, locale, certificates), so the five that matter — `PATH`, `HOME`, `TERM`, `QLAB_UI_PORT` (`src/net/http.rs:85`), `QLAB_BIN` (`src/handoff.rs:62`) — are set as explicit **overrides**, not as a rebuilt environment. A test asserting them must read `iter_extra_env_as_str()`, not assume an empty base: A1 had one that passed for the wrong reason until it was rewritten.
- **The keymap equivalence check** (`src/input.rs:1198`) text-scrapes each router named by `Source::region()` (`src/input.rs:207`) and asserts a per-section bidirectional multiset of `KeyCode` spellings against `KEYMAP`. A new surface needs a new `Source` (the explicit `[Source; 25]` at `src/input.rs:136` grows), a help row per key, and a router whose *comments may not spell* a `KeyCode::` variant.
- **`atlas.rs` is 2039 lines and must not become a directory** — no view is one, and it breaks `src/input.rs:217-218`.
- **The child is always `qlab cli`**, never `claude` directly (design ruling; `src/handoff.rs` holds the same rule).
- Invariant 4 (fail loud), invariant 10 (every seam needs a caller and a test that fails if reverted), invariant 8 (restart the owner after changing code it serves).
- Comment density: constraints the code cannot show, not narration. No AI-attribution trailers in commits. Commit by pathspec.
- Tests never open `.lab/registry.duckdb`; Python tests use `Registry(":memory:")`; run them with `/Users/azainmac/codebases/quant-trading-agent/.venv/bin/python -m pytest <targets> -p no:cacheprovider` and no `-q` (the repo's addopts has it).

## Rulings carried into the plan

- **Ctrl-C belongs to the child while the pane holds the keyboard.** `src/ui/shell.rs:217` makes Ctrl-C an unconditional `Command::Quit`; a terminal that cannot interrupt its child is not a terminal. The quit path stays one key away — return focus, then quit — and the pane's border says which key. This changes a rule the codebase argues for at length, so it owes a test and a comment naming the exchange.
- **Focus is `Ctrl-]` out, click or `i` in**, and the border states the current holder in both directions.
- **Quitting the workstation kills the child**, saying so; the pty dies with the client either way, and a silent orphan would be worse.
- **`/cli` opens the pane. `/build` keeps the full-screen hand-off** (`src/handoff.rs` unchanged, still tested).
- **Codex is refused by name** at the door until its own stream lands.

## File Structure

- `clients/atlas-tui/src/pty.rs` — NEW. The child's whole lifecycle behind a trait. Gated `#[cfg(feature = "operator")]`.
- `clients/atlas-tui/src/ui/widgets/terminal.rs` — NEW. Renderer only; takes `&vt100::Screen`.
- `clients/atlas-tui/src/store.rs` — the parser, the child's state, `dirty` on bytes.
- `clients/atlas-tui/src/bus.rs` — `AppEvent::Pty*` variants.
- `clients/atlas-tui/src/ui/views/atlas.rs` — the main column chooses chat or pane.
- `clients/atlas-tui/src/input.rs`, `src/ui/shell.rs` — the new `Source`, the focus keys, the Ctrl-C exchange.
- `clients/atlas-tui/src/cmd.rs` — `/cli` resolves to opening the pane.
- `clients/atlas-tui/src/ui/door.rs`, `src/ui/views/settings.rs` — the model question and its copy.
- `qlab/ui/server.py` — `chosen` on the llm payload and its save path.
- `README.md`, `docs/atlas.md`, `CLAUDE.md`, `planning-docs/2026-08-31-atlas-terminal-completion.md`.

---

### Task A1: The pty session

**Files:**
- Create: `clients/atlas-tui/src/pty.rs`
- Modify: `clients/atlas-tui/Cargo.toml` (add `portable-pty = "0.9"`, `vt100 = "0.16"`, `tui-term = "0.3"`), `clients/atlas-tui/src/main.rs` (module declaration, gated)
- Test: in-file `#[cfg(test)]` plus `clients/atlas-tui/tests/pty_session.rs`

**Interfaces:**
- Produces: `pub struct PtySession` with `pub fn open(spawn: &dyn Spawn, cols: u16, rows: u16) -> Result<PtySession, PtyError>`, `pub fn write(&self, bytes: &[u8])`, `pub fn resize(&self, cols: u16, rows: u16)`, `pub fn kill(&mut self)`; `pub trait Spawn { fn command(&self) -> CommandBuilder; }` with `DeskCli` (spawns `qlab cli`) and, in tests, a scripted `sh -c` child; `pub enum PtyEvent { Bytes(Vec<u8>), Exited { status: i32, said: String }, Failed { said: String } }`.
- Consumes: `QLAB_BIN`/`QLAB_UI_PORT` exactly as `handoff.rs:62` reads them.

- [ ] **Step 1: Write the failing test** — a scripted child (`sh -c "printf 'hello\\n'"`) yields `PtyEvent::Bytes` containing `hello` and then `Exited { status: 0, .. }`; a command that does not exist yields `Failed` whose sentence names the binary. (The controller has already proven this stack works end to end with exactly this shape — see the probe in the session scratchpad `pty-probe/`.)
- [ ] **Step 2: Run it, watch it fail** (`cargo test --test pty_session`).
- [ ] **Step 3: Implement.** `NativePtySystem::openpty` → `slave.spawn_command(builder)` → `master.try_clone_reader()` on a `std::thread` posting `PtyEvent` down an `mpsc`/bus sender; the env built explicitly (`PATH`, `HOME`, `TERM=xterm-256color`, `QLAB_UI_PORT`, `QLAB_BIN`) because `CommandBuilder` starts empty.
- [ ] **Step 4: Tests pass, both legs build** (`cargo test`, `cargo test --no-default-features` — the glass leg must not compile this module at all).
- [ ] **Step 5: Commit** `feat(atlas-tui): a pty session the desk owns`.

### Task A2: The terminal widget

**Files:**
- Create: `clients/atlas-tui/src/ui/widgets/terminal.rs`
- Modify: `clients/atlas-tui/src/ui/widgets/mod.rs`
- Test: in-file, plus a golden in `clients/atlas-tui/tests/golden_terminal.rs`

**Interfaces:**
- Consumes: `&vt100::Screen` and a `Focus`-like flag from the store.
- Produces: `pub fn draw(f: &mut Frame, area: Rect, screen: &vt100::Screen, focused: bool, said: Option<&str>)` — the pane plus a border that names the focus holder and the key that changes it.

- [ ] **Step 1: Write the failing golden** — a parser fed two known lines renders them inside the border, and the border reads `the keyboard is the desk's · i or click to give it to Claude` unfocused / `the keyboard is Claude's · ctrl-] returns it` focused.
- [ ] **Step 2: Run it, watch it fail.**
- [ ] **Step 3: Implement** with `tui_term::widget::PseudoTerminal`.
- [ ] **Step 4: Accept the goldens by name after reading them; both legs green** (the widget itself is glass-safe — it renders a screen the glass build never obtains).
- [ ] **Step 5: Commit** `feat(atlas-tui): draw a pseudoterminal on the desk`.

### Task A3: Store and bus wiring

**Files:** `clients/atlas-tui/src/bus.rs`, `clients/atlas-tui/src/store.rs`, `clients/atlas-tui/src/main.rs`; tests in `clients/atlas-tui/tests/store_pty.rs`.

**Interfaces:**
- Produces: `AppEvent::Pty(PtyEvent)`; `Store::pty_screen() -> Option<&vt100::Screen>`, `Store::pty_state() -> PtyState` (`Absent | Running | Ended { said }`), `Store::pty_focused() -> bool`.
- The parser is `vt100::Parser` held in the store, advanced ONLY in the event arm; `dirty` set on every byte batch.

- [ ] **Step 1: Failing tests** — bytes advance the screen and set `dirty`; `Exited` moves the state to `Ended` carrying a sentence that names the status and how to start another; a second `open` while `Running` is refused by name.
- [ ] **Step 2: Run, watch fail. Step 3: Implement. Step 4: Green.**
- [ ] **Step 5: Commit** `feat(atlas-tui): the child's screen lives in the store`.

### Task A4: Keys, focus, and the Ctrl-C exchange

**Files:** `clients/atlas-tui/src/input.rs` (new `Source`, `KEYMAP` rows, the `[Source; N]` bump), `clients/atlas-tui/src/ui/shell.rs` (the Ctrl-C rule), `clients/atlas-tui/src/ui/views/atlas.rs` (routing while focused); tests in `clients/atlas-tui/tests/keys_pty.rs` and the existing keymap-equivalence test.

**Carried from A1 (binding):** once the child's state is `Ended`, STOP forwarding keystrokes — `pty.rs`'s `write` to a dead child emits one sentence per keystroke, so a pane left focused after an exit would fill the desk with them. The keyboard returns to the desk when the child ends, and the border says so.

- [ ] **Step 1: Failing tests** — with the pane focused, `q`, `/`, a digit and `Ctrl-C` all reach the child as bytes and produce no `Command`; `Ctrl-]` returns focus and the next `q` quits; with the pane unfocused, every key behaves exactly as today (regression pin).
- [ ] **Step 2: Run, watch fail.**
- [ ] **Step 3: Implement.** Note in `shell.rs` why Ctrl-C is no longer unconditional and what the exchange is. Every new key owes a `KEYMAP` row and a help-overlay row (`Source::ALL` equivalence).
- [ ] **Step 4: Both legs, clippy both legs, fmt. Step 5: Commit** `feat(atlas-tui): the keyboard changes hands, and says so`.

### Task A5: ATLAS hosts the pane

**Files:** `clients/atlas-tui/src/ui/views/atlas.rs`, `clients/atlas-tui/src/cmd.rs` (`Scope::Cli` resolves to opening the pane), `clients/atlas-tui/src/dispatch.rs` or `main.rs` (the open/kill call sites); goldens.

**From A4 (binding, and A4 calls it the likeliest real bug left):** `AtlasView::typing()` is unchanged, so a half-typed ask row survives `/cli` — the row is still "typing" while the pane is drawn over it, and printable keys are swallowed into a row nobody can see. Opening the pane must settle the ask row (clear it, or take it out of `typing()` while a pane is up), and a test must pin that a partly-typed ask row does not eat the pane's keys. Also note `Ctrl-]` arrives as `KeyCode::Char('5')` + CONTROL on this client (crossterm's legacy C0 mapping); A4 accepts both spellings — do not "simplify" that to one.

**From A3's review (binding):** `open_pty` uses `tokio::spawn` and therefore PANICS rather than refusing if called outside the runtime (`store.rs:1006`). Every call site you add must be on the runtime's own loop; if you ever need one that might not be, it owes a sentence instead of a panic (invariant 4).

**Resize (ruled, binding).** `pty_resize` deliberately cannot be called from `draw`, and the pane's size is only known there. Use this client's own idiom: the pane records its INNER rect into a `Cell<Rect>` during draw exactly as `atlas.rs` already does for `input_row` (`:92`) and `book_word` (`:137`); after the frame the runtime compares it with what the pty was last told and calls `store.pty_resize(cols, rows)` only when it differs. One source of truth for the geometry — the layout code — and no mutation of the store inside a draw. A terminal resize needs no special case: the next frame reports the new rect.

**Leaving a dead pane (ruled, binding).** `Ended` keeps the pane up and only `close_pty` removes it, so the operator owes a way out: `/cli` on an `Ended` pane restarts in place (close, then open), and one key — free in `input::KEYMAP`, owed a help row — closes the pane and gives the column back to the chat. **Exception to "A2 owns the border copy":** A5 writes the one new border line for the `Ended` state naming that key, because it is a new state's sentence rather than a rewording of A2's two. A retry that fails REPLACES the pane's sentence rather than adding a second: one frame, one story about one child.

**Geometry contract from A2 (binding, and NOT written on `draw` itself until A2's fix round lands):** `draw(f, area, screen, focused, said)` takes `area` as the WHOLE pane including its border; `tui-term` renders into `block.inner(area)`, so the pty must be sized to `(w-2, h-2)` — tell `PtySession::resize` the inner rect, never the outer, or Claude wraps its output to a geometry it was never given. `said` is `Some` exactly when no child is live, and `focused && said.is_some()` must never be produced. A2's own comments still justify a short border form by "the column is 45 cells"; that reason is stale (the settled pane is 77) — the short forms are the narrow-TERMINAL case, needed below width 64 (desk form) and 48 (child form).

**Layout (measured, binding):** at 120×36 ATLAS is rail 8 · chat 45 · WOULD DO 32 · sidebar 33. While a child runs the pane spans chat + WOULD DO (77 columns at 120, 117 at 160) and the sidebar stays; below 60 columns for the pane the sidebar is dropped too and the pane takes the full content width; below `terminal::MIN_W` the widget's own refusal stands. Render at 120×36 and READ the frame — do not only assert on it.

- [ ] **Step 1: Failing tests** — while a child is running the ATLAS main column is the pane and the sidebar still draws the proposal card and the your-call pointers; with no child the column is the chat and its golden is byte-identical to today's; `/cli` on a local-reasoner desk is refused by name (`qlab cli` is a Claude verb) and on an unarmed desk exactly as today; the glass build offers neither the word nor the pane.
- [ ] **Step 2–4: red → green, goldens read and accepted, both legs.**
- [ ] **Step 5: Commit** `feat(atlas-tui): Claude runs in the tab, beside the desk`.

### Task B1: The owner can say nobody chose

**Files:** `qlab/ui/server.py` (`startup_llm_config`, `llm_payload`, the `POST /api/llm` save path), `tests/test_ui.py`.

- [ ] **Step 1: Failing tests** — a desk with no `llm_config.json` serves `chosen: false` and the default's values; a desk whose config was written by the door serves `chosen: true`; an existing file with no flag reads as chosen (migration: a config that exists was written by somebody).
- [ ] **Step 2–4: red → green.** Correct the standing comment at the collapse site rather than leaving it contradicting the code.
- [ ] **Step 5: Commit** `feat(desk): a mind nobody chose is not a mind the desk chose`.

### Task B2: The door asks about the mind

**Files:** `clients/atlas-tui/src/store.rs` (`desk_unchosen` gains the model's), `clients/atlas-tui/src/ui/door.rs` (`Door::wanted`, `Step::Model` reachable alone, Codex refused by name), `clients/atlas-tui/src/model.rs`; goldens.

- [ ] **Step 1: Failing tests** — a desk with a chosen mode and an unchosen mind opens the door straight at `Step::Model`; answering it retires the door; a chosen mind opens no door; Codex is listed and refuses with the reason.
- [ ] **Step 2–5** as above. Commit `feat(atlas-tui): the door asks which mind runs Atlas`.

### Task B3: The copy that is now wrong

**Files:** `clients/atlas-tui/src/ui/views/settings.rs` (`handoff_note` at `:4537` says `/cli, /build: claude, not <backend>` — `/cli` is a pane now), the MODELS card's line about what ATLAS looks like; goldens.

- [ ] One commit, `fix(atlas-tui): the models card says what the tab will be`.

### Task C1: Docs, record, suites, build

**Files and the exact sentences that become false** (verified at `51e8528`):
- `README.md:411-417` — "`qlab cli` opens the real Claude CLI … The workstation spells both as `/cli` and `/build`." After this stream `/cli` opens a PANE inside ATLAS while `qlab cli` (the terminal verb) and `/build` still take the whole screen. Rewrite the last sentence and say what the pane is.
- `docs/atlas.md:136-143` — "`/cli` opens the real Claude CLI … Both spawn a child that owns the terminal until it exits". The "both" is now false for `/cli`. State the split: `/cli` runs the child in the tab beside the desk sidebar; `/build` keeps the whole terminal.
- `CLAUDE.md:89-93` — "The workstation's `/cli` and `/build` spawn those verbs (`clients/atlas-tui/src/handoff.rs`): leave the alternate screen …". `/cli` no longer leaves the screen; name `src/pty.rs` beside `handoff.rs` and say which verb takes which path. Also add the by-construction line: the monitoring build contains no pty, no spawn and no forwarded keystroke (`nm` counts, and note the redirected-`CARGO_TARGET_DIR` gotcha below).
- `README.md:238` — "ten views on `1`–`9` and `0`" stays true; check the ATLAS row's description mentions the pane.
- `docs/atlas.md:69` — the rights table row for `build`; check the `web`/`workflows` rows still read true beside the pane.
- NEW: `planning-docs/2026-08-31-atlas-terminal-completion.md`.

**Verification gotcha (from A3's review):** `nm target/debug/atlas` returns a FALSE 0/0/0 when `CARGO_TARGET_DIR` is redirected — the relative path does not resolve. Use the absolute path when re-checking the by-absence claim.

**Live checks this stream owes and has never run** (name them in the record honestly): `qlab cli` has never actually run in the pane — every test child is a scripted `sh`; no wide-character or SGR fixture exists anywhere; a child's DECSCUSR cursor shape cannot reach the pane at all (tui-term's vt100 adapter has no `cursor_shape` override and `vt100 0.16` exposes no API), so a bar cursor renders as a block.

- [ ] Full `python -m pytest`; both cargo legs; clippy both legs; `cargo fmt --check`; `cargo build --release`. Exact counts in the record, plus what has NOT run live (a real Claude CLI inside the pane on this machine) and every follow-up.
- [ ] Commit `docs: the mind on the desk`.
