# One Desk Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One current proposal booked with one click; one research workflow at a time; a cardinality limit and a choosable method; runnable predictor lanes; a research template that watches held names and scouts contenders on the web; the qualitative matrix visible.

**Architecture:** Every change reaches the registry through the owner (`qlab/ui/server.py`) and the Rust client talks to it over HTTP. Governance is unchanged in kind: the referee PASS stays bound to `targets_hash`, one explicit human confirmation stays, operator overrides are state files with audit events, the scout role is quarantined to read-only web tools, and a contender enters the universe only through an approval.

**Tech Stack:** Python 3.13 (owner, governance, solvers), Rust/Ratatui (`clients/atlas-tui`, two feature legs), DuckDB registry, Claude workforce via the coordinator.

**Spec:** `planning-docs/2026-08-31-one-desk-design.md`

## Global Constraints

- Tests never open `.lab/registry.duckdb` — `Registry(":memory:")`; offline fixtures; no network.
- Never introduce a raw-order tool or an agent-reachable execution path. `POST /api/desk/proposal/book` is client-only, requires `human_confirmed: true` and the exact `targets_hash`, and re-validates approval, plan state, and referee PASS.
- Exactly one explicit human confirmation per fill (invariant 3). The GLASS build contains no `BOOK`, no `book` write, no METHOD writes, no run writes.
- Fail loud: refusals name the reason; no silent fallback; a superseded proposal is named, never dropped.
- Cardinality: `max_holdings` is enforced by `Mandate.check_targets` for every policy; promotion of `cardinal_min_variance` to operational requires the ablation evidence recorded in the completion doc.
- Operator overrides persist in `state_path("mandate_overrides.json")` with an audit event; `configs/mandate.yaml` is not edited by the desk.
- The scout role's tools are exactly `WebSearch`, `WebFetch`, `mcp__qlab__registry.recent_decisions`, `mcp__qlab__registry.log_decision`. `agents/*.md` is the source of truth; run `python -m qlab.agents.loader sync` after editing.
- Rust: both legs green, `cargo clippy --all-targets -- -D warnings`, `cargo fmt --check`; `tests/operator_gate.rs` censuses stay truthful (new writes live in `net/write.rs`).
- Commit messages: imperative, `type(scope): …`, no AI-attribution trailers. Restart the owner after merge (invariant 8).

---

## File structure

- `qlab/governance/proposal.py` (new): the single-proposal rule — `current_proposal(registry)`, `supersede(registry, newer_plan_id)`.
- `qlab/ui/server.py`: routes `GET /api/desk/proposal`, `POST /api/desk/proposal/book`, `GET/POST /api/desk/method`, `POST /api/research/predictors/run`, `held_record_change` trigger in the tick, `universe_change` approvals.
- `qlab/trader/mandate.py`: `max_holdings`, overrides merge.
- `qlab/algorithms/policy.py`, `qlab/algorithms/catalog.py`, `qlab/algorithms/cardinal.py` (new): the cardinality policy.
- `configs/specs/ablation_v1.yaml`: arm `A6`.
- `qlab/operator/templates.py`: `portfolio_watch`; `TRIGGER_TEMPLATE["held_record_change"]`.
- `agents/contender-scout.md` (new); `qlab/tui/claude.py`: web tools for that role only.
- `clients/atlas-tui/src/ui/views/{book,atlas,settings,predictors,research}.rs`, `net/{http,write}.rs`, `cmd.rs`, `dispatch.rs`, `input.rs`, `model.rs`.
- Tests: `tests/test_proposal.py`, `tests/test_mandate_cardinality.py`, `tests/test_cardinal_policy.py`, extensions of `tests/test_ui.py`, `tests/test_templates.py`, `tests/test_agents.py`; Rust unit + insta goldens.

---

## Stream F — one proposal, one click

### Task F1: The single current proposal

**Files:**
- Create: `qlab/governance/proposal.py`
- Modify: `qlab/ui/server.py` (`announce_desk_work`, new `GET /api/desk/proposal`)
- Test: `tests/test_proposal.py`, extend `tests/test_ui.py`

