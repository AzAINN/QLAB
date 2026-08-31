# One desk: what shipped, and what the ablation said

Completion record, 2026-08-31. Closes the plan and design record of the same
date ([design](2026-08-31-one-desk-design.md)). 51 commits over `57519ad`,
ending at `bd26706`.

## What the operator found, and where it stands

The six findings of the [design record](2026-08-31-one-desk-design.md), each
against what is now in the tree:

| # | Found | Now |
|---|---|---|
| 1 | Booking took two boxes and the same hash typed twice | one box, one click, one call — `POST /api/desk/proposal/book` |
| 2 | Two research runs left two allocations | one current proposal; one research workflow at a time, refused by name |
| 3 | No cardinality anywhere | `max_holdings` in the mandate; `cardinal_min_variance` at **research** stage — **not promoted**, see below |
| 4 | The method never changed; the board had no key | METHOD card (`GET`/`POST /api/desk/method`); `r` runs a predictor lane |
| 5 | Nothing watched the book; no role had web | `held_record_change` → `portfolio_watch`, with the `contender-scout` |
| 6 | The qualitative matrix was invisible | QUALITATIVE MATRIX card on RESEARCH, held names first |

## What shipped, per stream

**F — one proposal, one click, one workflow** (`2f7dd42`, `fa413fd`, `761e3c2`,
`58c3f3d`, `0a2dd4a`, `bb99efe`, `e3a688a`, `b54317b`, `f17cc63`).
`qlab/governance/proposal.py` makes a newer checked plan supersede the older
pending one and invalidates its approval with the reason; the chat says so once.
`GET /api/desk/proposal` is that one object. `POST /api/desk/proposal/book`
approves and executes in a single call, composing the two existing methods —
no new execution primitive — with every refusal landing before any state
change. On the client, `b` (or clicking the word BOOK) opens one box that
*displays* the allocation and the last six of the plan's own `targets_hash`;
Enter posts it. A second `POST /api/workflows/start` while a coordinator runs is
a 409 naming the running template.

**G — cardinality, the method, and the cap** (`4c88dd2`, `ce76b07`, `065262f`,
`870d952`, `604d236`, `cd760eb`, `e941766`, `e9372ce`).
`max_holdings` is a mandate limit checked by `Mandate.check_targets`, so every
policy including HRP is held to it; it ships as `null`. The operator's chosen
method and cap persist in `state_path("mandate_overrides.json")`, merged by
`load_mandate` with an audit event — `mandate.yaml` stays the governance
document. `cardinal_min_variance` (exact k-of-N selection, then min-variance on
the chosen names) entered the catalog at **research** and stayed there.

**H — the predictor board runs** (`62b52c5`, `2cf5614`, `1d6c6de`).
`POST /api/research/predictors/run` runs one lane and its baseline off the
dispatch lock; `r` on PRED refreshes the board and offers the lane picker.

**I — the matrix, the watch, the scout** (`3a59b40`, `078c9ea`, `33b5035`,
`b82e6d3`, `0f74c13`, `7586b9b`, `dc28512`, `c1ab825`, `d3048b1`, `06f208b`).
The matrix card ranks held names first and reads *held* from the live book
alone. A window-over-window change in a held name's record is
`held_record_change`, mapped to `portfolio_watch` (analyst → scout → reporter,
creates no plan). The `contender-scout` role holds `WebSearch`, `WebFetch`,
`registry.recent_decisions` and `registry.log_decision` — nothing else exists in
its grant. Its contenders become `universe_change` approvals answered one at a
time on AUDIT or from ATLAS; an answered question is found in SQL
(`Registry.answered_universe_change`), not by scanning a window.

