# The Armed Desk — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The operator arms the desk once and the desk remembers; while a governed workflow runs, the desk shows what the agent is actually doing and says so honestly when it hears nothing.

**Architecture:** Posture becomes owner-persisted state (`state_path("posture.json")`, mirroring `qlab/core/desk_mode.py`), served on `/api/tui`, answered once by the startup door, and obeyed by the Rust client from the first snapshot rather than from its arguments — `--operator` retires, `--glass` survives as a session override, and the cargo default flips so the daily build is the armed one. Separately, `atlas_coordinator_event` (already durable, redacted, and streaming to the client) joins the WORKFORCE console filter, and the active phase node grows a liveness line derived from real event recency, with silence reported rather than animated.

**Tech stack:** Python owner (stdlib, `Registry(":memory:")` tests), Rust client (ratatui 0.30, insta goldens, the established picker/door/console patterns).

**Spec:** `planning-docs/2026-08-06-armed-desk-design.md` — read it first.

## Global Constraints

- CLAUDE.md invariants bind: one DuckDB writer (owner only); tests fully offline (`Registry(":memory:")`, no live daemon/broker/network); fail loud, never a silent fallback; paths via `qlab/paths.py`; the owner is threaded — shared mutable state needs its lock; anything reachable needs a caller and a test; comments state constraints, not narration.
- Commit style: imperative, conventional prefix + scope, **no AI-attribution trailers**.
- Rust client rules: time-as-data (no clock reads in render/decision paths — `now` arrives as a parameter); theme tokens only, no literal colors outside `theme.rs`; `Some("")` renders as absent; guard on the **allocated** rect, never a parent; views never perform IO (they return `Command`s); every claimed key owes a `KEYMAP` row.
- **Standing test rules** (earned on the previous two streams, they bind here): after adding a guard, delete it and confirm a test fails — a guard nobody has removed is a guard nobody has tested; a guard whose condition is a comparison needs a case on **each side** of it, and a compound condition needs a case **per conjunct**; pin at the route/rendered surface, not at the constructor; credentials and foreign strings pass one boundary gate, never per-call-site reasoning.
- **Testing is lean and fast** (user directive): load-bearing pins plus one inversion, no permutation breadth; mocked clocks, no sleeps; focused module runs while iterating; the full suite once per task before the final commit; reviewers spot-run touched modules only.
- Both Rust matrix legs must be green each task. **Note the legs change meaning in Task 2:** after the default flips, `cargo test` is the *armed* leg and `cargo test --no-default-features` is the *glass* leg. Task 1 uses the old meaning; Tasks 2–4 use the new one and must state which they ran.
- `.venv/bin/python -m pytest` (the worktree has no venv of its own; the main checkout's venv is on PATH-adjacent — invoke it by absolute path).

---

### Task 1: The owner remembers a posture

**Files:**
- Create: `qlab/core/posture.py`
- Modify: `qlab/ui/server.py` (payload + route + snapshot block)
- Test: `tests/test_posture.py`; extend `tests/test_ui.py`

**Interfaces:**
- Produces: `Posture(armed: bool)` frozen dataclass; `DEFAULT_POSTURE = Posture(armed=False)`; `load_posture() -> Posture | None` (None = never chosen, the `desk_mode.load_desk_mode` rule: an unreadable or unrecognised file is "not chosen yet", never an error); `save_posture(p: Posture) -> None` at `state_path("posture.json")`.
- Produces: `OwnerSession.posture_payload() -> {"armed": bool, "chosen": bool}` and `OwnerSession.set_posture(armed: bool) -> dict` (persists, records an audit event `desk.posture_chosen` with `{armed}`, returns the payload).
- Produces: route `POST /api/desk/posture` body `{"armed": <bool>}` → 200 `posture_payload()`; a non-boolean `armed` → 400 with a sentence naming the accepted values (the `replace must be true or false` precedent). `GET`-side: `posture` block on `/api/tui`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_posture.py
from qlab.core.posture import DEFAULT_POSTURE, Posture, load_posture, save_posture

def test_a_desk_never_asked_has_not_chosen(tmp_state):
    assert load_posture() is None          # absence, not an error
    assert DEFAULT_POSTURE.armed is False  # the safe answer is the default

def test_a_choice_survives_the_owner(tmp_state):
    save_posture(Posture(armed=True))
    assert load_posture() == Posture(armed=True)

def test_an_unreadable_file_reads_as_not_chosen(tmp_state):
    from qlab.paths import state_path
    state_path("posture.json").write_text("{not json")
    assert load_posture() is None          # never an exception
```

```python
# tests/test_ui.py (append)
def test_the_posture_route_records_and_reflows(owner):
    body = owner.post("/api/desk/posture", {"armed": True})
    assert body == {"armed": True, "chosen": True}
    assert owner.snapshot()["posture"] == {"armed": True, "chosen": True}
    kinds = [e["kind"] for e in owner.registry.read_events(20)]
    assert "desk.posture_chosen" in kinds

def test_a_posture_that_is_not_a_boolean_is_refused(owner):
    status, body = owner.post_raw("/api/desk/posture", {"armed": "yes"})
    assert status == 400 and "true or false" in body["error"]
```

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv/bin/python -m pytest tests/test_posture.py -x -q`
Expected: FAIL — `ModuleNotFoundError: qlab.core.posture`.

- [ ] **Step 3: Write `qlab/core/posture.py`**

Mirror `qlab/core/desk_mode.py` exactly in shape and philosophy: frozen dataclass, `state_path` for the file, a docstring stating that posture is **explicit, never inferred** — a desk is armed because an operator said so, not because a binary could write. Absence and corruption both return `None`.

- [ ] **Step 4: Wire the owner**

`posture_payload()` computes `chosen` where the load resolves (`bool(load_posture())`), returns `{"armed": p.armed if p else False, "chosen": ...}`. `set_posture` persists **then** assigns any in-memory copy, then records the event (the C1 ordering rule: a failed write must not leave memory ahead of disk). Add the `posture` block to the `/api/tui` snapshot beside `desk_mode`. Route in the existing dispatch style; validate `armed` is a real `bool` (`"yes"`, `1`, `[]` all refuse) before the try.

- [ ] **Step 5: Green, then the guard proof**

Run: `.venv/bin/python -m pytest tests/test_posture.py tests/test_ui.py -q` → PASS.
Then delete the boolean validation and confirm exactly the refusal test fails; restore.

- [ ] **Step 6: Full suite once, then commit**

```bash
.venv/bin/python -m pytest -q
git add qlab/core/posture.py qlab/ui/server.py tests/test_posture.py tests/test_ui.py
git commit -m "feat(desk): posture is a choice the owner remembers, not a flag the operator repeats"
```

---

### Task 2: The client obeys the desk, not its arguments

**Files:**
- Modify: `clients/atlas-tui/Cargo.toml` (default features + the comment), `clients/atlas-tui/src/main.rs:126-161` (posture derivation, flags), `clients/atlas-tui/src/model.rs` (+`PostureBlock`), `clients/atlas-tui/src/store.rs` (accessor)
- Modify: `CLAUDE.md` (invariant 3's client sentence)
- Test: `clients/atlas-tui/tests/operator_gate.rs`, `clients/atlas-tui/src/store.rs` unit tests

**Interfaces:**
- Consumes: Task 1's `posture` block `{armed: bool, chosen: bool}` on `/api/tui`.
- Produces: `model::PostureBlock { armed: Option<bool>, chosen: Option<bool> }` (all-`Option`, `null_or_default` discipline); `Store::posture_armed(&self) -> Option<bool>`; `Store::posture_chosen(&self) -> Option<bool>`.
- Produces: `Posture::from_desk(featured: bool, forced_glass: bool, armed: Option<bool>) -> Posture` — a pure function, the single place the decision is made. `Operator` iff `featured && !forced_glass && armed == Some(true)`; everything else is `Glass`.

- [ ] **Step 1: Write the failing tests**

```rust
// store.rs unit tests — the decision table, both sides of every conjunct
#[test]
fn a_desk_arms_only_when_the_binary_can_and_the_operator_said_so() {
    use Posture::*;
    assert_eq!(Posture::from_desk(true,  false, Some(true)),  Operator);
    assert_eq!(Posture::from_desk(true,  false, Some(false)), Glass); // owner says no
    assert_eq!(Posture::from_desk(true,  false, None),        Glass); // never asked
    assert_eq!(Posture::from_desk(true,  true,  Some(true)),  Glass); // --glass wins
    assert_eq!(Posture::from_desk(false, false, Some(true)),  Glass); // cargo gate wins
}
```

```rust
// operator_gate.rs — the rendered consequence, not just the enum
#[test]
fn an_unarmed_desk_offers_nothing_even_in_a_capable_binary() {
    let store = store_with_posture(Some(false));
    let frame = frame_to_string(&store, 120, 36);
    assert!(frame.contains("GLASS"));
    assert!(!frame.contains("/mode"), "an unarmed desk must not advertise a write scope");
}
```

- [ ] **Step 2: Run and watch them fail**

Run: `cd clients/atlas-tui && cargo test from_desk` → FAIL (`no function from_desk`).

- [ ] **Step 3: Implement**

`from_desk` in `store.rs` beside the `Posture` enum, with the comment stating the conjunct order and why the cargo gate is outermost. In `main.rs`: drop `--operator` parsing; keep `--glass`; posture is no longer computed at startup — the client starts `Glass` and re-derives once per snapshot application from `store.posture_armed()`. Keep the writer construction where it is (fallible, before the screen); an unarmed desk simply never dispatches.

- [ ] **Step 4: Flip the default and rewrite the two texts**

`Cargo.toml`: `default = ["operator"]`. Rewrite the feature comment to say the by-construction guarantee now describes the `--no-default-features` artifact. Rewrite CLAUDE.md invariant 3's client sentence to match — name the confirm modal, the referee pin, and owner-side validation as the armed desk's actual protections.

- [ ] **Step 5: Green on both legs, then the guard proof**

Run: `cargo test` (armed leg) and `cargo test --no-default-features` (glass leg).
Then mutate `from_desk` to drop the `featured` conjunct and confirm the cargo-gate case fails; restore. Do the same for `forced_glass`.

- [ ] **Step 6: Commit**

```bash
git add clients/atlas-tui/Cargo.toml clients/atlas-tui/src CLAUDE.md
git commit -m "feat(atlas): the desk arms because the operator said so, and the docs say what still guarantees what"
```

---

### Task 3: The door's fourth question

**Files:**
- Modify: `clients/atlas-tui/src/ui/door.rs` (a posture step), `clients/atlas-tui/src/cmd.rs` (+`Command::Posture`), `clients/atlas-tui/src/net/write.rs` (+`set_posture`), `clients/atlas-tui/src/dispatch.rs` (routing)
- Test: `clients/atlas-tui/tests/golden_door.rs`, `clients/atlas-tui/tests/operator_gate.rs`

**Interfaces:**
- Consumes: `Store::posture_chosen()`; `Posture::from_desk`.
- Produces: `WriteClient::set_posture(armed: bool) -> Wrote` → `POST /api/desk/posture`; `Command::Posture { armed: bool }`; a door step rendered only when `posture_chosen() != Some(true)`.

- [ ] **Step 1: Write the failing tests**

```rust
#[test]
fn a_desk_never_asked_about_posture_is_asked_once() {
    let mut door = door_for(store_with_posture_chosen(false));
    let frame = draw(&door, 120, 36);
    assert!(frame.contains("ARM THIS DESK"));
    assert!(frame.contains("read-only"), "the safe answer is named, not implied");
}

#[test]
fn escape_leaves_the_desk_read_only() {
    let mut door = door_for(store_with_posture_chosen(false));
    assert_eq!(door.on_key(esc()), Some(Command::Posture { armed: false }));
}

#[test]
fn a_desk_that_answered_is_not_asked_again() {
    let door = door_for(store_with_posture_chosen(true));
    assert!(!draw(&door, 120, 36).contains("ARM THIS DESK"));
}
```

- [ ] **Step 2: Run and watch them fail** — `cargo test --test golden_door posture` → FAIL.

- [ ] **Step 3: Implement the step**

Place it after the model step and before credentials (arming is about *this client*; credentials are about the book). Two rows — `ARMED` / `READ-ONLY` — with a line stating what each means in one sentence: armed offers approvals, plan execution behind the confirm box, and `/mode`; read-only shows the same desk and writes nothing. Esc = read-only, matching the door's existing safe-answer rule. On accept, emit `Command::Posture`; the runtime dispatches, the owner records, the next snapshot re-derives the posture — no client-side latch.

- [ ] **Step 4: Green + guard proof**

`cargo test` both legs. Then mutate the `posture_chosen() != Some(true)` condition to always-false and confirm the ask-once test fails; restore. (The comparison rule: a case on each side already exists — chosen true and chosen false.)

- [ ] **Step 5: Commit**

```bash
git add clients/atlas-tui/src clients/atlas-tui/tests
git commit -m "feat(atlas): the door asks once whether this desk is armed"
```

---

### Task 4: The desk shows its work

**Files:**
- Modify: `clients/atlas-tui/src/ui/views/workforce.rs` (`CONSOLE_KINDS`, the phase node's activity line), `clients/atlas-tui/src/store.rs` (newest-coordinator-event accessor)
- Test: `clients/atlas-tui/tests/golden_workforce.rs` (or the existing workforce golden module), `store.rs` unit tests

**Interfaces:**
- Consumes: SSE `atlas_coordinator_event` rows already in the events ring; `coordinator_status().driving`.
- Produces: `Store::last_agent_event_at(&self) -> Option<Instant>` (arrival-stamped like `last_snapshot_at`); `workforce::activity_line(driving: bool, last: Option<Instant>, now: Instant) -> Option<String>` — a pure function: `None` when not driving; `Some("spoke 3s ago")` under the silence threshold; `Some("no word for 47s")` at or past it.

- [ ] **Step 1: Write the failing tests**

```rust
#[test]
fn silence_is_reported_rather_than_animated() {
    let t0 = Instant::now();
    assert_eq!(activity_line(false, Some(t0), t0), None);          // parked says nothing
    assert_eq!(activity_line(true, Some(t0), t0 + secs(3)).unwrap(), "spoke 3s ago");
    assert_eq!(activity_line(true, Some(t0), t0 + secs(47)).unwrap(), "no word for 47s");
    // both sides of the threshold, per the comparison rule
    assert!(activity_line(true, Some(t0), t0 + secs(44)).unwrap().starts_with("spoke"));
    assert!(activity_line(true, Some(t0), t0 + secs(45)).unwrap().starts_with("no word"));
}

#[test]
fn the_console_carries_the_agents_own_words() {
    let store = store_with_agent_event("moments-analyst", "calling moments_estimate");
    let frame = frame_to_string(&store, 120, 36);
    assert!(frame.contains("moments-analyst"));
    assert!(frame.contains("calling moments_estimate"));
}
```

- [ ] **Step 2: Run and watch them fail** — `cargo test activity_line` → FAIL.

- [ ] **Step 3: Implement**

Add `atlas_coordinator_event` to `CONSOLE_KINDS`; render its `agent` and `text` with kind-derived tone (`error` negative, `tool_start` accent, `text`/`result` primary) reusing the existing `event_row` widget and its flash-on-arrival. `SILENCE_AFTER: Duration = 45s` as a named constant with the reason on it. Stamp `last_agent_event_at` where SSE events land in `Store::apply` (`now` is already threaded there). Draw the line under the active phase node.

- [ ] **Step 4: Green + guard proof + goldens**

`cargo test` both legs; accept goldens after eyeballing (the console gains lines — say what changed in the report). Mutate the threshold comparison from `>=` to `>` and confirm the boundary case fails; restore.

- [ ] **Step 5: Full suite, then commit**

```bash
cargo test && cargo test --no-default-features
git add clients/atlas-tui/src clients/atlas-tui/tests
git commit -m "feat(atlas): the console carries the agent's own words, and silence says so"
```

---

## Self-review notes

- **Spec coverage:** A1's persistence/route/payload → Task 1; client derivation, default flip, doc rewrites → Task 2; the door's question → Task 3; transcript + activity + silence → Task 4. The spec's "suppressed kinds stay suppressed" is a *non*-change, enforced by not touching `_RECORDED_KINDS` — called out here so no implementer "helpfully" re-adds them.
- **Type consistency:** `posture_payload()`/`{armed, chosen}` (Task 1) is what `PostureBlock` decodes (Task 2), what `posture_chosen()` reads (Task 3), and what `Command::Posture` writes back (Task 3). `activity_line`'s signature (Task 4) takes `now` as data, per the global constraint.
- **Known risk:** Task 2 changes what the two cargo legs *mean*. Every later task must name which legs it ran, or a "both legs green" claim is ambiguous.
