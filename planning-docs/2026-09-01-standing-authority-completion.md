# Standing authority: what it books, and what still refuses

Completion record, 2026-09-01. Closes the plan and design record of the same
date ([design](2026-09-01-standing-authority-design.md),
[plan](2026-09-01-standing-authority-plan.md)). 18 commits over `ec18c3c`,
ending at the docs commit that carries this file.

The record's job is honesty, not celebration. The most useful thing this stream
produced is not the feature; it is the four pre-existing defects the feature
made reachable, and the list of things that have never run outside a test.

## What shipped

The owner's own 30-second beat books a referee-passed proposal that a live,
bounded grant covers — no human click — while every other gate stands: the
referee PASS pinned to the plan's own `targets_hash`, the mandate, the cost
gate, reconcile, and execution-time revalidation, in the order they already ran.

The model was not written here. `qlab/governance/authority.py` already
implemented `build_grant`, `check_grant_covers`, `detect_anomalies`, the
`authority_grants` table and four registry methods, with 14 passing tests, and
was **wholly unwired** — a `grep` across `qlab/` returned only the module
itself. Its own docstring said it must stay inert until a design review of the
schema, revocation, anomaly pauses and the operator experience. This stream
performed that review, fixed what the review found, and wired it. It did not
invent a second authority model.

| Piece | Where |
|---|---|
| The grant model, ceilings, anomaly rules | `qlab/governance/authority.py` |
| The table and its migration | `qlab/state/registry.py` (`authority_grants`) |
| `grant_refusals`, `book_under_grant`, `authority_payload`, the three routes | `qlab/ui/server.py` |
| The trigger — one book per tick, in the LOCK phase | `qlab/operator/heartbeat.py` (`build_owner_tick`) |
| The AUTHORITY card and `R` to revoke | `clients/atlas-tui` (Settings) |

### The commits

```
160c8c2 docs(planning): standing authority — the review the module asked for, and the wiring
0817842 docs(planning): the click is a freshness proof, so the beat gets a maximum plan age
84298ed feat(authority): a grant says how many books a day it covers
de19be1 feat(authority): the desk computes what suspends a grant
62f8b89 docs(planning): the kill switch halts a book the check never reads
6777981 fix(trader): the kill switch halts the book it fired for
b6c67c0 docs(planning): the reporting half of the halt defect
2c15956 fix(broker): a book is halted if the venue or the mandate says so
633872a feat(authority): the owner books what a live grant covers
2768913 feat(atlas-tui): the authority card shows what is left of a grant
6acf5bc fix(desk): a plan that reached the broker keeps its authority
377d51a feat(operator): the beat books what the grant already covers
8a4f0b1 fix(authority): a page of newer legs no longer hides a rejected order
9c244cd fix(atlas-tui): the authority card answers R on a suspended grant
e3e237f fix(operator): the beat asks the execution lane once, on the book's own lane
dcd7e00 feat(desk): standing authority is granted and revoked on the desk
c311bb1 fix(desk): the mid-execution error names the remedy that still works
a51dfb5 fix(desk): a standing grant's ceilings are finite, and its card prices nothing
```

## The rulings, each with its reason

- **`max_books_per_day` was added to the grant.** Every existing ceiling was
  per-plan, so nothing capped how many *plans* a grant booked on a 30-second
  beat. A seven-day grant with generous per-plan ceilings authorised a fill on
  every heartbeat, which is not what an operator granting "book the next
  rebalance" believes they are granting. The count is by **trading date**, read
  off the recorded fill's own stamp, never wall clock — the rule
  `_within_daily_budget` already followed.
- **A pre-migration grant row reads as refused, never unlimited.** A row
  written before `max_books_per_day` existed must never be more powerful than
  one written after. Absence is a missing ceiling, and missing ceilings refuse.
  Verified across all seven grant-scoped refusals: the gate refuses *and* the
  grant does not stand.
- **An anomaly input the owner cannot compute is itself an anomaly.** Unknown
  suspends; it never proceeds. `detect_anomalies`' four booleans are wired to
  live desk state — `halted` from the book's own halt flag, `reconcile_clean`
  from the reconcile the owner already runs, `data_execution_eligible` from the
  data permit the execute gate already reads, `recent_order_anomaly` from a
  rejected or expired order in the recent window — and any one of them that
  cannot be read suspends the grant by name.
