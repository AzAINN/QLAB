# Atlas Acts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Asking Atlas what needs doing returns ranked, gate-checked actionables; approving one starts the governed workflow and Atlas actually drives it to completion.

**Architecture:** Proposals reuse the existing `atlas_tasks` lifecycle rather than
introducing a second one — one new `origin` column separates a proposal (attended,
requires approval) from a trigger-created task (unattended, started by the
heartbeat). The authority path is untouched: `check_startable` is asked at
proposal time and asked again by `start_task` at execution time, and approval
routes through the client's single write chokepoint. Nothing here widens what any
mode permits.

**Tech Stack:** Python 3.13 (owner: DuckDB registry, stdlib HTTP), Rust (ratatui
0.30 client, insta goldens), pytest + cargo test.

## Global Constraints

- One DuckDB writer: the owner HTTP runtime. Nothing else opens `.lab/registry.duckdb`.
- Tests never open `.lab/registry.duckdb` — use `Registry(":memory:")`. Tests pass fully offline.
- Fail loud. No silent fallbacks. Absence is not an error; `Some("")` is absent.
- Time-as-data: never read the clock inside render or decision code; pass `now` in.
- Resolve files through `qlab/paths.py`.
- The owner is threaded (`ThreadingHTTPServer` + heartbeat): shared mutable state needs a lock.
- Invariant 10: anything reachable needs a real caller and a test that exercises it through that caller.
- After adding a guard, delete it and confirm a test fails, then restore. A comparison guard needs a case per side; a compound condition a case per conjunct.
- Pin behaviour at the route / rendered surface, not the constructor.
- No new execution path. Approval reuses `POST /api/atlas/tasks/<id>/start`.
- Rust: `cargo test` is the ARMED leg, `cargo test --no-default-features` the GLASS leg. Both must be green, plus clippy and `cargo fmt --check`.
- Run pytest as `PYTHONPATH=$PWD /Users/azainmac/codebases/quant-trading-agent/.venv/bin/python -m pytest` (the venv's editable install resolves to the main checkout).
- Commit messages: imperative, conventional prefix + scope. No AI-attribution trailers.
- Testing is lean and fast: the tests each task names, plus what is needed to witness a new guard.

---

### Task 1: The template menu that shows its refusals

**Files:**
- Modify: `qlab/operator/templates.py` (add `template_menu`, re-derive `startable_templates`)
- Test: `tests/test_atlas_templates.py` (create if absent; otherwise the module that already tests `startable_templates` — grep for it first)

**Interfaces:**
- Consumes: `check_startable(template_id, mode, facts) -> WorkflowTemplate`, `TEMPLATES`, `TemplateNotAllowed` (all existing in this module).
- Produces: `template_menu(mode: str, facts: dict) -> list[dict]` — one entry per registered template, in `TEMPLATES` order, each `{"template_id": str, "purpose": str, "startable": bool, "reason": str | None, "creates_plan": bool, "needs_coordinator": bool}`. `reason` is the refusal sentence when `startable` is False and `None` when it is True.

- [ ] **Step 1: Write the failing test**

```python
def test_the_menu_carries_the_refusal_rather_than_dropping_the_template():
    """A desk that silently omits what it will not do teaches nothing about why."""
    from qlab.operator.templates import template_menu

    menu = template_menu("research", {"data": {"blocked": True}})
    by_id = {entry["template_id"]: entry for entry in menu}
    assert by_id["regime_review"]["startable"] is False
    assert "data plane is blocked" in by_id["regime_review"]["reason"]
    # desk_brief requires nothing and creates no plan, so a blocked data plane
    # does not reach it.
    assert by_id["desk_brief"]["startable"] is True
    assert by_id["desk_brief"]["reason"] is None


def test_startable_templates_is_the_menus_permitted_half():
    """One authority, not two. If these ever disagree the gate has forked."""
    from qlab.operator.templates import startable_templates, template_menu

    facts = {"data": {"blocked": False}}
    for mode in ("observe", "research", "propose", "paused"):
        menu = template_menu(mode, facts)
        assert startable_templates(mode, facts) == {
            entry["template_id"]: entry["purpose"]
            for entry in menu if entry["startable"]
        }
```

- [ ] **Step 2: Run it to verify it fails**

Run: `PYTHONPATH=$PWD .venv/bin/python -m pytest tests/test_atlas_templates.py -q`
Expected: FAIL with `ImportError: cannot import name 'template_menu'`

- [ ] **Step 3: Write the implementation**

```python
def template_menu(mode: str, facts: dict) -> list[dict]:
    """Every registered template with the gate's verdict on it, refusals included.

    The refusal is the product here. `startable_templates` answers "what may
    run", which is what a menu needs; an operator asking "what should I do"
    also needs to know why the other things are not on offer, and inferring it
    from an absence is how a mode mistake reads as an empty desk.
    """
    out: list[dict] = []
    for template_id, template in TEMPLATES.items():
        entry = {"template_id": template_id, "purpose": template.purpose,
                 "creates_plan": template.creates_plan,
                 "needs_coordinator": template.needs_coordinator}
        try:
            check_startable(template_id, mode, facts)
        except TemplateNotAllowed as exc:
            entry.update({"startable": False, "reason": str(exc)})
        else:
            entry.update({"startable": True, "reason": None})
        out.append(entry)
    return out


def startable_templates(mode: str, facts: dict) -> dict[str, str]:
    """Every registered template Atlas may start right now → its purpose.

    Derived from `template_menu`'s permitted half, which is itself derived from
    `check_startable`. A second list of what is permitted would be a second
    authority, and the two would disagree the first time a template's
    requirements changed.
    """
    return {entry["template_id"]: entry["purpose"]
            for entry in template_menu(mode, facts) if entry["startable"]}
```

Keep the existing `startable_templates` docstring's reasoning; the body is what changes.

- [ ] **Step 4: Run the tests**

Run: `PYTHONPATH=$PWD .venv/bin/python -m pytest tests/test_atlas_templates.py -q` and the module that already covers `startable_templates` (grep: `git grep -l startable_templates tests/`)
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add qlab/operator/templates.py tests/
git commit -m "feat(operator): the template menu carries the refusals, not just the permits"
```

---

### Task 2: A proposal is a task with a different origin

**Files:**
- Modify: `qlab/state/registry.py` (`_SCHEMA` `atlas_tasks`, the ALTER block near line 365, `create_atlas_task`)
- Modify: `qlab/ui/server.py` (`atlas_run_startable`)
- Test: `tests/test_ui.py`, `tests/test_atlas.py` (whichever already covers `atlas_run_startable` / `create_atlas_task` — grep first)

**Interfaces:**
- Consumes: `Registry.create_atlas_task(task_id, dedupe_key, trigger_kind, trigger_payload, template_id) -> bool`.
- Produces: the same function with a new keyword-only parameter `origin: str = "trigger"`, persisted to a new `origin VARCHAR` column; `list_atlas_tasks` rows carry `origin` (they `SELECT *`). `UISession.atlas_run_startable` starts only `origin == "trigger"` tasks.

- [ ] **Step 1: Write the failing test**

```python
def test_the_heartbeat_never_starts_a_proposal(session):
    """The envelope, in one test. A proposal is attended by construction:
    the operator approves it or it does not run. If the heartbeat could start
    one, the approval gate would be decorative on arrival."""
    session.registry.create_atlas_task(
        "task-proposal", "regime_review|2026-08-06|SPY|abc", "operator_asked",
        {"why": "asked"}, "regime_review", origin="proposal")
    session.atlas.set_mode("research")

    started = session.atlas_run_startable(True, limit=5)

    assert started == []
    assert session.registry.get_atlas_task("task-proposal")["status"] == "queued"


def test_the_heartbeat_still_starts_a_trigger_task(session):
    """The other side of the same guard: this project did not turn autonomy off."""
    session.registry.create_atlas_task(
        "task-trigger", "regime_shift|2026-08-06|SPY|abc", "regime_shift",
        {"why": "regime"}, "regime_review")
    session.atlas.set_mode("research")

    started = session.atlas_run_startable(True, limit=5)

    assert [entry["task_id"] for entry in started] == ["task-trigger"]


def test_a_task_written_before_this_column_reads_as_a_trigger(session):
    """An existing dev DB's rows are NULL here, and they are all trigger work.
    Reading NULL as a proposal would silently stop the desk's own autonomy."""
    session.registry.con.execute(
        "INSERT INTO atlas_tasks (task_id, dedupe_key, trigger_kind, "
        "trigger_payload, template_id, status, attempt_count, created_at, "
        "updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ["task-old", "regime_shift|2026-08-06|SPY|old", "regime_shift", "{}",
         "regime_review", "queued", 0, "2026-08-06T00:00:00Z",
         "2026-08-06T00:00:00Z"])
    session.atlas.set_mode("research")

    started = session.atlas_run_startable(True, limit=5)

    assert [entry["task_id"] for entry in started] == ["task-old"]
