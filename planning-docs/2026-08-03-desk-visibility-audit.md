# The desk could not see itself — visibility audit of `feat/atlas-full-desk`

**Date:** 2026-08-03 · **Branch:** `feat/atlas-full-desk` (from `origin/main` 85b04d9) · **Status:** 7 commits, suite green at `764872f` (1282 passed, 10 skipped), not pushed, no PR
**Method:** every finding below was produced by driving the live desk — a probe owner on port 8791 over a copy of the live registry, plus the rendered page in a real browser — and then reproduced as a failing test before being fixed. Unit tests found none of them.

## The shape of the failure

Twelve findings across seven commits, and essentially one failure mode. Every layer of this desk *computed* the right thing and then failed to *hand it on*. The algorithms were fine. The reasoner had thirteen context keys and rendered six. The coordinator recorded its agent stream through a filter naming kinds no parser emits. The health gate decided eligibility and dropped the reason. The news pipeline fetched 50 stories and deleted 46 at the next stage, against an explicit comment saying not to. In each case the producing side was correct, the consuming side was correct, and the seam between them silently discarded the payload.

That seam is invisible to unit tests by construction: both sides pass their own tests. It is visible immediately on a live desk, because the desk says something false out loud.

The second pattern, which is the more dangerous one: **almost none of these presented as an error.** They presented as confident, plausible, wrong answers. A refusal with `reasons: []`. A stream saying "no coordinator has published to this desk's bus" while sixty events sat in the registry. Seven stall boxes reading "already decided" about decisions nobody made. A control model filed in the treatment arm. A bar chart drawing +0.324 taller than +0.531. None of these throw. All of them are read by an operator as fact.

---

## Findings

Triage: [SEAM] producer/consumer contract broken · [SILENT] absence rendered as a value · [WRONG-ARM] a claim the evidence does not support

1. **[SEAM] The reasoner dropped the four context keys an operator actually asks about.** — `1f2f255`
   `atlas_context` composed thirteen keys; `compose_reasoner_prompt` rendered six. The seven dropped included `gate_facts`, `portfolio`, `predictors` and `recent_decisions`. Nothing warned.
   **Invariant:** a context key the prompt never renders is a key Atlas cannot read.
   **Live evidence:** the owner's book stood at $10,007 against a $24,584 high-water mark — 59.3% drawdown, kill switch fired, desk halted. Asked "how is my book doing", the model could see none of it and answered confidently from the news and regime panels, about a desk that was not this one.
   **Fix:** five new blocks (AUTHORITY, THE BOOK, ARCHIVE DEPTH, PREDICTOR BOARD, RECENT DECISIONS), each carrying the absence rules the panel block already had. An unresolved decision outcome renders *unresolved*, never neutral: a model that reads a missing outcome as "it went fine" learns the inverse of the lesson.

2. **[SILENT] A refusal with no reason, reachable from the default configuration.** — `e21d012`
   `/api/data/health` served every named check PASS, `eligible_for_paper_proposal: false`, `reasons: []`.
   **Invariant:** invariant 4 — a refusal states its reason.
   **Live evidence:** the demo desk. Two independent causes: `_EXECUTION_GRADE_PROVIDERS` was the only check that never appended a reason (integrity, provenance and freshness all explained themselves; the rule that actually blocked the desk was mute), and `data_health` returned `reasons` on the healthy path while setting only `reason` on the blocked path — and `reason` is what `atlas_facts` and the TUI read. So Atlas received `eligible: false, reason: None` on every ordinary ineligible desk.
   **Fix:** both spellings populated on both paths; synthetic reported ahead of provider, since an `alpaca` panel can still be synthetic and blaming the provider there sends the operator to swap one that was already correct.
   **Test:** `test_no_panel_is_ever_refused_without_a_reason` parametrises provider × synthetic × policy × staleness and asserts no ineligible verdict carries an empty reasons list — the invariant, not the instance.