- **`MAX_AUTO_BOOK_AGE_S = 120`, because the click is a freshness proof.**
  Every binding on an approval — `plan_digest`, `targets_hash`,
  `book_revision` — is a content address of the *past*. `check_grant_covers`
  checks ceilings and never recency, and an approval lives 900 s, so a book
  whose revision has not moved passes the drift check trivially for the whole
  quarter hour. Without this bound the desk books a correctly-sized fill
  against a stale analysis every 30 seconds and no human ever sees the
  interval. Strictly tighter than the TTL, deliberately: a plan the grant calls
  stale is left for a human, who can still book it by hand until the approval
  expires.
- **The automatic path reaches `approved` through the same `decide_approval`
  the click calls.** Two ways in, one way to execute — rather than a second
  path that books around the approval object. `book_current_proposal`'s
  steps 2–6 were *extracted*, not copied, and the clicked path's five refusal
  literals are byte-identical (the only delta in the moved block is a two-line
  comment; a one-word mutation fails the pin).
- **It refuses any plan that has already started.** A half-filled book is
  exactly the case that deserves a person. `execute_plan` accepts `checked` or
  `submitted` so a human can resume, and its halt gate sits inside
  `if not already_started:` — so a resumed plan completes *past* a kill switch
  latched in the interim. That is a defensible trade for a human's resume; it
  is not one a 30-second beat may make. The rule is stated positively —
  anything but `checked` is left for a person.
- **Both write routes refuse a chat origin, and no MCP surface names a grant.**
  An Atlas that could grant itself authority would make the whole object
  decorative. Pinned by a census across every agent surface in
  `tests/test_mcp_server.py`, and by a grep of the source of `qlab/mcp/*.py`
  and `qlab/tui/claude.py`.
- **`PAPER_AUTO` is the only mode.** No live-trading mode is expressible, and
  a mode sent on the wire is refused by the module's own sentence rather than
  dropped.

## Four pre-existing defects this stream exposed

**None of these were in the new code.** Each was already in the tree, and each
became reachable or visible only once something finally depended on it working
with nobody watching. That is the honest headline of this stream.

1. **The drawdown kill switch halted a book the pre-trade check never read.**
   The switch latched `broker.name` while the check read `DEFAULT_BOOK`, so a
   halt on an Alpaca book did not stop the next trade there. Fixed at `6777981`
   across `qlab/trader/plan.py`, `qlab/autopilot/loop.py` (the drawdown latch)
   and `qlab/mcp/quant_trader.py` (the `halt`/`resume` tools, which now name
   and return the book they acted on). The reviewer reproduced it in both
   directions: reverting the fix fails "DID NOT RAISE MandateViolation" *and*
   the cross-book test, proving the leak ran both ways. A census confirms all
   six `set_halt` call sites are book-named and zero defaulted halt reads
   remain. Two defaulted `get_account()` calls survive, both **cash** not halt:
   `reconcile.py:26` is rated MEDIUM and left for its own task — on an Alpaca
   desk it compares Alpaca cash to the simulated row, giving either a spurious
   diff that blocks trading or, if that row is absent, a silently no-op cash
   check, which is a *lost* check that the anomaly wiring consumes as
   `reconcile_clean`. `quant_lab.py:500` is LOW and correct to leave.
2. **`AlpacaPaperBroker.portfolio_state` reported the venue's `trading_blocked`
   and never qlab's own latch** — so the desk could not see its own halt.
   Fixed at `2c15956`: `bool(acct.trading_blocked) or bool(reg.get_account(
   self.name)["halted"])`, its own book, never `DEFAULT_BOOK`. The consequence
   is narrower and more pointed than first claimed, and this is the version to
   record: the shipped `mandate.yaml` sets `drawdown_tiers.breaker_pct` and
   `kill_switch.trailing_drawdown_pct` to the *same* 0.15, so at the instant of
   breach the tier trips the alert independently of the bug. What the bug
   actually cost was the **recovery window** — the registry latch is sticky
   (cleared only by an explicit resume) while `drawdown_tier` is a live,
   non-sticky recomputation, so once equity partly recovered below the breaker
   tier while the book was still latched, `operator/atlas.py`'s `kill_switch`
   alert went silent on a still-halted book. The alert failed exactly when an
   operator most needs telling they are still halted.
