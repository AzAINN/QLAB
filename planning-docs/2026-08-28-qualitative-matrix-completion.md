# The qualitative matrix, through the catalog — completion record (2026-08-28)

Closes Stream E of `2026-08-28-primary-sources-plan.md` (tasks 7–9). Task 7
built the matrix (counts per name, no signed column). Task 8 turned those counts
into two bounded, unsigned risk views. Task 9 — commit `24892da`, plus the fix
round in `495942e` — put that chain behind the catalog, the referee and the
ablation, and ran it.

**The headline is a null result, and it is a *measured* null.** The A5 arm walked
68 quarterly windows and fired zero views, so its covariance was A1's at every
rebalance and its out-of-sample numbers are A1's to four decimal places. That is
the arm reproducing its baseline on a silent archive, not a broken sweep — and
the mechanism that kept the archive silent is stated below, because a null whose
cause is unknown is not a deliverable.

## The numbers

`qlab batch configs/specs/ablation_v1.yaml --offline`, run against an isolated
`QLAB_STATE_DIR` so no live desk registry was touched. Run id
`8f930afdb495cc95`; synthetic offline panel, seed 7, 2008-01-01 → 2024-12-31,
quarterly, 756-day estimation window, 5 bps per side.

| arm | what it is | sortino | ann_vol | maxDD | dSharpe |
|-----|-----------|--------:|--------:|------:|--------:|
| A2  | scenario CVaR (the falsifiable rival) | 0.7544 | 0.0754 | -0.2258 | 0.9054 |
| A1  | classical min-variance baseline | 0.6984 | 0.0732 | -0.2093 | 0.8746 |
| **A5** | **views-conditioned min-variance** | **0.6984** | **0.0732** | **-0.2093** | **0.8746** |
| B4  | regime-conditional min-variance | 0.6907 | 0.0732 | -0.2081 | 0.8717 |
| **B2** | **HRP — the real bar** | **0.6565** | 0.0749 | -0.2063 | 0.8494 |
| **B3** | **ERC / risk parity** | **0.5611** | 0.0748 | -0.2105 | 0.7822 |
| B1  | equal weight | 0.3806 | 0.0788 | -0.2148 | 0.6174 |
| A3t | MVSK + vol-target overlay (research only) | 0.3213 | 0.0788 | -0.2391 | 0.5529 |
| B0  | 60/40 | 0.2733 | 0.1376 | -0.3397 | 0.5610 |
| A3  | MVSK | 0.1641 | 0.1027 | -0.3150 | 0.4128 |
| A4  | MVSK multistart | 0.1641 | 0.1027 | -0.3150 | 0.4128 |

A5 ≡ A1 on every column. A5 beats HRP and ERC by exactly the margin A1 does,
which is to say the qualitative lane contributed nothing here — in either
direction.

The walk's own counts, now logged as a `views_summary` run rather than left in a
dict that dies with the process (`registry.newest_run_of_kind("views_summary")`):

```json
{"ablation_run_id": "8f930afdb495cc95", "arm": "A5",
 "windows": 68, "windows_with_views": 0, "views_applied": 0,
 "windows_conditioned": 0, "infeasible_windows": 0, "unverified_windows": 0}
```

`qlab batch` prints the first three as one line:
`views: 68 windows, 0 with views, 0 views applied`.

## Why no rule fired — the mechanism, not a shrug

68 arm-sourced `qualitative_matrix` runs were logged, one per rebalance. Every
row of every one of them has `coverage: 1`, `primary_docs: 0`, `corroborated`
in `{0, 1}`. Against the two rules in `qlab/research/matrix_views.py`:

- **The tail rule** needs `primary_docs - baseline.primary_docs >= 2` with at
  least that many corroborated. The synthetic news generator emits no
  `tier == "primary"` claims at all, so the excess is 0 in every window. It can
  never fire offline. This is a property of the fixture, not of the rule.
- **The correlation rule** needs a sleeve whose total coverage is `>= 4` and
  whose loudest name holds `>= 75%` of it. It cannot fire on this spec **for a
  second, structural reason**: `sleeves_for` groups by the universe's own
  `asset_class`, and the ablation's seven pinned names
  (ACWI, BNDW, GSG, IGF, GLD, VNQ, EMB) fall into **seven distinct classes** —
  every sleeve is a singleton. A singleton sleeve has total coverage 1 and no
  `others` to state a correlation against, so the rule skips it before it ever
  looks at coverage. The correlation rule is unreachable on `ablation_v1`
  regardless of how loud the record gets.