**K — Atlas in charge, the CLI on the desk, visuals, rights** (`5faa88f`,
`07e2c37`, `a429049`, `1e4ec42`, `c6b8147`, `d760b18`, `34582f2`, `5a47f5c`,
`96caac2`, `cf991cb`, `afb0728`, `16e1cd2`, `3ca49a3`, `a5ae9f4`, `06bf678`,
`2f8a7c1`, `c3e6afe`, `9d5d45c`, `d57b194`, `1ca1553`, `bd26706`).
Atlas starts its own research from the chat with four named tools —
`workflow.start`, `workflow.resume`, `atlas.task.create`, `approvals.list` —
and queued work nobody answered expires. `qlab cli` opens the real Claude CLI
wearing the Atlas persona through the owner-backed proxy; `qlab build` opens
Claude Code on this checkout; the workstation spells both `/cli` and `/build`,
and the stdin reader stops while the child holds the terminal. `qlab/visuals/`
is a registry of what a build draws (the angle-encoding circuit first), served
at `GET /api/visuals` and `/api/visuals/<name>`, painted by the VISUALS view on
key `0` — the tenth and last digit the rail has. Rights `{web, workflows,
build}` live in `state_path("atlas_rights.json")`, are set on the MODELS card,
and shape the grants: `web` the chat's and `qlab cli`'s two web tools, `build`
the `/build` key, and `workflows` is refused **for the desk chat only**.

## A6: the ablation met its gate and was not promoted

Verbatim from [the A6 record](2026-08-31-a6-cardinality-not-promoted.md).

The pre-registered gate: promote if A6's sortino ≥ B2's (HRP) **and** A6's max
drawdown is no worse than B2's by more than 1 percentage point. Offline, seed 7,
isolated registry:

| arm | what it is | sortino | max drawdown | ann return | ann vol | deflated Sharpe |
|-----|-----------|--------:|-------------:|-----------:|--------:|----------------:|
| A1  | min-variance, all 7 names | 0.6984 | −0.2093 | 0.0344 | 0.0732 | 0.8420 |
| B2  | HRP (the real bar)        | 0.6565 | −0.2063 | 0.0330 | 0.0749 | 0.8127 |
| B3  | equal risk contribution   | 0.5611 | −0.2105 | 0.0282 | 0.0748 | 0.7369 |
| **A6** | **exact 4-of-7, then min-variance** | **0.9485** | **−0.1763** | **0.0560** | **0.0867** | **0.9570** |

"The gate is met on both legs: sortino +0.2920 over B2, drawdown 3.00pp
*better*, not worse. A6 also tops every other arm in the matrix."

Why that is not believed, in four findings:

- **No look-ahead found.** The full panel and the panel truncated at the same
  `as_of` produce byte-identical weights at every date tested; that is now a
  regression test.
- **The selection is a constant, not a mechanism.** Over the 57 rebalances with
  a full 756-day window A6 picks the same basket 56 times —
  `('ACWI', 'BNDW', 'GLD', 'VNQ')`, with one `('ACWI', 'BNDW', 'GLD', 'GSG')`.
  "So the arm does not contain 57 out-of-sample selection decisions. It contains
  **one**, repeated." The constancy is a property of the generator
  (`_synthetic_prices` derives vol and drift from `md5(ticker)`, so relative
  vols are stationary and consecutive windows overlap ~92%), not a discovery
  about k-of-N, and the selector is not broken. This panel cannot exercise
  selection, so it cannot be the panel that promotes it.
- **The seed margin collapses, and the sweep is weaker than it looks** — the
  seed does not resample the per-asset volatility profile, so it is a check on
  luck-of-the-path, not luck-of-the-basket.

  | seed | A1 sortino | B2 sortino | A6 sortino | A6 − B2 | A6 max_dd | B2 max_dd |
  |-----:|-----------:|-----------:|-----------:|--------:|----------:|----------:|
  | 7    | 0.6984 | 0.6565 | 0.9485 | **+0.2920** | −0.1763 | −0.2063 |
  | 11   | 0.1313 | 0.0611 | 0.2883 | +0.2272 | −0.2119 | −0.2887 |
  | 23   | −0.6178 | −0.6471 | −0.5753 | +0.0718 | −0.4050 | −0.4021 |
  | 42   | −0.4404 | −0.4268 | −0.4234 | +0.0034 | −0.3818 | −0.4094 |

- **The confidence intervals overlap almost completely.** A6's annualised
  interval `[0.31, 1.71]` contains essentially all of B2's `[−0.02, 1.52]`.
  (`sortino_ci` is per-period; the table's `sortino` is annualised — both scales
  are given in the record so they cannot be misread as the same number.)
