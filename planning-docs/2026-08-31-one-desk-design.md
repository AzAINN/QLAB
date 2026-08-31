# One desk: one proposal, one click, one method, and research that watches what you hold

Design record, 2026-08-31. Binding authority for the plan of the same date.

## What the operator found

Six things, verified against `main` at `581e23c`:

1. **Booking is a hassle.** A proposal takes `a` (approve → a box that
   asks for the last six of the targets hash) and then `x` (execute → a
   second box, the same hash again). The binding is right; the typing
   twice is not.
2. **Two research runs leave two allocations.** `announce_desk_work`
   opens an approval for *every* checked plan. Nothing says which one the
   desk wants answered.
3. **No cardinality.** The mandate caps weight per name, never the number
   of names. Exact k-of-N selection exists (`qlab/core/selection.py`,
   `selection_k_of_n`) but at research stage, reachable by no operator.
4. **The method never changes.** `operational_policy: hrp` is a mandate
   field no desk surface can set; the predictor board (ridge vol
   prediction — the "linear regression"; angle/ZZ kernel ridge) is
   agent-runnable and has no key on the PREDICTORS view.
5. **Nothing watches the book.** No template reads what is held; no
   Claude role has web access (`build_workforce_agents` grants `Agent` +
   the proxy tools, nothing else).
6. **The matrix is invisible.** The qualitative matrix is logged per
   window and served at `/api/research/qualitative`, and no view renders
   it.

## Rulings

- **Exactly one explicit human confirmation stays** (invariant 3). It
  becomes one click on a box that shows the allocation and the hash it is
  bound to; the client posts that hash; the owner re-validates approval,
  plan, and referee PASS before any fill. Zero confirmations would be an
  invariant change the operator has not asked for in so many words.
- **One current proposal.** A newer checked plan supersedes an older
  pending one; the older approval is invalidated with the reason and the
  chat says so once. One research workflow runs at a time; a second start
  is refused by name.
- **Cardinality is a mandate limit, not only a solver.** `max_holdings`
  is checked by `Mandate.check_targets`, so every policy — HRP included —
  is held to it. The cardinality *policy* (exact selection, then
  min-variance on the chosen names) enters at research stage and is
  promoted to operational only if the ablation arm earns it; the record
  states the number either way.
- **Operator overrides live in state, not in the shipped mandate.** The
  method and `max_holdings` chosen on the desk persist in
  `state_path("mandate_overrides.json")`, merged by `load_mandate`, with
  an audit event. `configs/mandate.yaml` stays the governance document.
- **The scout has eyes, not hands.** A quarantined `contender-scout`
  role gets read-only `WebSearch`/`WebFetch` and the registry's decision
  tools, no data/solve/trade tools. Its memo's excerpts enter the desk
  only through the provenance-gated news lane; a contender outside the
  universe becomes a `universe_change` approval the operator answers.
  Nothing it says moves a weight.
- **Held names first.** The matrix card and the watch trigger rank held
  names above the rest; a window-over-window change in a held name's
  record (coverage, corroboration, primary documents) is a trigger, and
  the template it maps to is `portfolio_watch`.

## Surfaces

| Surface | Today | After |
|---|---|---|
| BOOK / ATLAS | `a` then `x`, two typed hashes | one proposal, `b`/click BOOK, one box, Enter |
| Workflows | any number, each with its own proposal | one running; a second start is refused by name |
| Settings | DESK, NEWS, POLICY, THEME, SYSTEM, MODELS, UNIVERSE | + METHOD (policy picker, `max_holdings`) |
| PREDICTORS | read-only board | `r`/click run: choose a lane, run it, board refreshes |
| RESEARCH | ranking, ledger, catalog | + QUALITATIVE MATRIX card, held names first |
| Triggers | regime, drift, drawdown, kill switch, new run | + `held_record_change` → `portfolio_watch` |
| Roles | 10 | + `contender-scout` (web, quarantined) |

## Out of scope, by decision

Live-account trading (paper only, unchanged); the owner's route census
and `server.py` split (recorded follow-ups); a second confirmation for the
universe change beyond the existing approval flow.