3. **[SEAM] Grounding deleted the macro lane that `feed.py` deliberately fetched.** — `8b0011d`
   `feed.py` keeps untagged items on purpose, with a comment saying "an untagged item is macro context, not a mis-tagged holding item" and a second warning that without it the desk goes blind. `ground()` dropped every one, because its universe filter asked whether a record's tickers intersected the book and an empty tuple intersects nothing. Two modules in direct contradiction, with the fetch paying for records the next stage deleted.
   **Live evidence:** Alpaca tags no ETFs, so **46 of 50 fetched stories died at that line**, leaving a 4-record window. Three of six qualitative signals refused to report, each saying the window held fewer than the 5 records needed. The desk described a quiet market; the market was not quiet, it just was not talking about ACWI or BNDW.
   **Fix:** the two conflated cases are now distinct. A record tagged TSLA when the desk does not hold TSLA is still dropped — that is a claim about someone else's position. A record tagged with nothing is kept. Kept macro records retain their empty ticker tuple so nothing downstream can promote them into evidence about a holding. `keep_macro=False` restores the old behaviour; it defaults on because the failure mode was silence — nobody asked for a starved window, they got one, and the desk reported confidently on top of it.
   **Live result:** window 4 → 50 records, 4 → 18 claims, all three insufficient signals now ok (attention_concentration 3.0 of 4 named holdings, corroboration_ratio 0.0, publisher_concentration 1.0). The last two are damning readings of benzinga-only coverage, which is the point: measured and stated instead of missing.
   **Note:** `test_news_archive` had encoded the bug as expected behaviour, asserting grounding deleted an untagged Samsung item.

4. **[SILENT] An admitted model arrived without the bar it cleared.** — `a497ae5`
   The board admitted `kernel:angle` — a quantum angle feature map — as champion on mean_ic 0.178 against a 0.03 bar. Read from what Atlas was handed, that is a decisive win for the augmented lane.
   **Live evidence:** the same run computed a paired t of **0.237 across five folds** and per-fold ICs of +0.324, +0.531, +0.471, **−0.239, −0.195**. `predictor_board_summary` kept five metric fields and dropped `ic_std`, `per_fold`, `wins_vs_baseline`, `delta_mean_ic_vs_baseline`, `family`, `variant`, plus `admission`, `n_obs`, `n_folds`, `target`, `horizon_days`, `embargo_days` and `kernels` at board level. Every discarded field is one that makes the surviving fields mean something.
   **Three rules follow:** (a) `usable: true` is a comparison and its threshold must be visible — this champion cleared mean_ic by +0.148 and ic_stability by +0.041, i.e. it *scraped* the second, and that margin is the whole story of how much confidence the admission deserves; (b) a t-statistic without its n is not evidence but a ratio — 0.237 reads like a number until you learn it came from five folds; (c) a mean over folds that change sign is not a skill estimate — 0.178 here is arithmetic.
   **Also:** the board speaks in model_ids and the operator speaks in English, so the block now states which families *are* the quantum augmentation and which is the control.

5. **[WRONG-ARM] A control model was filed in the treatment arm.** — `2a9f5c6`
   Found by running the new research-lane route against the real board (run `460363cb26581770`, 671 obs, 5 folds): `kernel:linear augmented=True` sat at mean_ic `0.11049604765723449` — digit for digit identical to `ridge:none`.
   **Cause:** `quantum_gram` returns before the map term when `kind == "linear"`, so `kernel:linear` carries no quantum feature map at all and is the plain ridge baseline in dual form. Classifying by family prefix filed a control as a treatment.
   **Why it matters:** it lets the lane claim a row it did not earn, and lets "is the quantum lane working" be answered with a model containing no quantum anything. The same wrong claim was in the reasoner's `_predictors_block`, stated to Atlas in prose.
   **Fix:** the lane is decided by the feature map (a variant naming `angle` or `zz`); a kernel-family row carrying no map ships a `control_note` explaining that it is a control. Prompt and screen now describe the same experiment.
   **Test:** parametrised over the whole variant space × all three families, so it cannot regress into a prefix check.

6. **[WRONG-ARM] The fold chart drew the opposite of its own numbers.** — `2a9f5c6`
   Invisible in the payload; found only by looking at the rendered page. Bars were bottom-anchored in a flex row and nudged with a margin, so every positive bar shared a top edge and **+0.324 drew taller than +0.531**.
   **Fix:** two fixed rows of equal height, positive bars bottom-anchored above the line, negative bars top-anchored below it — the only layout in which "above" and "below" mean the same thing for every bar. Verified in-browser: three positive folds end and two negative folds begin on the same pixel row.
   **Standing lesson:** a chart is a claim. This one passed every assertion about the data it drew.