- **The win is a return outcome from a return-blind selector.** A6 earns 0.0560
  against A1's 0.0344 while running more risk (0.0867 vs 0.0732 ann vol); the
  objective sees only volatilities and correlations.

A structural blocker sits underneath the evidence: `OperationalPolicy.arm()`
builds `Arm(arm_id, objective, solver)` and **drops `params`**, so a
`cardinal_min_variance` policy registered today would hand the autopilot an arm
with no `cardinality` and run plain full-universe min-variance under the
cardinal name. No agent tool can express `k` either.

**Decision, verbatim: "`cardinal_min_variance` stays at `research`. It is not
registered in `_POLICIES`. Arm A6 stays in `ablation_v1.yaml` so the measurement
is reproducible and the next run can be compared against this one."** What would
change the answer: a wider candidate universe than seven names with a volatility
profile that actually varies across draws, a positive margin that survives that
sweep, and an execution path that carries `k` end to end.

One real hole was closed in review and does not change the numbers: long-only
min-variance may park a selected name on its lower bound, so the *delivered*
plan could hold fewer than `k` names above the 1e-4 threshold the mandate and
the trader count at. The policy now counts its own delivered names at 1e-4 and
refuses, naming both counts. A6 delivered exactly 4 names at every rebalance of
all four seeds, so the recorded results are unaffected.

## What has NOT run live

Recorded plainly, because none of it is covered by the offline suite.

- **The `contender-scout` has never run.** Its web tools need a live Claude
  session; the memo → `universe_change` flow is tested offline only. A
  contender outside core ∪ extended needs catalog promotion before
  `universe_add` will accept it.
- **VISUALS has never been rendered against a live owner.** The fixtures were
  hand-written to the contract, so the first live render is the integration
  test.
- **The live 756-day predictor board is untimed.** The client's 120 s deadline
  was measured on the synthetic panel only.
- **`held_record_change` is NOT in `_WORKFLOW_TRIGGERS`, and the beat now
  honours that.** As shipped on this branch the sentence above was written as
  if membership in `_WORKFLOW_TRIGGERS` decided what the beat starts. It did
  not: `_WORKFLOW_TRIGGERS` was read only by `_within_daily_budget`, which the
  mint never calls, while `atlas_run_startable` gated on `origin` alone and
  `portfolio_watch` is admissible in `research` — so the owner *did* start one
  Claude coordinator (with WebSearch/WebFetch) per moved held name per window,
  uncounted against `max_autonomous_workflows_per_day`. The branch fix round
  makes the two sets one: `atlas_run_startable` starts only the trigger kinds
  the budget counts. A `held_record_change` task is now minted, announced by
  `announce_desk_work`, and stays `queued` until a human starts it — from
  WORKFORCE, or from chat via `workflow.start` under rights. The same gate
  applies to every other kind outside the set (`owner_startup`,
  `data_recovered`, `kill_switch`, `new_research_run`): they are queued and
  announced rather than started unattended.
- **The live-on-Alpaca-book path** is still unexercised end to end, unchanged by
  this branch.

## Follow-ups

Carried whole from `.superpowers/sdd/2026-08-31-one-desk-plan/j1-followups.md`.
Each is deliberately NOT in this branch; each names the task that surfaced it.
The first entry is the branch fix round's own, and it is the top one.

- **I2 (top follow-up): bound the `held_record_change` mint before the watch
  may run unattended.** Two bounds, both required. (1) *Coalesce per window*:
  the mint is one task per moved held name, so a wide news day mints one
  workflow's worth of work per ticker; one task per window carrying the moved
  names is the shape that can be started once. (2) *Count against the budget*:
  once coalesced, the kind joins `_WORKFLOW_TRIGGERS` so every start it earns
  is charged to `max_autonomous_workflows_per_day` — the fix round made
  membership in that set the single gate for unattended starts, so joining it
  is now the whole of the promotion. Until both hold, the task is queued and
  announced and a human starts it. (An outage window becoming the next baseline
  is the third, older half of this: a `degraded` flag at the matrix write site.)