3. **A page of newer legs hid a rejected order.** The recent-order scan read
   the newest 200 legs, so a rejection older than a page of newer fills
   vanished and the desk reported clean. Fixed at `8a4f0b1`. Reproduced end to
   end: one rejected order at 20h plus 200 filled legs at 19h returned `[]`
   with the saturation check reverted — a clean desk reported while a rejection
   sat in the window.
4. **On a blocked lane the permit prime never latched**, re-measuring a 252-day
   snapshot every 30 s while holding `_LOCK`. Fixed at `e3e237f`. Measured
   before the fix at 3 ticks → 3 measurements, 3 `permit_primed` rows, 0
   permits: on a live desk with a broken feed that is a snapshot attempt every
   beat, each failing after a provider timeout, stalling the snapshot poll, SSE
   and approvals behind it, plus 2,880 event rows a day.

A fifth, found by A4's reviewer and corroborated independently from the other
direction by A3's, is a *labelling* defect rather than a safety one and is
fixed here too: a `PermissionError` raised inside the `except` propagated
instead of the original error, so a genuine mid-execution fault surfaced as a
lifecycle 400 (`6acf5bc`, now `raise exc from bookkeeping`).

## What has NEVER RUN LIVE

Unsoftened, because every claim above rests on tests and every test uses
`Registry(":memory:")` and a simulated broker.

- **No grant has ever been created against a live owner.** Every
  `POST /api/desk/authority` in this stream was in-process or over a socket to
  a test server.
- **No automatic fill has ever happened on a real desk.** Not once, on any
  lane, against any book. The `authority.booked` row has only ever been written
  by a test.
- **The client's AUTHORITY card has never spoken to the real routes.** B1's
  payload and B2's card were built against a written contract, in that order,
  and the card's tests use fixtures. The ten ways a shape mismatch breaks the
  client are enumerated in
  `.superpowers/sdd/2026-09-01-standing-authority-plan/b1-integration-checklist.md`
  and reproduced here, because they are the first thing to check when the card
  is finally pointed at a live owner:

  1. `grant: {}` instead of `null` → the card renders `standing --` with
     all-`--` ceilings instead of `none` plus the remedy. A **silent misread**,
     not an error. (`model.rs:1766`)
  2. Any scalar wrongly typed — `days_left` as a float or string, `max_orders`
     / `max_books_per_day` / `books_today` not ints, `max_notional` /
     `max_turnover` not floats — fails serde for the **whole payload** →
     `Fetched::Malformed` → the card never populates. There is no per-field
     tolerance. (`model.rs:1779-1802`)
  3. `anomalies` as objects rather than `[str]` → the same whole-payload
     failure. (`model.rs:1769`)
  4. `max_turnover` as `35.0` where the client expects `0.35` → renders
     `3500.0%`. Nothing catches it. (`settings.rs:5423`)
  5. `books_today` meaning "remaining" rather than "spent" → `books_left()`
     silently inverts. (`model.rs:1811`)
  6. Revoke requiring `grant_id` in the body → 400, rendered as a refusal. The
     client deliberately sends a reason and no id. (`net/write.rs:1405`)
  7. Revoke returning `{"revoked": {...}}` rather than `{"grant": {...}}` → the
     toast loses the id; nothing breaks. (`net/write.rs:1411-1417`)
  8. A refusal status other than 400/403 — e.g. a 409 "already revoked" →
     `AuthorityFailed` → an Alarm toast reading "the grant may still stand".
     (`net/write.rs:1421-1431`)
  9. A refusal body keyed on anything but `error` → the raw JSON is shown
     instead of the owner's sentence. (`net/write.rs:1688`)
  10. `GET /api/desk/authority` requiring a lane or query parameter → non-2xx →
      `Fetched::Failed`, logged and dropped, and the card reads "nothing has
      said what may book itself" **forever**, with no owner-down signal.
      (`net/http.rs:424-443`)