Two consequences worth stating plainly. First, this ablation tests the tail rule
and the plumbing; it does **not** test the correlation rule at all. Second, a
live archive with primary documents is the only place either rule can fire, so
the offline null is not evidence about the hypothesis — it is evidence that the
chain runs end-to-end, refuses correctly, and records what it did.

## Two things this evidence cannot settle

**1. A5-vs-A1 is confounded: views and estimator move together.** When a rule
does fire, `qlab/core/moments.condition` replaces A1's Ledoit-Wolf-shrunk,
Marchenko-Pastur-denoised covariance with the probability-weighted **sample**
covariance that `conditioned_moments` returns. So a firing window changes two
things at once — the views *and* the estimator — and the difference cannot be
attributed to either. This is B4's precedent exactly: the regime arm's
lambda-mixed covariance has the same shape of confound. It is documented in the
A5 arm's comment in `configs/specs/ablation_v1.yaml` and left in place
deliberately: tilting the *shrunk* estimator would redefine what
`views_conditioned_min_variance` means, and doing that before any evidence says
the entry earns a place is designing a promotion nobody has asked for. It is a
promotion-time design question, and a promotion that skips it would be reading a
combined effect as a views effect.

**2. The referee's lineage gate covers one path, not both.** A solve whose
moment set names a views run FAILS unless that run is in the registry, stayed
inside its KL budget, and had verified provenance — but only where
`deterministic_referee` is *given* a registry, which today is the two autopilot
call sites in `qlab/autopilot/loop.py` (`run_once` and `_build_trigger_proposal`).
The owner's workflow/plan path (`qlab/ui/server.py` ~590) calls the referee with
neither a registry nor a moments summary — it re-runs only the drawdown-tier
check — and a workflow plan carries no reference to the moment set it came from,
so there is nothing there for the lineage check to read even if it were passed
one.

**This is an explicit precondition of promoting
`views_conditioned_min_variance` to `operational`.** Today the stage gate is the
real gate: `algorithms.solve` refuses a research entry in code, so no conditioned
covariance can reach a governed solve, a workflow phase or a paper plan by any
path. The moment the stage changes, the workflow path becomes the unguarded one.
Promotion must first carry a moment-set reference through the plan and pass the
registry at that call site, with a test that fails without it.

**3. A conditioned moment set is consumable by every operational algorithm.**
`moments.condition` returns a `moment_set_id` like any other, and nothing
downstream distinguishes it: `objective.build` takes the id and checks only the
objective *form*; `algorithms.solve` checks only that the catalog entry is
operational. No code path anywhere reads `provenance.views_run_id` off the
moment set before solving — the referee reads it from the moments *summary*,
after the solve, and only where it is handed a registry (see 2 above). So the
one thing today keeping a views-tilted covariance out of a governed solve is
that `moments.condition` itself refuses: the stage gate on
`views_conditioned_min_variance`. The tensor, once produced, is anonymous.

**This too is an explicit precondition of promotion.** Promoting the entry as
it stands would not make the conditioned tensor solvable by *its own* entry —
it would make it solvable by `min_variance`, `hrp`, `erc` and every other
operational algorithm, none of which were measured on it and none of which
would record that they had been handed one. Promotion must make a conditioned
`moment_set_id` consumable **only** by its own catalog entry: the lineage has to
be read where the objective or the solve is built, not merely re-checked
afterwards by a referee that may not have been given a registry.

## After the branch review (2026-08-29)

The whole-branch review found seven seams that only show when the branch is read
end to end. All were fixed on this branch; each has a failing-first test.

1. **The plural provider env did not govern `news.fetch`.** `docs/news-setup.md`
   tells operators to set `QLAB_NEWS_PROVIDERS`, but the owner's feed tool
   called the singular `fetch_news`, which read only `QLAB_NEWS_PROVIDER` — a
   desk configured for two live sources served synthetic fixtures and said
   nothing. `news.fetch` now reads the whole stack and reports each member's
   outcome; the singular API resolves through the same stack and refuses a
   multi-member one, naming `fetch_news_stacked`.
2. **`research.qualitative_matrix` was granted but not forwarded.** It was in
   `agents/*.md` and in `OWNER_LAB_TOOLS`, but missing from `_LAB_TOOL_BASES`
   and from the proxy, so `_proxy_tool` returned `None` and
   `build_workforce_agents` dropped it from the grant in silence — invariant 10
   again, from the other direction: a grant with nothing to forward it.