- F1: `current_proposal` ordering has no SQL tiebreak for equal `created_at` —
  add one in the registry query.
- G2: cardinality was evaluated on the 7-name ablation spec with
  `cardinality: 4`; the evaluation it needs is a 20-name spec. Promotion is also
  blocked by a code fact: `OperationalPolicy.arm()` drops params and no tool
  carries `k`, so promoting `cardinal_min_variance` as specified would ship a
  wrong-number bug.
- G3: `_FULL_UNIVERSE_POLICIES` is hand-maintained in `server.py`; a
  `holds_every_name` flag on `OperationalPolicy` is the durable home.
- I3: the `news-analyst` role's matrix grant is deferred — the Ollama runner has
  no `TOOL_SCHEMAS` entry for it.
- I3/K4-routes: `Registry.answered_universe_change` replaces the 500-row scan in
  `_check_not_already_answered` (done in K4-routes if its report says so;
  otherwise still open).
- H1: `POST /api/research/predictors/run` has no rate limit or concurrency cap
  (same posture as `/api/alpaca/test`); a data-dependent fold failure
  (`n_splits` too large for the panel) is a 500 by construction.
- H2: the in-flight line is client-only — nothing on `/api/tui` says a board is
  being fitted, so a second window can start a second run over the first; the
  120 s deadline was timed on the synthetic panel only; `box_rect` is a third
  copy of centred-box arithmetic (door.rs and settings.rs disagree about
  wrapping).
- K3b-rust: the rail is out of digits (VIS is on `0`); an 11th view fails loudly
  in a store test and needs a numbering decision. The first live VISUALS render
  is the integration test — the fixtures were hand-written to the contract.
- K4-claude: stop projecting `atlas` into `.claude/agents/` (it is the desk's
  reasoner, not a Claude Code subagent).
- Scout: the `contender-scout` has never run live — web tools need a live Claude
  session; the memo → `universe_change` flow is tested offline only, and a
  contender outside core ∪ extended needs catalog promotion before
  `universe_add` accepts it.
- Older, still open: `server.py`/`test_ui.py` splits; the owner route census
  (dead `/api/atlas/ask` family); operator-surface resume of a halted headless
  desk.
- Ruled unnecessary: unquoting the `/api/visuals/<name>` path segment (names are
  registry identifiers; an unknown name 404s naming the known ones).
- I4 (parked Minors for the branch fix round, not follow-ups): pointer rects
  guarded by pane height; `note_rows` hard-break; the misplaced `landing` doc
  paragraph.

## The rulings that shaped the work

- **Exactly one explicit human confirmation stays** (invariant 3). It became one
  click on a box that shows the allocation and the hash it is bound to; the
  client posts that hash and the owner re-validates approval, plan and referee
  PASS before any fill. Zero confirmations would be an invariant change the
  operator did not ask for.
- **`max_holdings` ships as `null`.** The cap is a limit the operator sets, not
  a default the branch imposes on an existing book.
- **Rights are an operator's stated intent, not a security boundary** — exactly
  like the posture. `POST /api/atlas/rights` is as unauthenticated as every
  other owner route, and `workflows` binds the desk chat alone: a
  `qlab workforce run`, the owner's own coordinator, the heartbeat's dispatch
  and a non-Claude reasoner making its own owner call are bound by none of it.
  The MODELS card carries that sentence — `nothing here binds a non-chat
  caller` — because no per-right row can.
- **`web: true` is a widening, kept and declared.** The desk chat and `qlab cli`
  gained read-only `WebSearch`/`WebFetch`; that is a real increase in what the
  desk can reach, it is bounded to those two tools, and the rights card is where
  an operator withdraws it.
- **The four chat action tools are a named exemption.** Atlas's evidence tools
  stay read-only; `workflow.start`, `workflow.resume` and `atlas.task.create`
  start governed work and nothing else, and every run they start is still held
  by the template gate, the mode, and the referee.
- **The scout has eyes, not hands.** Its memo's excerpts enter the desk only
  through the provenance-gated news lane, and a contender outside the universe
  is a question the operator answers. Nothing it says moves a weight.