**Interfaces:**
- Produces: `current_proposal(registry) -> dict | None` = `{"plan_id", "approval_id", "approval_state", "targets", "targets_hash", "referee": {...}, "created_at", "superseded": [plan_ids]}`; `supersede(registry, keep_plan_id) -> list[str]` invalidates every other pending approval with reason `f"superseded by {keep_plan_id}"` and returns their plan ids.
- Consumes: `registry.list_plans`, `registry.list_approval_requests`, `registry.invalidate_approval(approval_id, reason)` (find the existing invalidation seam in `qlab/governance/approval.py` / registry; if there is only "invalidate on plan/book change", add a reasoned invalidation with the same state transition).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_proposal.py
from qlab.state.registry import Registry
from qlab.governance.proposal import current_proposal, supersede

def _checked_plan(reg, pid, targets):
    # use the registry's own plan-logging seam with state="checked" (read
    # how tests/test_ui.py builds a checked plan for the approval tests)
    ...

def test_no_plan_no_proposal():
    reg = Registry(":memory:")
    assert current_proposal(reg) is None

def test_the_newest_checked_plan_is_the_proposal_and_older_pending_are_superseded(session):
    a = _checked_plan(session.registry, "plan-a", {"SPY": 0.6, "TLT": 0.4})
    session.announce_desk_work(True)                     # opens approval for a
    b = _checked_plan(session.registry, "plan-b", {"SPY": 0.5, "TLT": 0.5})
    session.announce_desk_work(True)                     # opens for b, supersedes a
    p = current_proposal(session.registry)
    assert p["plan_id"] == "plan-b" and p["approval_state"] == "pending"
    assert p["superseded"] == ["plan-a"]
    states = {r["plan_id"]: r["state"] for r in session.registry.list_approval_requests(10)}
    assert states["plan-a"] == "invalidated"
    chat = [e for e in session.registry.read_events_of_kind("atlas_message", 10)]
    assert any("superseded" in e["payload"]["text"] for e in chat)

def test_the_proposal_route_serves_the_same_object(session):
    status, out = handle_api(session, "GET", "/api/desk/proposal", {}, {})
    assert status == 200 and out["proposal"] is None