3. **Plugin providers were undiscoverable outside the stack.** Only
   `fetch_news_stacked` called `load_plugin_providers()`, so
   `qlab news-check --provider acme` called an installed entry-point provider
   unknown. Discovery now runs once on a resolve miss.
4. **Claim-key provenance was not point-in-time, and the views run was not
   bound to its matrix.** The archive was read as "the newest matrix by write
   time", so a window logged for T+7 sourced a view at T — look-ahead entering
   through the provenance gate itself. The owner now stamps its matrices
   `source: "desk"`, claim keys are selected at or before the run's own date,
   the persisted views run carries the `matrix_run_id` it verified against
   (`None` for a quoted excerpt, never absent), `moments.condition` refuses a
   verified run carrying no such field, and the referee FAILS a solve whose
   cited matrix is dated after it. This closes E1's "a `views` run bound to the
   matrix run", which had been left as provenance verified against *something*
   unnamed.
5. **An `ablation_a5` matrix could answer as the desk's record.**
   `research.qualitative_matrix` read `matrix_runs(source=None, ...)`, so the
   arm's research window — another universe, another day, built by rule rather
   than read from the press — could be served as what the press said. It now
   selects the desk's stamp and reports `source`.
6. **GDELT was anchored to wall clock.** `timespan=48h` means "48 hours back
   from now", so every non-live `as_of` asked for today's articles and then
   dropped all of them against the caller's cutoff: a permanent empty window,
   with no error, indistinguishable from a quiet press. The request now carries
   an explicit `startdatetime`/`enddatetime` derived from `as_of`.
7. **The A5 arm's online path was half-built rather than absent.**
   `MatrixViewsConditioner._matrix` called `fetch_news` without handling
   `PartialWindow` (one provider short a feed would abort the walk mid-arm) and
   passed `provider="synthetic"` to `ground()`, which would have stamped live
   records with the fixtures' provider name. It now refuses `offline=False` at
   construction until that path is designed; the offline walk is unchanged.

Also: `qlab.arms.estimate` refused nothing when `views_source` and
`regime_conditional` were combined — it applied the regime-conditioned
covariance and then let the views conditioner overwrite it, so such an arm
measured only the views under a name claiming both. It now refuses.

## Adaptations Task 9 made to its brief

The brief (`.superpowers/sdd/2026-08-28-primary-sources-plan/task-9-brief.md`)
sketched interfaces against files it had not read. What was actually built
differs in these ways, each deliberate:

- **`MomentSet` had no `provenance` field and no `id`.** The field was added
  (`qlab/core/types.py`) **last in the dataclass and outside `content_hash`**: a
  conditioned set already differs from its parent in the tensors the hash
  covers, and hashing lineage would renumber every moment set already logged.
  `summary()` writes `provenance` *after* the `diagnostics` update, so an
  estimator diagnostic cannot shadow what the referee reads. `condition()`
  therefore records `parent = ms.content_hash()`, not `ms.id`.
- **The registry needed a migration, not just a column.** `moment_sets` gained
  `provenance VARCHAR` by `ALTER TABLE … ADD COLUMN IF NOT EXISTS`, and
  `log_moment_set` switched to a **named-column** INSERT, because the new column's
  ordinal differs between a fresh desk and a migrated one. `moment_set()` parses
  it back and answers `{}` for a NULL (pre-migration) row.
- **Three registry readers were added** rather than filtering `list_runs`:
  `newest_run_of_kind`, `runs_of_kind` and `get_run`. A desk logs solves and
  backtests continuously, so scanning the newest N runs stops finding the last
  matrix as soon as N others land — and a caller checking "have I logged this
  already" then re-logs what it already has.
- **`condition()` re-establishes mean pinning as arithmetic.** The brief copied
  the mean and asserted it. The implementation computes the tilted mean, measures
  it against the parent's, records the distance as
  `provenance["mean_pinning_max_abs"]`, and discards it — so a tilt that *would*
  have moved a mean is visible in the audit rather than merely absent from the
  tensor.