```

Use whatever `session` fixture the target test module already provides, and match its
existing helpers for setting facts/mode. If the fixture's default mode is not
`research`, set it as shown.

- [ ] **Step 2: Run it to verify it fails**

Run: `PYTHONPATH=$PWD .venv/bin/python -m pytest tests/test_ui.py -q -k heartbeat_never_starts_a_proposal`
Expected: FAIL with `TypeError: create_atlas_task() got an unexpected keyword argument 'origin'`

- [ ] **Step 3: Write the implementation**

In `_SCHEMA`, add `origin VARCHAR` to the `atlas_tasks` column list. Beside the
existing ALTERs (near line 365) add:

```python
        # Proposals and triggers share one lifecycle and one gate; only who may
        # start them differs. NULL is a row written before this column existed,
        # and every one of those is trigger work.
        self.con.execute(
            "ALTER TABLE atlas_tasks ADD COLUMN IF NOT EXISTS origin VARCHAR")
```

In `create_atlas_task`, add the parameter and persist it:

```python
    def create_atlas_task(self, task_id: str, dedupe_key: str, trigger_kind: str,
                        trigger_payload: dict, template_id: str | None,
                        *, origin: str = "trigger") -> bool:
```

Add `origin` to the INSERT's column list and `origin` to its values, keeping the
placeholder count in step.

In `qlab/ui/server.py`'s `atlas_run_startable`, inside the candidate loop:

```python
            if candidate.get("origin") != "trigger":
                # A proposal is started by the operator approving it, never by
                # the beat. This line IS the envelope.
                continue