```

- [ ] **Step 2: Run to verify they fail** — `ImportError` for `qlab.governance.proposal`.
- [ ] **Step 3: Implement** `proposal.py`; in `announce_desk_work`, after opening the newest plan's approval, call `supersede(registry, newest)` and record one `atlas_message`: `"⚑ Plan {new[:8]} supersedes {old[:8]}: one proposal at a time."`; add the route.
- [ ] **Step 4: Run green.** `pytest tests/test_proposal.py tests/test_ui.py -q`
- [ ] **Step 5: Commit** `feat(governance): one current proposal — a newer checked plan supersedes the older`

### Task F2: Book with one click, owner side

**Files:** Modify `qlab/ui/server.py` (`POST /api/desk/proposal/book`, `book_current_proposal`); Test: extend `tests/test_ui.py`.

**Interfaces:**
- `POST /api/desk/proposal/book` body `{"plan_id", "targets_hash", "human_confirmed": true}` → approves the pending request for that plan (if pending) and executes it in one call through the EXISTING `decide_approval` + `execute_plan_with_approval` seams; returns `{"booked": true, "execution": {...}, "approval_id"}`. Refuses 400: hash mismatch (`"targets_hash does not match the plan"`), plan not the current proposal (`"not the current proposal"`), `human_confirmed` not `True`, referee not PASS for that hash, approval invalidated/expired/consumed. Emits event `proposal_booked` with plan id, hash, approval id.

- [ ] **Step 1: Tests** — happy path books (assert the execution result and a `proposal_booked` event); wrong hash refuses and books nothing; a superseded plan refuses "not the current proposal"; `human_confirmed: "yes"` refuses; GLASS is a client concern (no test here).
- [ ] **Step 2–4:** fail, implement (no new execution primitive — compose the two existing methods; the second must see the approval the first just granted), green.
- [ ] **Step 5: Commit** `feat(ui): book the current proposal in one confirmed call`

### Task F3: One research workflow at a time

**Files:** Modify `qlab/ui/server.py` (`POST /api/workflows/start`, Atlas task start) — find the coordinator-running check the owner already uses ("one at a time"; grep `coordinator` + `running` in server.py and `qlab/operator/coordinator.py`); Test: extend `tests/test_ui.py`.

- [ ] **Step 1: Tests** — with a running coordinator stubbed (monkeypatch the seam the owner reads), `POST /api/workflows/start` → 409 `{"error": "a research workflow is already running: <template> (<id>)", "running": {...}}`; Atlas's autonomous dispatch queues (task state `queued`) instead of spawning; when nothing runs, start succeeds as before.
- [ ] **Step 2–4.** **Step 5: Commit** `fix(ui): one research workflow at a time, refused by name`

### Task F4: BOOK in one click, client side

**Files:** `clients/atlas-tui/src/net/http.rs` (`/api/desk/proposal` joins the refetch), `net/write.rs` (`book(plan_id, targets_hash)`), `cmd.rs`/`dispatch.rs` (`Command::Book`), `ui/views/book.rs` + `ui/views/atlas.rs` (the proposal card, `b` key, clickable `book`), `ui/widgets/confirm.rs` (a `Modal::book(...)` variant that arms on Enter with the hash DISPLAYED, not typed), `input.rs` KEYMAP, `model.rs`, goldens.

- [ ] **Step 1: Tests** — `Modal::book` arms on Enter and its token carries the displayed hash; `b` on the proposal opens it; the GLASS leg has no `b` binding and no `book` write (operator_gate census); a superseded proposal renders struck with "superseded by …"; goldens for the card (armed, glass) and the box.
- [ ] **Step 2–4** both legs + clippy + fmt.
- [ ] **Step 5: Commit** `feat(atlas-tui): the current proposal, booked with one click`

## Stream G — cardinality and the method

### Task G1: `max_holdings` in the mandate

**Files:** `qlab/trader/mandate.py`, `configs/mandate.yaml` (`max_holdings: 8`), `tests/test_mandate_cardinality.py`.

- [ ] **Step 1: Tests** — `Mandate(max_holdings=8).check_targets({9 names > 0})` raises `MandateViolation` naming the count and the cap; 8 names pass; `max_holdings: null` = no cap; `0` or `> len(universe)` refused at load; the shipped mandate loads with `8`.
- [ ] **Step 2–4.** Update any existing test that builds targets with more than 8 names under the shipped mandate (do not weaken — give those tests an explicit `max_holdings=None`).
- [ ] **Step 5: Commit** `feat(mandate): max_holdings caps how many names a plan may hold`

### Task G2: The cardinality policy, evidence-gated

**Files:** Create `qlab/algorithms/cardinal.py`; modify `qlab/algorithms/catalog.py` (entry `cardinal_min_variance`, stage `research`), `qlab/algorithms/policy.py` (policy registered ONLY on promotion), `qlab/arms.py`/`qlab/experiment.py` (arm `A6`), `configs/specs/ablation_v1.yaml`; tests `tests/test_cardinal_policy.py`.

**Interfaces:** `solve_cardinal_min_variance(ms: MomentSet, k: int, mandate) -> dict[str, float]` = `select_k_of_n(tickers, k, covariance=ms.cov)` then the existing classical min-variance on the selected subset, weights zero elsewhere, mandate-checked (including `max_holdings`).

- [ ] **Step 1: Tests** — exactly `k` non-zero weights; the selected set equals `select_k_of_n`'s; refuses `k > MAX_EXACT_ASSETS`; refuses `k > mandate.max_holdings`; catalog entry visible at `research` and `require_operational_stage` refuses it.
- [ ] **Step 2–4.**
- [ ] **Step 5:** Run the offline ablation once (isolated `QLAB_STATE_DIR`); record `A6` beside B2 (HRP), B3 (ERC), A1 in the report. **Promotion rule:** if A6's sortino ≥ B2's AND its max drawdown is no worse than B2's by more than 1pp, flip the catalog stage to `operational`, register `cardinal_min_variance` in `_POLICIES` (arm `A6`), and commit that as its own `feat(catalog): promote …` with the numbers in the message; otherwise leave it at research and record why. Either way: **Commit** `feat(algorithms): cardinal min-variance — exact k-of-N then min-variance, ablation arm A6`.

### Task G3: The method and the cap, choosable on the desk (owner)

**Files:** `qlab/trader/mandate.py` (`load_mandate` merges `state_path("mandate_overrides.json")` for `operational_policy` and `max_holdings` only), `qlab/ui/server.py` (`GET/POST /api/desk/method`), tests.

- [ ] **Step 1: Tests** — GET lists the operational catalog entries with `current`; POST `{"operational_policy": "min_variance"}` persists the override, the session's mandate reflects it, an audit event `mandate_override` is logged; POST a research-stage id → 400; POST `{"max_holdings": 5}` persists and `check_targets` enforces 5; overrides survive `load_mandate()` reload; an override for any other field is refused.
- [ ] **Step 2–4.** **Step 5: Commit** `feat(ui): choose the operational method and the holdings cap from the desk`

### Task G4: Settings METHOD card

**Files:** `clients/atlas-tui/src/ui/views/settings.rs` (`Card::Method`), `net/http.rs`, `net/write.rs`, `cmd.rs`, `dispatch.rs`, `input.rs`, `model.rs`, goldens.

- [ ] **Step 1: Tests** — the card lists policies with the current marked; `m` opens the `Switch` (reuse) → POST; `k` opens a numeric input for `max_holdings` (1..N) → POST; click words; glass read-only; goldens.
- [ ] **Step 2–4.** **Step 5: Commit** `feat(atlas-tui): a METHOD card — pick the policy, set the holdings cap`

## Stream H — runnable predictors

### Task H1: Run a predictor lane from the owner

**Files:** `qlab/ui/server.py` (`POST /api/research/predictors/run`, off the dispatch lock like `/api/alpaca/test`), tests.

**Interfaces:** body `{"model": "ridge"|"angle_kernel"|"zz_kernel", "universe": "core", "lookback_days": 756, "offline": bool}` → runs the corresponding lane through the same functions the `research.predict_vol` / `predictor_board` tools use (read `qlab/research/board.py::_validated_models` for the exact lane names and use those), logs the run, returns `{"run_id", "model", "ic", ...}`; unknown model → 400 naming the lanes.

- [ ] **Step 1: Tests** — small synthetic panel; each lane runs and logs a run; unknown lane refuses; the board GET reflects the new run.
- [ ] **Step 2–4.** **Step 5: Commit** `feat(ui): run a predictor lane from the desk`

### Task H2: PREDICTORS gains `r`

**Files:** `clients/atlas-tui/src/ui/views/predictors.rs`, `net/write.rs`, `cmd.rs`, `dispatch.rs`, `input.rs`, goldens.

- [ ] **Step 1: Tests** — `r`/click `run` opens a `Switch` of lanes → POST → in-flight line → board refetch; glass has no `r`; goldens.
- [ ] **Step 2–4.** **Step 5: Commit** `feat(atlas-tui): run a predictor lane from the board`

## Stream I — watch what you hold, scout what you don't

### Task I1: The matrix card

**Files:** `clients/atlas-tui/src/ui/views/research.rs`, `net/http.rs` (`/api/research/qualitative` joins the refetch), `model.rs`, goldens.

- [ ] **Step 1: Tests/goldens** — a QUALITATIVE MATRIX pane: held names first (from the book's positions), columns coverage / publishers / corroborated / primary / days-to-release, `calendar_error` and `news_error` named on the pane; empty window named, never blank.
- [ ] **Step 2–4.** **Step 5: Commit** `feat(atlas-tui): the qualitative matrix, held names first`

### Task I2: The `held_record_change` trigger

**Files:** `qlab/operator/heartbeat.py` (beside the matrix log), `qlab/operator/templates.py` (`TRIGGER_TEMPLATE["held_record_change"] = "portfolio_watch"`), tests.

- [ ] **Step 1: Tests** — two ticks with two windows where a HELD name's `primary_docs` rises by ≥ 1 or `corroborated` by ≥ 2 → one trigger task with reason naming the ticker and the delta; the same change on an unheld name → no trigger; the same window twice → no trigger.
- [ ] **Step 2–4.** **Step 5: Commit** `feat(operator): a held name's record changing is a trigger`