- **No Alpaca desk has exercised any of the halt fixes against the real
  venue.** Defects 1, 2 and 3 above are all Alpaca-lane defects, and all three
  fixes are proven only against a faked trading client built through `__new__`
  so no network is possible. The behaviour of `acct.trading_blocked` on a real
  Alpaca paper account is assumed, not observed.

## Known open item: the high-water mark on `/api/tui`

B1's F2 fix stopped the AUTHORITY read from re-anchoring the drawdown
high-water mark across data lanes: `authority_payload()` now has an **empty
signature**, so no caller can pass a lane, and the read serves a `(stamp, list)`
cache the beat publishes. Measured at 20 high-water-mark writes and 9 lane
resets per ten GETs before, and 0 and 0 after.

**Do not read that as the high-water mark being safe in general. It is not.**
`/api/tui` has the identical defect, takes `offline` from the query string with
no `_offline_for_book` clamp, and is polled every 3 seconds — a *faster*
cadence than the card ever was. It was measured identically before and after
this stream: ten lane-alternating polls give **22 high-water-mark writes and 9
lane resets at base `ec18c3c`, and 22 and 9 at `a51dfb5`**. This stream neither
created nor worsened it. It is recorded here as an open item with its own
follow-up (F1 below) so the F2 fix cannot be read as closing it.

The consequence, stated plainly: a client polling `/api/tui` while the lane
alternates re-anchors the book's high-water mark, after which no drawdown ever
reaches the kill switch.

## Limitations, each as a decision

- **A half-filled book still needs manual intervention, and A4's fix does not
  change that.** The approval now survives a mid-execution failure and the
  misleading invalidation reason is gone, but
  `check_approval_for_execution`'s `book_revision` binding still refuses the
  resume ("book moved since approval"). The error message was fixed to stop
  instructing the one action that destroys the authority it just preserved
  (`c311bb1`), and a deliberate tripwire test goes red when the carve-out lands
  so the sentence must change with it. The carve-out is this stream's top
  follow-up (F2).
- **An ineligible first permit is never re-measured**, so a recovered lane
  stays suspended until a hand-booked fill or a health call.
  `current_data_permit` returns the newest row for the purpose without
  filtering on eligibility, which is what makes a refusal genuinely unable to
  become a permission by the beat — the safe direction, and the reason the
  latch has no re-open. A permit TTL is the real fix; two implementers
  deliberately declined to invent one mid-stream (F3).
- **The first automatic book on a demanding lane takes two 252-day snapshots** —
  the prime's, then the gate's re-measurement at the door. Once per desk, and
  the gate's is not optional: it is what stops a recorded permit from standing
  in for a live one.
- **`MAX_AUTO_BOOK_AGE_S` can be overrun by earlier lock-phase work on a
  reasoner desk.** Several phases including a 252-day `data_health` run under
  the lock before the book, and a reasoner's 45 s timeout sits between phase
  one and the book, so a 120 s budget is plausibly exceeded. The outcome is
  safe — a refusal — but silently non-booking. Recording the plan's age on the
  refusal would make it diagnosable (F4).
- **Above ~200 legs in 24 h the recent-order scan suspends rather than
  under-counting.** Suspending is correct: the function's own rule is that
  unknown suspends, invariant 4 backs it, and a spurious suspend is a bounded
  inconvenience where the false negative it replaces was a silent governance
  failure. An exact registry-side count retires it (F5).
- **The `authority.booked` row *is* the daily budget ledger**, and it is
  written after the fill, outside any `try`. If `record_event` raises
  post-fill, the book happened but spends none of the day and the next beat can
  book again (F6).
- **Granting is not built into the client**, so an accidental `R` costs a trip
  to the route. Acceptable only because revocation is idempotent and the key
  sits behind a focused card reached by two deliberate Down presses. A confirm
  box in front of "stop" is the wrong fix — withdrawing authority is the safe
  direction. Granting *from* the desk is the right fix (F7).