```

`startable_tasks` must carry `origin` through so this can be read — in
`qlab/operator/atlas.py`'s `startable_tasks`, add to the entry:

```python
            entry = {"task_id": task["task_id"], "template_id": template_id,
                     # NULL is a pre-column row, and those are all trigger work.
                     "origin": str(task.get("origin") or "trigger")}
```

- [ ] **Step 4: Run the tests**

Run: `PYTHONPATH=$PWD .venv/bin/python -m pytest tests/test_ui.py tests/test_atlas.py -q`
Expected: PASS

- [ ] **Step 5: Prove the guard**

Delete the `origin != "trigger"` continue, re-run, and confirm
`test_the_heartbeat_never_starts_a_proposal` fails. Restore it. Record the
before/after in your report.

- [ ] **Step 6: Commit**

```bash
git add qlab/state/registry.py qlab/ui/server.py qlab/operator/atlas.py tests/
git commit -m "feat(operator): a proposal is a task the beat will not start"
```

---

### Task 3: Asking Atlas yields a ranked list of actionables

**Files:**
- Modify: `qlab/ui/server.py` (new `UISession.atlas_actionables`, route, `/api/tui` block, route table in the module docstring)
- Test: `tests/test_ui.py`

**Interfaces:**
- Consumes: `template_menu(mode, facts)` (Task 1), `Registry.create_atlas_task(..., origin="proposal")` (Task 2), `session.atlas_facts(offline)`, `session.atlas.mode`, `AtlasSupervisor._dedupe_key`-shaped keys.
- Produces:
  - `UISession.atlas_actionables(offline: bool) -> dict` returning
    `{"trading_date": str, "items": [{"template_id", "purpose", "startable", "reason", "creates_plan", "task_id"}]}`.
    `task_id` is present only on startable items (a refused item has nothing to approve) and is the id of a `proposal`-origin queued task.
  - Route `POST /api/atlas/actionables` → 200 with that payload.
  - `/api/tui` gains an `actionables` block carrying the same `items` list for the newest proposal set, so the client renders without a second fetch.

**The dedupe key must keep its shape.** `AtlasSupervisor._task_age` parses the
trading date out of `kind|trading_date|universe|state_hash`; a proposal minted
any other way reads as *age unknown* and `startable_tasks` refuses it — the very
gate meant to permit it. Build it the same way, with `kind = f"proposal:{template_id}"`.

- [ ] **Step 1: Write the failing test**

```python
def test_asking_for_actionables_lists_refusals_beside_the_offers(session):
    session.atlas.set_mode("research")
    payload = session.atlas_actionables(True)
    by_id = {item["template_id"]: item for item in payload["items"]}
    # Every registered template is represented, permitted or not.
    assert by_id
    refused = [item for item in payload["items"] if not item["startable"]]
    assert all(item["reason"] for item in refused)
    assert all(item.get("task_id") is None for item in refused)