7. **[SEAM] The manager could not see its own workforce.** — `1603bf0`
   `/api/atlas/context` carried twelve keys and not one named a workflow, a step, a phase or an agent. `grep -c workflow qlab/ui/index.html` returned **0**.
   **Live evidence:** ten runs on the desk — 3 blocked, 3 interrupted, 1 abandoned, 3 complete — none reaching the surface Atlas reasons from. Atlas could describe the market in detail and not its own desk.
   **The good part was already there:** a blocked reporter had written *"Memo compiled and referee PASS reported, but the paper-trade preview is blocked: the permit does not allow it"* — a precise, honest account of where it stopped, sitting in the registry, read by nothing. `workforce_summary()` therefore carries step summaries **verbatim** rather than reducing them to a status label, which is what made the desk unreadable in the first place.
   **Fix:** `workforce_summary()`, a `workforce` context key, `_workforce_block()`, `GET /api/workforce`, and a panel drawing the five-agent pipeline as phase-rail pips.

8. **[SILENT] "Absent" rendered as "already decided".** — `1603bf0`
   Driving the real page showed **seven amber stall boxes against six runs needing attention**; the seventh was `abandoned` — a decision the operator had already taken, being reported back to the person who took it. Fixing that (adding `awaiting_operator`, separate from `stalled_at`, because "it stopped here" and "someone is waiting on you" are different claims) exposed a worse bug: the template read `w.awaiting_operator ? 'awaiting you' : 'already decided'`, so against a server predating the key, **all seven boxes rendered "already decided"** — the UI inventing operator decisions that never happened.
   **Invariant:** absent must never render as false.
   **Fix:** explicit three-way — `=== true` / `=== false` / else `"unknown: this desk did not say"`. A test pins the comparison *form*, because truthiness regressions are invisible to payload assertions.

9. **[SILENT] Fifteen stale triggers, refusing on the wrong reason.** — `1603bf0`
   `/api/atlas/startable` returned 15 rows: same template, same refusal, one `drift_breach` per trading day back to 2026-07-19. Queued tasks never expired.
   **The count was the visible problem; the reason was the real one.** Every row refused on the *permit* — "needs paper-proposal-eligible data" — which tells the operator that widening the permit unblocks it. Widening the permit would have started a **fifteen-day-old drift breach against a portfolio whose weights had long since moved.** A trigger is a claim about a specific day. It does not keep.
   **Fix:** `startable_tasks` reads the trading date from the dedupe key (not `created_at`, which is wall-clock and would age a task recorded just after midnight UTC by a day it had not lived) and refuses past `max_task_age_days = 5` with a reason naming the date, the age, and the fact that the condition will re-fire under today's date if it still holds. `today` is injected, never read from the clock: a deterministic surface whose answer changes at midnight cannot be reproduced, and the authority gate reads this one. An unreadable date yields `stale=None` and is refused — unknown age must not read as known-fresh.
   **Live result:** 8 of 15 now refuse on age rather than on a permit they were never really waiting for.
   **Note:** two existing tests queued a task under a hardcoded past date and started it "today" — the bug written as a fixture.

10. **[SEAM] The event filter named kinds the parser does not emit.** — `764872f`
    `_RECORDED_KINDS = ("text", "tool", "agent", "error", "session", "result")`, against a parser emitting `text`, `text_delta`, `tool_start`, `tool_result`, `session`, `error`, `result`. `tool` and `agent` match nothing; `tool_start`/`tool_result` are unreachable.
    **Consequence:** every tool call and every subagent handoff was dropped. All **31** recorded coordinator events carried an empty `agent`. The desk was not failing to hand off to subagents; it was failing to write down that it had.
    **Why the test passed:** the pre-existing bus test drove the recorder with kinds it invented on the spot. A test that makes up its own vocabulary cannot detect that the vocabulary is wrong.
    **Test:** `test_the_recorded_kinds_are_kinds_the_real_parser_actually_emits` reflects over `claude.py`'s source for the kind literals the parser assigns and asserts `_RECORDED_KINDS` is a subset. Pinning the tuple's contents would pin the instance; this pins the rule.

11. **[SILENT] A 500-row window over a shared bus, reported as silence.** — `764872f`
    The first live call to `/api/workforce/stream` returned zero events and stated, with full confidence, "no coordinator has published to this desk's bus". A coordinator had published sixty. The endpoint read a fixed 500-event window and filtered in Python; ~500 news rows had landed in the preceding four hours, so the coordinator's events had scrolled out before the filter saw them. **The reason string was accurate about the window and wrong about the world.**
    **Fix:** `Registry.read_events_of_kind(kind, limit)` does the selection in SQL. Same call then returned 60 where it had returned 0.

