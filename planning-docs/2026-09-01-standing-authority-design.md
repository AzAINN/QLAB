# Standing authority: the review the code asked for, and the wiring it was waiting on

Design record, 2026-09-01. Binding authority for the plan of the same date.

## What is already built, and why it is switched off

`qlab/governance/authority.py` implements standing paper authority in
deterministic code: `build_grant`, `check_grant_covers`, `detect_anomalies`,
the `authority_grants` table, four registry methods, and 14 passing tests.
Nothing in production references any of it — `grep` for the module across
`qlab/` returns only the module itself. Its own docstring says why:

> **This is inert by default and must stay that way until reviewed.** … The
> plan requires a separate design review of the grant schema, revocation,
> anomaly pauses, and the operator experience before standing authority is
> actually used; this module implements the schema and the checks so that
> review has something concrete to examine, not so the desk can start trading
> unattended.

> **Superseded 2026-09-01 (record only).** Both claims above — that `grep`
> returns only the module itself, and the docstring quoted here — were true
> when written and no longer describe the module: this stream wired it at
> `dcd7e00` and rewrote that paragraph in the fix round. They stand as the
> record of what the review was looking at.

That review is this document. The stream does not invent a second authority
model; it reviews the one that exists, fixes what the review finds, and wires
it. What the module already guarantees, and this record adopts unchanged:
expiring and never open-ended (`MAX_GRANT_DAYS = 30`); revocation checked
first and outrankable by nothing; a plan touching any symbol outside the
grant's universe refused **whole**, never trimmed to fit; ceilings on notional,
turnover and order count; anomalies that **suspend** without revoking; and
`PAPER_AUTO` as the only mode, with no live-trading mode expressible.

## The review

### 1. Schema — accepted, with one addition

The ceilings are per-plan. Nothing bounds how *many* plans a grant books. A
seven-day grant with generous per-plan ceilings authorises a fill on every
heartbeat, which is not what an operator granting "book the next rebalance"
believes they are granting. **`max_books_per_day` is added** to the grant, to
`build_grant`'s required ceilings (positive, no default), and to the table by
migration — the `kind`-column migration in `qlab/state/registry.py` is the
precedent. A grant that omits it is refused like any other missing ceiling.

Counting is by trading date over `orders`/`events` the owner already writes,
never by wall clock — the same rule `_within_daily_budget` follows for
autonomous workflows.

### 2. Revocation — accepted, and it must be one keystroke

`check_grant_covers` returns on `revoked_at` before it reads anything else,
which is right. Two additions, both about reach rather than logic: revocation
must be reachable from the desk in one keystroke with no typed confirmation
(withdrawing authority is the safe direction, and a box between an operator and
"stop" is a hazard), and it must be recorded with its reason like every other
governance transition.

### 3. Anomaly pauses — accepted, and they must be computed

`detect_anomalies` takes four booleans that nobody supplies. Each is wired to
live desk state: `halted` from the book's halt flag, `reconcile_clean` from the
reconcile the owner already runs, `data_execution_eligible` from the data
permit the execute gate already reads, and `recent_order_anomaly` from a
rejected or expired order in the recent window. An input the owner cannot
compute is itself an anomaly — unknown suspends, never proceeds.

### 4. Operator experience — the part that did not exist

- **Granting states its ceilings.** No ceiling has a default; the operator sets
  universe, notional, turnover, orders, books per day and a TTL within 30 days,
  and the desk shows the grant it composed before it is written.
- **A live grant is visible wherever a fill can happen**, with what is left of
  it: days remaining, books left today, and the ceilings themselves.
- **Every automatic fill says so**, naming the grant, at the moment it happens
  and in the audit stream afterwards.
- **Revoking is one keystroke** from that same surface.

### 5. Freshness — the risk the review nearly missed