def test_a_startable_actionable_becomes_a_proposal_task(session):
    session.atlas.set_mode("research")
    payload = session.atlas_actionables(True)
    offered = [item for item in payload["items"] if item["startable"]]
    assert offered, "research mode offers at least one template"
    task = session.registry.get_atlas_task(offered[0]["task_id"])
    assert task["origin"] == "proposal"
    assert task["status"] == "queued"
    assert task["template_id"] == offered[0]["template_id"]


def test_asking_twice_on_one_day_proposes_once(session):
    """Same question, same facts, same day — one proposal, not two. The dedupe
    key is the existing shape, so `_task_age` can still read the date out of it."""
    session.atlas.set_mode("research")
    first = session.atlas_actionables(True)
    second = session.atlas_actionables(True)
    ids = lambda p: sorted(i["task_id"] for i in p["items"] if i["startable"])
    assert ids(first) == ids(second)
    assert len(session.registry.list_atlas_tasks(200)) == len(ids(first))


def test_a_proposal_is_startable_rather_than_stale(session):
    """The trap this dedupe shape exists to avoid: a key `_task_age` cannot
    parse reads as age-unknown, and an age-unknown task is refused."""
    session.atlas.set_mode("research")
    payload = session.atlas_actionables(True)
    offered = [i for i in payload["items"] if i["startable"]][0]
    facts = session.atlas_facts(True)
    entry = next(e for e in session.atlas.startable_tasks(facts)
                 if e["task_id"] == offered["task_id"])
    assert entry["startable"] is True, entry.get("reason")


def test_the_actionables_route_answers(session):
    session.atlas.set_mode("research")
    status, payload = handle_api(session, "POST", "/api/atlas/actionables", {}, {})
    assert status == 200
    assert payload["items"]