- **`qlab/operator/` now imports from `qlab/ui/` for the first time.** Both
  directions of the import are function-scoped and lazy, so there is no cycle,
  and `build_owner_tick`'s contract was already the owner's — but it inverts
  the intended layering and reaches a `_`-private symbol. A layering fact worth
  stating rather than a defect.
- **The two halt disjuncts are indistinguishable downstream.** An operator
  cannot tell "Alpaca blocked me" from "my kill switch fired". Separate
  `halted_by_venue` / `halted_by_mandate` keys would fix it, and there is no
  consumer yet (F8).
- **A stopped beat shows "paused by: not measured yet" on every card.** True,
  and the safe reading — but an operator who stopped the beat deliberately sees
  a pause they did not cause, and the explanatory clause is exactly the half
  the card's width cut drops, so at rest they cannot tell "owner just started"
  from "beat is dead" (F9).
- **The beat now measures anomalies on every tick, including idle ones** — one
  broker build, one reconcile and one 200-row order page per 30 s under
  `_LOCK`, measured at 6.3 ms on an idle desk with no grant. Bounded,
  single-lane, single-cadence, within the envelope the desk already pays.
  Gating it on `live_grant() is not None` was considered and declined: it would
  change the contract that anomalies are served whether or not a grant stands.
- **The refusal record dedupes against the newest row only**, so two
  alternating refusal states write a row each tick. A volume concern (worst
  case ~2,880 rows/day from a flapping reconcile on an Alpaca lane), not a
  safety one.
- **The trading date is `date.today()`** — the owner's local date, matching the
  existing convention and rolling at local rather than UTC midnight. Not
  exchange-calendar aware.
- **`days_left` floors**, so a fresh 7-day grant reads "6 d left". Correct and
  pinned. Ceiling would trade an understatement for an overstatement on a
  safety card, which is the wrong direction; rendering `expires_at`, or
  "< 1 d" at zero, is the card-side fix (F10).
- **The 120 s anomaly staleness bound is derived from the 30 s beat and does
  not scale with it.** Raising `QLAB_ATLAS_INTERVAL_S` above 120 would make
  every card read unmeasured between beats. This stream added the note at
  `qlab/operator/heartbeat.py`'s `DEFAULT_INTERVAL_S`, in `.env.example` beside
  `QLAB_ATLAS_INTERVAL_S`, and in `docs/atlas.md`; the coupling is enforced by
  nothing (F11).

## The monitoring build can see a grant and cannot stop one

Verified with `nm` on both legs at this branch's tip: the `--no-default-features`
artifact carries **0** `revoke_authority` symbols and 460 authority symbols; the
armed build carries 10 and 460. So the read-only binary renders the AUTHORITY
card — the grant, its ceilings, what is left of it, and any anomaly — and has no
way to revoke.

That is correct by construction rather than an oversight: `net::write` is
`#[cfg(feature = "operator")]`, and a monitoring artifact that could withdraw
authority would not be read-only. It is written down because the consequence is
not obvious from either half. An operator watching a desk through a glass window
who sees a grant standing must reach for an armed client or
`POST /api/desk/authority/revoke`; the window in front of them will not do it.
The client says as much at runtime (`settings.rs:372`); until now no document did.

## Follow-ups

