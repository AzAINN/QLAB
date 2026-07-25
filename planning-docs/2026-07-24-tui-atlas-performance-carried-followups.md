# Carried follow-ups — TUI atlas & portfolio performance branch

Status note for the branch implementing
`2026-07-24-tui-atlas-performance-ui-plan.md` (atlas view, portfolio
performance, connection chip, ablation leaderboard, markdown report
rendering). All eleven planned tasks shipped with per-task reviews, a
whole-branch review, one fix wave, and a scoped re-review.

Everything below was **deliberately carried**, not missed. Each was raised by
a review, triaged, and judged not worth blocking the merge. Recorded here so
the reasoning survives and nobody re-derives it.

## Governance-adjacent (look at these first)

- **`reset_book` does not clear `plans`.** A `checked` plan whose legs were
  sized against the discarded book survives a paper-book reset and stays
  executable after human confirmation. Same stale-state family as the
  `equity_marks` defect this branch fixed (marks now clear on reset and are
  stamped with their book); plans were outside that fix's scope. Worth
  deciding deliberately: either clear plans on reset, or invalidate a plan
  whose book epoch no longer matches.
- **`equity_marks.ts` is `VARCHAR` ordered lexicographically.** Every current
  writer emits UTC ISO-8601 with an explicit `+00:00` offset, so ordering is
  correct today. A writer emitting `Z` or a non-UTC offset would silently
  mis-order the newest-N read. Pre-existing pattern in the registry, not
  introduced here.

## Honesty of displayed numbers

- `metrics` and `cadence` are computed over the full (capped) daily series
  while the chart and `window_change` use `tail(365)`. The payload discloses
  the span (`cadence.observed_span_days`, `n_obs`); the TUI prints only the
  rate, so on a book longer than a year the metrics row's window is not stated
  next to the chart's dated span.
- `since_start` keeps its whole-book name while being computed from the capped
  read; only `marks_capped` distinguishes the two. No surface renders it
  today, so this is a field-name nit rather than a displayed claim.
- `marks` counts raw rows while `series` counts daily-resampled points, so an
  hourly-poll book reads "720 marks" above a 30-point chart. Honest, but easy
  to misread as chart points.
- Non-finite ablation metrics are silently skipped in the atlas overlay, and a
  bundle whose curated five are all non-finite renders as
  "no ablation recorded for this arm yet" — a degenerate run that *is*
  evidence reads as no run. The leaderboard's `_cell` handles the same case
  with an em dash; prefer that treatment if this is revisited. Note
  `test_atlas_ablation_without_curated_numbers_reads_as_absent` pins the
  current behaviour and must change with it.
- A new TUI against a pre-branch owner gets a snapshot with no `performance`
  or `leaderboard` keys, and the Book then states "No equity history yet" and
  Research "No ablation recorded yet" — assertions about the book that are
  really facts about the owner's age. The atlas fails loud instead
  ("atlas unavailable"), because it has its own route. Pre-existing
  sparse-snapshot convention; the new copy is just more assertive than its
  neighbours.

## Performance and robustness

- `performance()` and `leaderboard()` recompute per 2-second tick inside the
  global owner lock. Small next to `market()`, which was already on that path.
  A memo keyed on `(len(marks), newest ablation run_id)` would remove it.
- The poll-sourced mark write in `tui_snapshot` is unguarded, unlike the
  execution and daily hooks. Safe because the throttle is advanced *before*
  the write (a failure is not retried for an hour and the next tick succeeds)
  and because the next line calls `portfolio()` anyway. The ordering is
  commented in place — do not "fix" it into write-then-advance.
- `_cell`'s fixed field widths overflow on extreme values (Sharpe ≥ 100,
  return ≥ 1000%) and would re-break leaderboard alignment. Cosmetic.
- The leaderboard name column allows 24 cells; the longest real display name
  ("Equal risk contribution") is 23. One space of headroom, and the alignment
  test's fixture uses shorter invented names, so the real worst case is
  untested.
- The daemon refresh thread is not joined on unmount. Latent only if a future
  test reintroduces a nonzero `refresh_interval`; the pattern predates this
  branch.

## Atlas / console rendering

- The atlas payload memo can strand a stale detail pane: a successful visit,
  then a failed one (which overwrites the detail with an error), then a
  success with an identical payload early-returns before re-rendering, so the
  pane keeps claiming unavailability until the cursor moves. Also, each visit
  spawns its own fetch with no response ordering, so a slow earlier response
  can briefly show superseded numbers.
- Blank-line collapse is inert on the streamed path: a lone blank line arrives
  with an empty accumulator and is dropped. Same as the `bulletin` behaviour
  it replaced, so not a regression. The naive fix prints a spurious blank at
  every flush; a real one needs "last kind written" threaded the way fence
  state now is.
- `_console_flush` resets fence state, and it also fires at `tool_start` — so
  a fenced block spanning a tool call loses its opener. Chat-mode only, and
  content blocks do not interleave that way in practice.
- Two report renderers now coexist with opposite rules: `_console_report`
  (markdown-aware, chat mode) and `_print_results_fallback` (`bulletin`,
  id-stripping, workforce memos). The divergence is deliberate — "unifying"
  them would leak `decision_id`s into the console, which
  `test_results_fallback_cleans_markdown_mojibake_and_ids` exists to prevent.
- `POST /api/performance/backfill` hardcodes Alpaca `period="1M"` and has no
  client affordance, so recovered history caps at roughly 21 marks.

## Test coverage gaps

- `performance()`'s 5000-mark cap and the `tail(365)`-vs-`since_start` basis
  difference have no test.
- The atlas overlay's curated five and the leaderboard's curated five now come
  from one shared constant, but nothing asserts the two surfaces render the
  same set — the sharing is untested as sharing.
- Nothing pins that every `operational` catalog id has an atlas entry.
  `test_arm_algorithm_keys_exist_in_catalog` checks atlas ⊆ catalog, not the
  reverse, so a seventh operational algorithm would produce an atlas with no
  champion star and no complaint.
- The `set_entries` double-call ordering fix was verified manually, not by a
  committed regression test.