def test_the_snapshot_carries_the_actionables(session):
    session.atlas.set_mode("research")
    session.atlas_actionables(True)
    status, snap = handle_api(session, "GET", "/api/tui", {"offline": ["1"]}, {})
    assert status == 200
    assert snap["actionables"]["items"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `PYTHONPATH=$PWD .venv/bin/python -m pytest tests/test_ui.py -q -k actionable`
Expected: FAIL with `AttributeError: 'UISession' object has no attribute 'atlas_actionables'`

- [ ] **Step 3: Write the implementation**

```python
    def atlas_actionables(self, offline: bool) -> dict:
        """What Atlas would do next, ranked, with the gate's verdict on each.

        Every item is checked here AND again by `start_task` when it is
        approved. That is not redundant: the mode can change and a plan can
        appear between proposing and approving, and a proposal made in Research
        must not execute on a permit it no longer holds.
        """
        from qlab.operator.templates import template_menu

        facts = self.atlas_facts(offline)
        mode = self.atlas.mode
        trading_date = str(facts.get("as_of") or _utc_today())[:10]
        universe = ",".join(sorted(facts.get("universe", [])))
        items: list[dict] = []
        for entry in template_menu(mode, facts):
            item = dict(entry)
            item["task_id"] = None
            if entry["startable"]:
                item["task_id"] = self._proposal_task(
                    entry["template_id"], trading_date, universe, facts)
            items.append(item)
        # Startable first, then the refusals; the gate's own order within each
        # half, which is the registry's declaration order.
        items.sort(key=lambda item: not item["startable"])
        self.registry.record_event(
            "atlas_actionables",
            {"mode": mode, "offered": sum(1 for i in items if i["startable"]),
             "refused": sum(1 for i in items if not i["startable"])})
        return {"trading_date": trading_date, "items": items}

    def _proposal_task(self, template_id: str, trading_date: str,
                       universe: str, facts: dict) -> str:
        """The queued proposal for this template today, created or found.

        The dedupe key keeps `AtlasSupervisor`'s shape —
        `kind|trading_date|universe|state_hash` — because `_task_age` reads the
        trading date out of it, and a key it cannot parse reads as age-unknown,
        which `startable_tasks` refuses.
        """
        kind = f"proposal:{template_id}"
        dedupe = f"{kind}|{trading_date}|{universe}|{template_id}"
        for task in self.registry.list_atlas_tasks(200):
            if task.get("dedupe_key") == dedupe:
                return str(task["task_id"])
        task_id = uuid.uuid4().hex[:12]
        self.registry.create_atlas_task(
            task_id, dedupe, kind, {"template_id": template_id},
            template_id, origin="proposal")
        return task_id
```

Match the module's existing id generation and `_utc_today` equivalent rather than
importing new ones — grep for how `atlas_observe` gets its trading date and reuse
exactly that. `startable_tasks` resolves a template from `template_id` when the
trigger kind is unknown, which is why `proposal:` kinds need no `TRIGGER_TEMPLATE`
entry — verify that before relying on it.

Route, beside the other `/api/atlas/*` POSTs:

```python
    if method == "POST" and path == "/api/atlas/actionables":
        return 200, session.atlas_actionables(off)
```

Add the route to the module docstring's route table. Add the `/api/tui` block
beside the existing atlas keys — the newest proposal set, read from the task
table rather than recomputed, so a snapshot never mints proposals as a side
effect of being drawn.

- [ ] **Step 4: Run the tests**

Run: `PYTHONPATH=$PWD .venv/bin/python -m pytest tests/test_ui.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add qlab/ui/server.py tests/test_ui.py
git commit -m "feat(ui): asking Atlas what to do returns items you can approve"
```

---

### Task 4: The desk shows what Atlas would do

**Files:**
- Modify: `clients/atlas-tui/src/model.rs` (an `Actionables` block on `Snapshot`)
- Modify: `clients/atlas-tui/src/store.rs` (accessor)
- Modify: `clients/atlas-tui/src/ui/views/atlas.rs` (render the list in the sidebar, above the predictor board)
- Test: `clients/atlas-tui/src/ui/views/atlas.rs` unit tests + `clients/atlas-tui/tests/golden_atlas.rs` (create if the ATLAS view has no golden yet — grep `tests/golden_*.rs` first)

**Interfaces:**
- Consumes: `/api/tui`'s `actionables` block from Task 3.
- Produces: `model::Actionables { items: Vec<ActionItem> }` and
  `model::ActionItem { template_id: Option<String>, purpose: Option<String>, startable: Option<bool>, reason: Option<String>, task_id: Option<String> }`,
  all `Option` with `#[serde(default)]` on the block (an owner that does not serve it is absent, not an error);
  `Store::actionables(&self) -> &[ActionItem]`.

- [ ] **Step 1: Write the failing test**

```rust
#[test]
fn the_sidebar_lists_what_the_desk_would_do_and_why_it_would_not() {
    let store = store_with_actionables(vec![
        item("regime_review", "Re-read the regime panel.", true, None, Some("t1")),
        item("desk_rebalance_review", "Propose a rebalance.", false,
             Some("creates a paper plan, which requires Propose mode"), None),
    ]);
    let frame = render_atlas(&store, 120, 36);
    assert!(frame.contains("regime_review"), "{frame}");
    // The refusal is the product: a template silently dropped teaches nothing.
    assert!(frame.contains("desk_rebalance_review"), "{frame}");
    assert!(frame.contains("requires Propose mode"), "{frame}");
}

#[test]
fn an_owner_that_serves_no_actionables_draws_no_panel() {
    // Absence is not an error, and not an empty box either.
    let store = store_with_actionables(vec![]);
    let frame = render_atlas(&store, 120, 36);
    assert!(!frame.contains("WOULD DO"), "{frame}");
}
```

Write `store_with_actionables`, `item` and `render_atlas` against this crate's
existing test helpers — copy the shape used by the neighbouring view tests
(`golden_workforce.rs` / `golden_door.rs` build a `Snapshot` and apply it). Do not
invent a second harness.

- [ ] **Step 2: Run it to verify it fails**

Run: `cd clients/atlas-tui && cargo test the_sidebar_lists_what_the_desk_would_do`
Expected: FAIL to compile — `Actionables` does not exist

- [ ] **Step 3: Write the implementation**

Model, mirroring `PostureBlock`'s all-`Option` discipline. Render in the ATLAS
sidebar above the predictor board: a header, then one row per item — startable
items in the theme's primary text with their template id and purpose, refused
items dimmed with the owner's own refusal sentence, bounded the way this crate
bounds foreign strings (`format::bounded`). Draw nothing at all when the list is
empty.

- [ ] **Step 4: Run the tests**

Run: `cd clients/atlas-tui && cargo test && cargo test --no-default-features`
Expected: PASS on both legs

- [ ] **Step 5: Commit**

```bash
git add clients/atlas-tui/src clients/atlas-tui/tests
git commit -m "feat(atlas): the sidebar says what the desk would do, and what it would not"
```

---

### Task 5: Approving one starts it

**Files:**
- Modify: `clients/atlas-tui/src/cmd.rs` (a `Do` scope; `Command::ApproveAction`)
- Modify: `clients/atlas-tui/src/net/write.rs` (`start_task`)
- Modify: `clients/atlas-tui/src/dispatch.rs` (route the new command; its label)
- Test: `clients/atlas-tui/src/cmd.rs` unit tests, `clients/atlas-tui/tests/operator_gate.rs`

**Interfaces:**
- Consumes: `Store::actionables()` (Task 4); `POST /api/atlas/tasks/<id>/start` (exists).
- Produces: `Command::ApproveAction(String)` (the task id), `#[cfg(feature = "operator")]`;
  `WriteClient::start_task(&self, task_id: &str) -> Wrote`;
  a `/do` command scope whose values are the startable items' template ids, resolving to the task id the owner served.

**The scope writes**, so `resolve`'s existing posture filter refuses it on a glass
or unarmed window without new mechanism — and `Writes::dispatch` refuses it again
at the chokepoint. Both are already in place; do not add a third check.

- [ ] **Step 1: Write the failing test**

```rust
#[test]
fn approving_names_the_task_the_owner_served_not_the_template() {
    // The client must not invent an id. `/do regime_review` resolves to the
    // task_id that came off the snapshot, or it refuses.
    let store = store_with_actionables(vec![
        item("regime_review", "Re-read the regime panel.", true, None, Some("t1")),
    ]);
    let resolved = resolve(&scoped("do", "regime_review"), &store, Posture::Operator);
    assert!(matches!(resolved, Resolved::Runs(Command::ApproveAction(ref id)) if id == "t1"));
}

#[test]
fn a_refused_item_cannot_be_approved() {
    let store = store_with_actionables(vec![
        item("desk_rebalance_review", "Propose a rebalance.", false,
             Some("creates a paper plan, which requires Propose mode"), None),
    ]);
    let resolved = resolve(&scoped("do", "desk_rebalance_review"), &store, Posture::Operator);
    assert!(matches!(resolved, Resolved::Refused(_)));
}

#[test]
fn a_glass_window_is_not_offered_the_scope() {
    let store = store_with_actionables(vec![
        item("regime_review", "Re-read the regime panel.", true, None, Some("t1")),
    ]);
    let resolved = resolve(&scoped("do", "regime_review"), &store, Posture::Glass);
    assert!(matches!(resolved, Resolved::Refused(_)));
}
```

Match this crate's real `resolve` test helpers — grep the existing scope tests in
`cmd.rs` and copy their construction rather than inventing `scoped`.

- [ ] **Step 2: Run it to verify it fails**

Run: `cd clients/atlas-tui && cargo test approving_names_the_task`
Expected: FAIL to compile

- [ ] **Step 3: Write the implementation**

Add the scope beside the existing ones, its values derived from
`Store::actionables()` filtered to `startable == Some(true)` with a present
`task_id` — a refused item is offered nothing to type. Add
`WriteClient::start_task` posting to `/api/atlas/tasks/<id>/start` and reading the
response the way the other write methods read theirs (the owner answers 200 with a
body that may say `started: false` and a `blocked_by`; report that as a refusal,
not a success — this is the trap that shipped once before with plan execution).

- [ ] **Step 4: Run the tests**

Run: `cd clients/atlas-tui && cargo test && cargo test --no-default-features && cargo clippy --all-targets && cargo fmt --check`
Expected: PASS on both legs, clean

- [ ] **Step 5: Prove the guard**

Delete the `startable`/`task_id` filter in the scope's value list, re-run, and
confirm `a_refused_item_cannot_be_approved` fails. Restore it.

- [ ] **Step 6: Commit**

```bash
git add clients/atlas-tui/src clients/atlas-tui/tests
git commit -m "feat(atlas): approving an actionable starts the work the owner offered"
```

---

### Task 6: An approved action is driven, not parked

**Files:**
- Modify: `qlab/ui/server.py` (`atlas_workflow_runner`'s undriven case; a `drive_pending` sweep)
- Modify: `qlab/operator/heartbeat.py` (call the sweep on the beat)
- Test: `tests/test_ui.py`, `tests/test_heartbeat.py` (grep for the module that already covers the beat)

**Why this task exists:** the owner drives one coordinator at a time. If the slot
is busy when a proposal is approved, the workflow is registered and *not walked* —
`Dispatched` carries `driving: False` and a `drive_reason`, and the task sits in
`running` forever with nothing advancing it. "Atlas does the actions" is only true
if something picks that up when the slot frees.

**Interfaces:**
- Consumes: `UISession.coordinator_status()["driving"]`, `Registry.list_atlas_tasks`, `Registry.get_workflow`, `UISession.drive_workflow`.
- Produces: `UISession.drive_pending_tasks() -> list[dict]` — for each `running` task bound to a workflow that is not terminal and not currently being driven, drive it; returns what it drove. Drives **at most one** per call, because the owner has one coordinator slot.

- [ ] **Step 1: Write the failing test**

```python
def test_an_approved_action_that_could_not_be_driven_is_driven_later(session, monkeypatch):
    """One coordinator at a time means an approval can land while the slot is
    busy. Registering a workflow is not running it — without this sweep the
    task sits in `running` with nothing walking its phases."""
    drove: list[str] = []
    monkeypatch.setattr(session, "drive_workflow",
                        lambda wid, goal, roles=(): drove.append(wid) or
                        {"driving": True})
    workflow_id = session.registry.start_workflow(
        "portfolio_review", {"goal": "g", "as_of": "2026-08-06",
                             "universe": "core", "offline": True})["workflow_id"]
    session.registry.create_atlas_task(
        "task-parked", "proposal:regime_review|2026-08-06|SPY|regime_review",
        "proposal:regime_review", {}, "regime_review", origin="proposal")
    session.registry.update_atlas_task("task-parked", status="running",
                                     workflow_id=workflow_id)

    session.drive_pending_tasks()

    assert drove == [workflow_id]


def test_the_sweep_drives_one_at_a_time(session, monkeypatch):
    """The owner has one coordinator slot; driving two would be the bug
    invariant 9 already caught once."""
    ...  # build two parked tasks the same way, assert len(drove) == 1


def test_a_busy_coordinator_is_not_interrupted(session, monkeypatch):
    monkeypatch.setattr(session, "coordinator_status", lambda: {"driving": True})
    ...  # assert the sweep drives nothing
```

Fill the two elided tests with the same construction as the first; the plan shows
the shape once because the setup is identical.

- [ ] **Step 2: Run it to verify it fails**

Run: `PYTHONPATH=$PWD .venv/bin/python -m pytest tests/test_ui.py -q -k driven_later`
Expected: FAIL — no `drive_pending_tasks`

- [ ] **Step 3: Write the implementation**

```python
    def drive_pending_tasks(self) -> list[dict]:
        """Walk a running task's workflow that nothing is currently walking.

        Registering a workflow is not running it. An approval that landed while
        the coordinator slot was busy leaves a real workflow parked at phase
        one, and `reconcile_tasks` will never resolve it because it never
        finishes. One per call: the owner has one coordinator.
        """
        from qlab.state.registry import agent_for_phase

        if self.coordinator_status().get("driving"):
            return []
        for task in self.registry.list_atlas_tasks(200):
            if task.get("status") != "running":
                continue
            workflow_id = str(task.get("workflow_id") or "")
            if not workflow_id:
                continue
            workflow = self.registry.get_workflow(workflow_id)
            if workflow is None or workflow.get("status") in TERMINAL_STATUSES:
                continue
            driven = self.drive_workflow(
                workflow_id, str(workflow.get("goal") or ""),
                roles=tuple(agent_for_phase(p) for p in workflow.get("phases", ())))
            return [{"task_id": task["task_id"], "workflow_id": workflow_id,
                     "driving": bool(driven.get("driving"))}]
        return []
```

Use whatever the registry actually calls a terminal workflow status — read
`reconcile_tasks` and reuse its own test rather than inventing a constant. Call the
sweep from the heartbeat beside the existing `atlas_run_startable` call, recording
its result under its own key.

- [ ] **Step 4: Run the tests**

Run: `PYTHONPATH=$PWD .venv/bin/python -m pytest tests/test_ui.py tests/test_heartbeat.py -q`
Expected: PASS

- [ ] **Step 5: Prove the guard**

Delete the `if self.coordinator_status().get("driving")` early return, re-run, and
confirm `test_a_busy_coordinator_is_not_interrupted` fails. Restore it.

- [ ] **Step 6: Commit**

```bash
git add qlab/ui/server.py qlab/operator/heartbeat.py tests/
git commit -m "feat(ui): an approved action that could not be driven is driven when the slot frees"
```