### Task I3: `portfolio_watch` and the contender scout

**Files:** `agents/contender-scout.md` (new), `agents/news-analyst.md` (may read the matrix), `qlab/operator/templates.py` (`portfolio_watch` phases `("analyst", "scout", "reporter")`), `qlab/operator/coordinator.py` + `qlab/tui/claude.py` (the `scout` phase resolves to `contender-scout`; `--allowedTools` for that dispatch adds `WebSearch,WebFetch` — and NOTHING else new), `qlab/ui/server.py` (`universe_change` approval kind; on approval, `mandate_overrides.json` gains `universe_add: [ticker]` merged into `universe_whitelist`), `qlab/governance/goal_guard.py` (the template passes the domain gate), then `python -m qlab.agents.loader sync`; tests `tests/test_templates.py`, `tests/test_agents.py`, `tests/test_ui.py`.

**Interfaces:**
- `contender-scout` output contract (its prompt states it): a memo with, per held name, "what changed" with a URL per claim; up to 3 contenders each with ticker, thesis in two sentences, 2+ URLs; logged via `registry.log_decision(kind="scout_memo", ...)`. No weight, no size, no direction on a price.
- Reporter phase turns contenders into `POST /api/approvals` `{"kind": "universe_change", "ticker", "memo_decision_id"}`; approval decisions for that kind never touch a plan.