- **The brief had no runner.** It said "add an arm to the yaml"; an arm with no
  caller is invariant 10's failure mode. `qlab/research/views_arm.py`
  (`MatrixViewsConditioner`) is the production caller of `views_from_matrix`: it
  walks the rebalance dates, builds the window's matrix, logs it, derives views
  against the previous window, pools them under a KL budget and conditions the
  covariance. `qlab.arms` refuses `views_source` without a conditioner and
  refuses it outright for higher-moment objectives, and the ablation runner owns
  the conditioner because `qlab.arms` holds no registry.
- **`deterministic_referee` gained a `registry` parameter** (keyword, defaulting
  to `None`) and three refusals plus one audit reason. Being given no registry is
  a refusal, never a quiet exemption.
- **Agent tool lists.** `research.qualitative_matrix` was added to `agents/atlas.md`
  and `agents/moments-analyst.md` and to the owner's `OWNER_LAB_TOOLS`.
  `moments.condition` was briefly added to `agents/moments-analyst.md` on the
  controller's instruction, against the plan's rule that no role holds it until
  the catalog entry is operational; that widening was reverted the same day
  (`fix(agents): withhold moments.condition …`), and `tests/test_agents.py` now
  pins that no role holds the tool. The tool itself refuses every call on the
  operational path regardless, so nothing conditioned was ever reachable.
- **`moments.condition` orders its gates deliberately**: views-run kind, KL
  budget, provenance verified, *then* the stage check last, so the three lineage
  refusals stay reachable and tested while the entry is research-stage.

### The fix round (`495942e`)

- The walk counts are logged as a `views_summary` run and printed by
  `qlab batch`; they are kept **out** of every arm's `metrics`, which feed the
  ranking and the deflated-Sharpe trial accounting.
- The baseline window was "the second-newest `qualitative_matrix` run by write
  time". On a registry shared with a live owner that is a matrix logged from
  *today's* news for *another* universe — look-ahead and a universe swap in one
  line. The arm now stamps its own matrices (`source: "ablation_a5"`) and reads
  back only those, dated strictly before this window, over the same ticker set.
- `provenance_verified: True` was written by assertion. It is now derived by the
  same check `research.apply_views` runs, extracted to
  `qlab/research/view_provenance.py` so there is one definition of "grounded".
  A window whose views cite a claim key the matrix does not hold is recorded with
  `provenance_verified: False` and **not** conditioned on.
- `research.qualitative_matrix` validated `as_of` and then ignored it, and never
  read `universe`. Both are honoured now; a matrix with no row for the requested
  universe refuses rather than returning an empty table.
- Two tests asserted on `inspect.getsource` string counts. Both replaced by
  behaviour: the referee's `registry` kwarg is asserted to *be* the registry
  object at both autopilot call sites, and the runner's arm wiring is covered by
  a real `run_ablation`.
- A test asserting `out.mu is parent.mu` where both were `None` now runs on a
  parent that carries a mean. The unreachable `"vol"` branch of `_typed` is gone
  (the rules emit tail and corr only).

## Promotion is a separate decision, and this is not evidence for it

`views_conditioned_min_variance` stays `stage="research"`,
`agent_runnable=False`. The offline ablation is **not** evidence either way: no
rule fired, so nothing was measured about the hypothesis. Promotion needs, at
minimum:

1. A live archive with primary documents — the only place the tail rule can fire.
2. A universe whose sleeves have more than one member — the only place the
   correlation rule can fire at all.
3. A resolution of the shrunk-vs-raw confound, so a measured difference can be
   attributed.
4. The workflow-path lineage precondition above, closed with a failing-first test.
5. Tool-authority review of what becomes agent-reachable when the stage changes.

Until then the chain is visible, auditable and inert, which is what it was built
to be.

## 2026-08-30 — residuals after the branch re-review

The re-review of the branch-review fixes confirmed all eleven items and left
five minors. Four were closed in `fix(research): the referee mirrors
condition's absence rule, and the registry drops its callerless lookup`: the
referee now FAILS a views run that claims `provenance_verified` but records no
`matrix_run_id` key (a present-but-`None` key is a quote-sourced run and
passes by design), `Registry.newest_run_of_kind` was deleted because nothing
in production called it once `matrix_runs` landed (invariant 10), the
`matrix_runs` docstring says who stamps what, and two tests pin the singular
env without an ambient plural leaking in. One is parked, deliberately: `qlab
batch` on a spec containing A5 refuses the **whole** run when invoked without
`--offline`, because the conditioner is constructed once for the spec and its
online path is not designed. The refusal names A5 and `--offline`, so it is
loud; building the conditioner per arm so the other arms still run is a small
change for whoever designs the online path.