The click is not only consent; it is a **freshness proof**, and removing it
removes a guarantee nothing else provides. Every binding on an approval —
`plan_digest`, `targets_hash`, `book_revision` — is a content address of the
*past*: each says "nothing has changed since a human looked". A human pressing
BOOK is looking at a card built from a poll seconds old, at market data whose
staleness the desk shows them, and they refuse by not pressing it.

`check_grant_covers` checks ceilings, never recency. An approval lives 900
seconds (`approval.py`'s `ttl_seconds`), and a book whose revision has not
moved passes the drift check trivially the whole time — so the automatic
path's real hazard is not an oversized fill, which the mandate already bounds.
It is a **correctly-sized fill against a stale analysis, repeated every 30
seconds, with no human ever seeing the interval.**

So the automatic path carries a maximum plan age of its own, strictly tighter
than the approval TTL: `MAX_AUTO_BOOK_AGE_S = 120`. A plan older than that is
refused by the grant and left for a human, who can still book it by hand until
the approval expires. The number is a constant rather than a grant field in
this stream — one more operator-set ceiling is one more to get wrong, and the
conservative bound needs no tuning to be safe. Making it configurable is a
follow-up, not a gap.

## Rulings

- **The owner books; the agent never does.** No MCP tool, no chat action tool,
  and no proxy verb creates, edits, reads around, or consumes a grant. The
  create/revoke route refuses chat origin outright, as `POST /api/atlas/rights`
  already does — an Atlas that could grant itself authority would make the
  whole object decorative.
- **A grant replaces the per-plan human confirmation and nothing else.** The
  referee PASS pinned to the plan's own `targets_hash`, the mandate, the cost
  gate, reconcile and execution-time revalidation all still run, in the order
  they run today. `book_current_proposal`'s steps 2–6 are shared verbatim
  between the clicked path and the standing path; only step 1 differs.
- **The heartbeat is the trigger**, at most one book per tick (30 s default,
  `qlab/operator/heartbeat.py`). A proposal that a live grant covers is booked
  by the owner on its own beat; nothing an agent runs reaches it.
- **A grant reaches `approved` the way a human does.** The automatic path
  performs the same `decide_approval(..., "approve")` the click performs, so
  one execution path survives with two ways to reach an approved record —
  rather than a second path that books around the approval object.
- **Absence refuses.** No grant, an expired grant, an unreadable grant, an
  uncomputable anomaly input, or a bound the owner cannot evaluate all refuse
  the automatic path. The clicked path is unaffected by any of it.
- **Invariant 3 gains a third recorded form of confirmation** — a persisted
  standing grant — beside the desk's hash-bound click and the two env hatches.
  It keeps its refusal of any agent-reachable execution path.

## The bug this stream inherits

`qlab/ui/server.py:5797-5810` invalidates the approval when
`execute_plan_with_approval` raises — including after `execute_plan` has set
the plan `submitted` and put legs at the broker, which is exactly the state the
resume path needs its authority for. The One Desk stream fixed the same defect
in `withdraw_orphans`; this sibling was missed. It is fixed here because the
standing path raises through the same handler, and a half-filled book that
booked itself is worse than one a human started.

## Surfaces

| Surface | Today | After |
|---|---|---|
| A fill | one click on the hash-bound BOOK box | that, or the owner's own beat under a live grant |
| `authority_grants` | a table nothing writes | the grant the owner consults |
| Settings | DESK, NEWS, METHOD, MODELS, … | + AUTHORITY: the live grant, what is left of it, and revoke |
| Audit | approvals, executions | + granted, suspended, consumed, revoked, expired |
| `agents/*.md` | no role sees a grant | unchanged, deliberately |

## Out of scope, by decision

Live trading (`PAPER_AUTO` remains the only mode); retiring
`QLAB_AUTOPILOT_EXECUTE=1` (documented, and a separate decision); a grant that
edits itself or renews automatically; per-symbol ceilings below the universe
scope; and the two remaining pre-push findings (the defensive-basket exemption
and the silent contender truncation), which are recorded in
`planning-docs/2026-09-01-pre-push-review-findings.md` and belong to whatever
touches those paths next.