- [ ] **Step 1: Tests** — the role's tool list is exactly the four names; `role_scopes` reports no trader scope; `build_workforce_agents` grants `WebSearch,WebFetch` only to that role; `portfolio_watch` is startable in `research` mode and cannot create a paper plan (`check_startable`); a `universe_change` approval, when approved, adds the ticker to the merged universe and logs an audit event; `goal_guard` accepts "watch my holdings and find a contender".
- [ ] **Step 2–4.** **Step 5: Commit** `feat(workforce): portfolio_watch — a scout with eyes, a contender by approval`

### Task I4: Universe-change approvals in the client

**Files:** `clients/atlas-tui/src/ui/views/book.rs`/`atlas.rs` (approvals list renders `universe_change` with the memo line), `ui/widgets/confirm.rs` (`Modal::action` facts, Enter), `net/write.rs` (existing approve), goldens.

- [ ] **Step 1–5.** Commit `feat(atlas-tui): approve a universe change from the desk`

## Stream J — close-out

### Task J1: Docs, record, suites, build

- [ ] README + `docs/` (book in one click; one proposal; METHOD card; run a lane; portfolio watch; the scout's limits); `planning-docs/2026-08-31-one-desk-completion.md` with A6's numbers and the promotion decision; full pytest; both cargo legs + clippy + fmt; `cargo build --release`. Commit `docs: one desk — what changed and what the ablation said`.

---

## Self-review

- **Spec coverage:** one click → F2/F4; one proposal → F1; one workflow → F3; cardinality → G1/G2; method choosable → G3/G4; predictors runnable → H1/H2; watch + scout → I2/I3/I4; matrix visible → I1; record → J1.
- **Placeholders:** the Rust tasks state behaviours, keys, payloads and golden names rather than full code; implementers read the sibling views (the NEWS card precedent) — deliberate, as in the prior two plans.
- **Type consistency:** `targets_hash` string everywhere; `max_holdings: int | None`; approval kinds `plan` (default) and `universe_change`; policy ids match `_POLICIES` keys; lane names come from `board._validated_models`, not invented.

---

## Stream K — Atlas takes charge (added 2026-08-31 after the operator's second review)

The operator asked Atlas to "create a desk_rebalance_review starter" and Atlas
answered "I can't create workflows — that's your path, not mine", with fifty
stale July tasks and a 31-day-old board behind it. Rulings: Atlas starts and
resumes research workflows itself, from the chat, within the mode gates; only
booking stays a human click. The real Claude CLI can be opened on the desk —
as Atlas (the qlab MCP server is the personality provider, Claude runs it) or
as a builder on the repo. What a build produces gets a place to render.
Stale work expires on its own. Granite/ollama remain choosable as the Atlas
reasoner; the CLI hand-offs are Claude-only and say so.

### Task K1: Atlas starts the work itself, and stale work expires

**Files:** `qlab/ui/server.py` (chat tool handlers), `qlab/tui/claude.py` (`_CHAT_TOOLS`), `qlab/operator/atlas.py` + `agents/atlas.md` (persona; then `python -m qlab.agents.loader sync`), `qlab/operator/heartbeat.py` (expiry), tests in `tests/test_ui.py`, `tests/test_agents.py`, `tests/test_heartbeat.py` (or the tick tests' module).

**Interfaces:**
- Chat tools for the Atlas reasoner: `workflow.start(template_id, goal="")` → `check_startable(template, mode, facts)`; research templates start at once (one at a time — F3's rule; a second is queued and named), plan-creating templates start in `propose` mode and end at a checked plan (the single proposal), nothing ever executes; `workflow.resume(workflow_id)`; `atlas.task.create(kind, reason)`; `approvals.list()`. Each is a thin owner method the existing `/api/workflows/start`, resume and task routes already implement — no second code path.
- Expiry: on the tick, Atlas tasks older than the trigger cutoff (find the existing 5-day constant) become `expired` with reason `older than the N-day cutoff`, once; workflows with no phase progress in 7 days become `stale` (never deleted); `system_status` carries `expired_tasks` and `stale_workflows` counts.
- Persona: "You create and run research workflows yourself and say what you started; you never book — booking is the one click the operator makes."

- [ ] Tests: `workflow.start` from the chat starts a research template and returns its id; a plan-creating template in `research` mode is refused by name (the mode gate, unchanged); in `propose` mode it starts; a second start while one runs is queued and named; 50 stale tasks expire in one tick and a fresh one stays; `agents/atlas.md`'s tool list gains exactly the new names; `role_scopes` shows no trader scope.
- [ ] Commit `feat(atlas): the desk manager starts its own research, and stale work expires`

### Task K2: `/cli` and `/build` — the real Claude CLI on the desk

**Files:** `qlab/autopilot/cli.py` (verbs `qlab cli` and `qlab build "<request>"`), `qlab/tui/claude.py` (argv builders), Rust `src/ui/shell.rs` + `src/cmd.rs` + `src/dispatch.rs` (commands `/cli`, `/build <request>`: leave the alternate screen, disable mouse capture, spawn the child in the repo root, wait, restore), `input.rs`, docs.

**Interfaces:**
- `qlab cli`: interactive `claude` with `--mcp-config <the qlab-operator proxy config the workforce already writes>`, `--append-system-prompt <Atlas persona>`, `--allowedTools <proxy tools + WebSearch,WebFetch>`; refuses with a named remedy when the `claude` binary is absent or the owner is down.
- `qlab build "<request>"`: interactive `claude` in the repo root with a builder system prompt (repo conventions from CLAUDE.md; where a visual goes — Task K3; how to rebuild: `cd clients/atlas-tui && cargo build --release`, then `qlab --restart runtime`), Claude Code's own default tools and permission prompts (the operator is in the loop); the request is the first message.
- The TUI's `/cli` and `/build` suspend and restore the terminal; on return from `/build`, if `git status` shows changes under `qlab/` or `clients/atlas-tui/`, one line offers `qlab --restart runtime`. GLASS: neither command exists (census).

- [ ] Tests: exact argv for both builders; absent-binary refusal; the suspend/restore sequence with a fake spawner; glass census unchanged.
- [ ] Commit `feat(cli): open the real Claude CLI on the desk — as Atlas, or as a builder`

### Task K3: VISUALS — what a build produced, rendered in the desk

**Files:** `qlab/visuals/__init__.py` (registry: every module in `qlab/visuals/` exposing `TITLE: str` and `render(params: dict) -> str`), `qlab/visuals/quantum_circuit.py` (dependency-free ASCII drawer of the angle-encoding circuit the kernel lane uses: one wire per feature, `RY(θ)` then a ZZ entangler row, parameters read from the last predictor run), `qlab/ui/server.py` (`GET /api/visuals`, `GET /api/visuals/<name>?param=…`), Rust `src/ui/views/visuals.rs` (new view, key `0`/next free: list + render pane, Up/Down pick, Enter render, scrollable), `model.rs`, `net/http.rs`, goldens; the K2 builder prompt names this as where a visual goes.

- [ ] Tests: the registry discovers a module and refuses one without `render`; the circuit for 3 features has 3 wires and the gate glyphs; the route serves text; goldens both postures.
- [ ] Commit `feat(visuals): a place for what a build draws, starting with the circuit`

### Task K4: Rights — who may do what, set on the desk

**Files:** `qlab/ui/server.py` (`GET/POST /api/atlas/rights` → `{web: bool, workflows: bool, build: bool}`, persisted in `state_path("atlas_rights.json")` with an audit event; defaults `web: true, workflows: true, build: true`), `qlab/tui/claude.py` (reads rights when building the chat tools and the `/cli` argv), Rust MODELS card (rights rows, Space/click toggles beside the existing backend picker; a line that the CLI hand-offs are Claude-only when granite/ollama is the reasoner), tests both sides.

- [ ] Tests: rights persist and reload; `web: false` removes the web tools from the chat argv; `workflows: false` makes `workflow.start` refuse by name; goldens.
- [ ] Commit `feat(atlas): rights are set on the desk, not in a file`

### Task K5 (controller, done 2026-08-31): clean slate

`.lab` (108 MB, July–August state) archived to `.lab-archive/20260830-235115`; the owner restarted on a fresh registry. K1's expiry keeps stale work from re-accumulating.

**Order amendment:** K2 may run at once (its files are disjoint from F/G); K1 after F2 (both edit `server.py`); K3 and K4 after K1.