12. **[SILENT] SDK liveness chatter buried the reasoning, and suppression changed the meaning of empty.** — `764872f`
    With the filter fixed, 56 of 60 live events were `Claude session ...` (42 `task_progress`) — the four carrying real debate reasoning sat under a 93% flood. Dropped `session` from `_RECORDED_KINDS`; the lifecycle is already bracketed by `atlas_coordinator_started`/`_stopped`.
    **The bus is durable**, so that does nothing for the 394 heartbeats already written. `agent_stream()` therefore also filters on read — and, because this is an audit surface, **counts what it set aside and names the count in the reason** rather than discarding rows in silence. Filtering mostly-suppressed rows also means over-reading: the query takes 8× the window so heartbeats cannot fill it alone.
    **Subtle case:** if *every* event was a heartbeat, the honest statement is "a coordinator ran and published only N liveness heartbeats", not "no coordinator has published" — something ran, it just never said anything worth keeping. Reporting that as silence would be a confidently wrong reason. `test_heartbeats_only_is_not_reported_as_the_agents_being_silent` holds the two apart.
    **Live result:** 46 events / 43 reasoning rows / 3 operational, 394 heartbeats set aside and named. The analyst dispatch, the challenger's counter-case, a defend-or-amend round, the optimizer, a referee PASS and the reporter — the whole five-agent pipeline legible for the first time.

---

## The quantum lane, end to end

The specific question this branch was meant to make answerable — *is the quantum feature augmentation earning its place?* — was, before these commits, unanswerable from any surface. The board had no web route at all (`grep predictor qlab/ui/` returned nothing: no route, no panel, no nav entry). Atlas received a summary with the admission bar, the fold count, the per-fold series and the family/variant fields stripped out. And the one classifier deciding which arm a model belonged to put a control in the treatment arm.

It is now answerable, and the answer is **not yet**:

| | |
|---|---|
| Champion | `kernel:angle` (quantum angle feature map), ADMITTED |
| Mean IC | 0.178 vs a 0.03 bar — cleared by +0.148 |
| IC stability | cleared by +0.041 — **scraped** |
| Paired t vs baseline | 0.237 over **5 folds** — n.s. |
| Per-fold IC | +0.324, +0.531, +0.471, **−0.239, −0.195** — negative in 2 of 5 |
| `kernel:linear` | **a control**, not a treatment: mean_ic identical to `ridge:none` to 17 digits |
| Whole board | every model n.s.; the panel says so on each row |

The champion's 0.178 is arithmetic over folds that changed sign, and its t-statistic cannot distinguish it from the baseline. That is a real and useful research result. What matters here is that no surface was previously capable of stating it — the same run, read through the old summary, said "admitted champion, mean IC 0.178 against a 0.03 bar", which an operator would reasonably read as a win.

---

## What this says about the system

- **The tests were not wrong; they were testing the wrong side of the seam.** Every producer had tests. Every consumer had tests. Nothing tested that what the producer wrote was what the consumer read. Three tests actively encoded a bug as expected behaviour (`test_news_archive` asserting a macro item was deleted; two startable tests queueing a past-dated task and starting it "today"; the bus test inventing parser kinds).
- **Where the two are separable, pin the invariant.** Reflecting over the parser's source; parametrising over variant × family; asserting no ineligible verdict anywhere can carry an empty reason list. Each of these would have caught its bug before it was written.
- **Some claims only exist in the rendering.** The bar chart and the truthiness bug produced correct payloads and false screens. Payload assertions are structurally incapable of seeing them, so the layout mechanism itself is now pinned by test.
- **"Verify against the live desk" earned its place twelve times out of twelve.** Not one of these was found by reading code or by running the suite.

## Follow-ups

- Push and open the PR — nothing on this branch has left the worktree.
- The 42-per-run `task_progress` heartbeat volume is suppressed at both ends now but still written by the SDK path; consider not recording it at all rather than recording-then-filtering.
- `qlab/ui/index.html` is approaching the size where the panels want to be separate modules; three of this branch's bugs were in template expressions.
- Agent stall summaries are carried verbatim and are model-authored prose. That is deliberate, but it means an agent can write anything into an operator-facing amber box; worth a bound.