| # | Follow-up | Why |
|---|---|---|
| F1 | Fix the `/api/tui` high-water-mark re-anchoring the same way F2 fixed the AUTHORITY read | Measured at 22 hwm writes / 9 lane resets per ten polls, identical at `ec18c3c` and HEAD, at a 3 s cadence. A polling client can disable the kill switch. Pre-existing and untouched by this stream |
| F2 | The part-filled resume carve-out, as **one shared predicate pushed down into `plan.py`** | Already precedented at `plan.py:271-278`; the predicate to add (only the plan's own `client_order_id` set accounts for every position delta since the approval) is **stricter** than the existing one, so it closes the same hole in both places rather than loosening a gate. Waive only `book_revision`, only when `submitted`; status, expiry, kind, `plan_digest`, `targets_hash` and the data permit stay binding. The 900 s TTL still caps the window. The tripwire test and `server.py`'s remedy sentence must change with it |
| F3 | A TTL on the data permit | The only thing that re-opens a lane that recovered after an ineligible first permit |
| F4 | Record the plan's age on the `authority.refused` row | Makes a silently non-booking reasoner desk diagnosable |
| F5 | An exact registry-side count of today's books and of recent order anomalies | Retires both 200/500-row scan ceilings |
| F6 | Write the `authority.booked` row inside the execution write, or fail loud into the beat | The row is the daily budget ledger and is currently written outside any `try` |
| F7 | Granting from the desk | Closes the "revoke is a keystroke, grant is a curl" asymmetry |
| F8 | Split `halted` into `halted_by_venue` / `halted_by_mandate` | An operator cannot tell the venue from their own kill switch |
| F9 | One string: let the unmeasured card distinguish "owner just started" from "beat is dead" | The explanatory clause is the half the width cut drops |
| F10 | Render `expires_at`, or "< 1 d" at zero, beside the floored `days_left` | "6 d left" on a fresh 7-day grant reads as an off-by-one |
| F11 | Make `_ANOMALY_STALE_AFTER_S` derive from the configured interval, or refuse an interval above it | Today the coupling is documented in three places and enforced in none |
| F12 | Retire `QLAB_AUTOPILOT_EXECUTE=1` | Out of scope by decision; a standing grant is now the better-bounded answer to the same need |
| F13 | Give `reconcile.py:26`'s `get_account()` a book | On an Alpaca desk the cash check is either spuriously blocking or silently absent — and the anomaly wiring consumes its verdict as `reconcile_clean` |

Two pre-push findings remain untouched and belong to whatever edits those paths
next: the defensive-basket exemption and the silent contender truncation, both
recorded in [pre-push review findings](2026-09-01-pre-push-review-findings.md).

## Two lessons about testing, kept because they recurred

- **A controller-prescribed assertion was too weak, twice.** In B2's fix round
  the prescribed pin was `contains('will refuse')` on a clipped warning — and
  the implementer *ran it* at the clipped height and found it still passed,
  because the warning's first clause also ends in "will refuse". The shipped
  pin is the whole sentence unwrapped out of the card's own column, which fails
  on any cut. The pattern is prescribing an assertion without running it; the
  remedy is to state the property and let the implementer find a pin that
  bites.
- **Two of B1's thirty new tests initially passed** — a raising probe that
  `_grant_anomalies` swallowed, and an "or" assertion that could not tell the
  two NaN layers apart. Both were caught by **ablation**, not by review, and
  both are now red-on-revert, proven by a clean 2x2. Red-first is not enough on
  its own; the ablation is what proves the test is load-bearing.

## Verification

Every suite run from the worktree root at the docs commit, offline, with no
owner runtime involved.

| Suite | Result |
|---|---|
| `python -m pytest` (full offline suite) | **2056 passed, 10 skipped**, 1437 warnings, 157.79s |
| `cargo test` (armed, default features, full parallelism) | **1247 passed, 0 failed, 1 ignored** across 28 test binaries |
| `cargo test --no-default-features` (glass, full parallelism) | **783 passed, 0 failed, 0 ignored** across 28 test binaries |
| `cargo clippy --all-targets -- -D warnings` | clean, 0 warnings |
| `cargo clippy --all-targets --no-default-features -- -D warnings` | clean, 0 warnings |
| `cargo fmt --check` | clean, no output |
| `cargo build --release` | ok in 57.32s → `clients/atlas-tui/target/release/atlas`, 8,576,928 bytes (8.2 MiB) |

Both cargo legs ran at full parallelism — the pty tests hold a shared mutex, so
`--test-threads` was deliberately not passed. Neither cargo leg needs an owner,
and no test in either language opens `.lab/registry.duckdb`.

Every one of those counts is offline and synthetic. See **What has NEVER RUN
LIVE** above for what that does and does not establish — the suites prove the
code does what the tests say, and nothing about what a live owner or a real
Alpaca account does with it.

## Docs changed

`CLAUDE.md` invariant 3 now names **three** recorded forms of confirmation — the
hash-bound click, a persisted standing grant, and the two out-of-band env
hatches — and keeps its refusal of any agent-reachable execution path.
`AGENTS.md` mirrors it. The sweep for sentences this stream made false covered
`README.md` (the human-gate bullet, the pipeline diagram, the SETT row, the
loop, the one-writer diagram, the clients paragraph), `docs/architecture.md`
(the diagram and the governance section), `docs/cli.md` (the "two that can book
without a click" claim, a new standing-authority section with the real field
names, and the `R` key), `docs/atlas.md` (the heartbeat, the mode paragraph, the
deployment section), `docs/ibm-bob.md` and `clients/atlas-tui/README.md` (both
claimed a fill costs exactly one confirmation).

## What the branch review found after this record was written

The counts and claims above were true when written, at `c8a5b57`. A
whole-branch review and a scoped re-review followed, each with a fix round;
the branch merged at `e08e7ce`. This section records what they found rather
than rewriting what preceded them.

### The deadlock the suite could not have caught either way

The first fix round made the standing path call `_mark_after_mutation` after an
automatic fill, so an unattended book leaves the same trace as a clicked one —
an execution-sourced equity mark and a valuation-cache drop. That call now runs
**inside `_LOCK`**, from the heartbeat's observe phase, which is the one shape
that could freeze the owner for every client at once.

The suite cannot answer whether that is safe, and it is worth knowing why
before someone trusts a green run here: **`_tick` passes a fresh
`threading.Lock()`, not the module object.** Every test of this path holds a
lock nothing else contends. The re-review established the answer by reading the
call graph — `_mark_after_mutation` reaches `invalidate_valuation` (a bare
attribute store), `record_equity_mark` → `portfolio` → `get_broker` /
`portfolio_state`, and registry calls; `qlab/state/registry.py` takes no lock,
and the only lock anywhere on the path is `qlab/core/data.py`'s `_CACHE_LOCK`,
an `RLock` leaf over filesystem I/O that cannot reach back into `server.py` —
and then by experiment: a real fill driven while holding the production
`ui_server._LOCK` with a 45-second faulthandler armed. It booked in 0.055 s,
wrote the mark and dropped the cache. Both clicked routes already call the same
function under the same lock.

### A lane frozen at startup, and a lane named by a flag nobody read

`build_owner_tick` captured `offline` in its closure at owner start and never
re-read it, so a runtime `POST /api/desk_mode` flip left standing books pricing,
deciding permits and executing on the startup lane until restart. The fix
derives the lane per book from `session.desk_mode.offline` rather than
re-reading per tick — a per-tick re-read would have moved the news fetch, desk
read, matrix, judgment request, `atlas_observe` and the autonomous start onto a
new lane as a side effect of a book-scoped fix. Deriving at the book cannot go
stale by construction: the helper takes no flag, so no caller can supply one.

That left two `offline` parameters that nothing read — one in
`book_under_grant`, one in `prime_execution_permit`, the second having gone
dead in the same round unnoticed. Both were removed rather than kept for
signature compatibility, on the grounds that the parameter *names the lane an
unattended fill runs on*: a future caller would pass a flag believing it
controlled where a real fill prices and executes.

**Carried, and deliberately not fixed:** the tick's read half — news, desk read,
matrix, judgment request, `atlas_observe`, the autonomous start — still runs on
the captured startup flag. Only the book, the prime and the anomaly
measurements follow the live desk mode. A desk-mode flip leaves that divergence
until the owner restarts.

### The third recurrence of the same testing lesson

The section above records two assertions on this branch that passed vacuously.
The re-review found a third variant: the lane fix's *prime* half had no failing
test at all — reverting it alone left 2065 of 2065 green, because every bit of
failing power lived in the sibling half of the same commit. A fix round closed
it with an ablation demonstrated in both directions. The lesson is now that a
two-site fix needs a pin per site: one test that fails when either half is
reverted is indistinguishable, from the outside, from one test that pins one
half and ignores the other.

### Counts at the merged tip

`e08e7ce`: full suite **2067 passed, 10 skipped** — run in the worktree and
again on the merged tree in the main checkout, identical. `cargo test` (ARMED)
**1247**, `cargo test --no-default-features` (GLASS) **783**, clippy clean on
both legs, `cargo fmt --check` clean. Still every count offline and synthetic;
**What has NEVER RUN LIVE** above is unchanged by any of this.
