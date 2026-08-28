# 2026-08-28 — Primary sources and the qualitative matrix: design

The spec behind `2026-08-28-primary-sources-plan.md`. Two streams, one after
the other, both inside the boundary the project is built on: agents own
judgment, algorithms own numbers, deterministic code owns rigor.

## Why

Atlas keeps reporting the same fact about the qualitative record: one
publisher (Benzinga, through Alpaca), zero corroborated claims, the commodity
and rates sleeves silent. The grounding code already knows what would fix it —
`Claim.corroborated` is true for a **primary** source standing alone or two
independent **secondary** publishers agreeing — but nothing feeds it a primary
source, and only one secondary publisher ever arrives. More headlines from the
same feed cannot change that verdict. Primary documents and independent
publishers can.

And once the record can establish things, the desk has no way to let it touch
the numbers. `qlab/core/views.py` already carries the deterministic core — risk
views on volatility, correlation and tail mass, entropy-pooled with **every
mean pinned**, a KL budget capping the tilt — but it is reachable only through
operator-pasted excerpts (`news_risk_review`), and its output is research-only
by construction. The matrix is the bridge: a deterministic, point-in-time
summary of the record, mapped by explicit bounded rules into those same views,
with a promotion path that runs through the catalog like every other method.

## Stream D — primary sources

### D0. What stays fixed

- **The provider contract.** `ProviderFetch = Callable[[datetime, tuple[str, ...]], list[NewsItem]]`
  in `qlab/news/feed.py`, registered by name in `PROVIDERS`. New providers are
  new entries; nothing upstream of `fetch_news` learns their names.
- **The look-ahead boundary.** `ground()` drops anything published at or after
  `as_of`. A provider that dates a record by *acceptance time* (EDGAR's
  `acceptanceDateTime`) is point-in-time honest; one that used filing *date*
  would leak a same-day filing into a morning read. The plan pins the former.
- **Tiering by source name.** `source_tier()` reads `PRIMARY_SOURCE_MARKERS`
  ("sec", "edgar", "bls", "eia", "federal reserve", "treasury"…). A provider
  whose `NewsItem.source` carries the marker is primary with no new code path.
- **The archive is provider-agnostic** (`build_archive_batch`,
  `registry.record_news_items`), but `ArchiveBatch.provider` is one name and
  `MACRO_LANE_PROVIDERS` decides who may keep untagged (macro) items. Both
  widen by data, not by structure.
- **Fail loud.** The RSS provider refuses if any configured feed is
  unreachable so a shrinking window cannot pass for a quiet market. Every new
  provider keeps that rule; a *stack* of providers reports each member's
  outcome separately.

### D1. Stacked providers

`QLAB_NEWS_PROVIDERS=alpaca,edgar,gdelt` (plural) selects a stack. `fetch_news`
gains `fetch_news_stacked(as_of, universe, providers) -> StackedWindow`: items
from every member, each still labeled with its own `provider`, plus a
per-member `outcomes` map (`ok` / the error sentence) so a dead member is
reported, never absorbed. Grounding then clusters across providers, which is
what turns "one outlet" into corroboration. The singular variable keeps
working and means a stack of one. The owner's `news_provider_for` returns the
stack; `archive_desk_news` archives **one batch per member** — `ArchiveBatch`
keeps its single `provider` and the `news_archive` event stays per provider,
so the bus row an operator reads still names one source.

### D2. Installable and shareable

- First-party providers live in `qlab/news/providers/` and use **stdlib only**
  (`urllib`, `json`, `xml.etree`) — `pip install qlab[news]` adds nothing but
  `feedparser`, exactly as today. No keys: EDGAR, BLS, EIA, Treasury and GDELT
  are public.
- **Third-party providers** register through an entry-point group,
  `qlab.news.providers`: a pip-installable package declares
  `[project.entry-points."qlab.news.providers"] mysource = "pkg.mod:fetch"`
  and `feed.py` discovers it lazily. That is the shareable unit: someone can
  publish a provider without touching qlab.
- **Source lists are data, shipped.** `configs/news_sources.yaml` grows `edgar`
  and `gdelt` sections; the file joins `[tool.setuptools.data-files]` (it is
  read through `data_path` and is currently *not* installed in a wheel — the
  rss provider would fail loudly on a clean install; the plan fixes that and
  removes the stale `index.html` package-data entry).
- **`qlab news-check --provider edgar`** exercises one member, and with no
  argument the whole stack, reporting per member.

### D3. The providers

- **`edgar`** (primary). Ticker→CIK from `company_tickers.json` (cached under
  `state_path("news_cache")`, refreshed weekly); per CIK the submissions API
  `data.sec.gov/submissions/CIK##########.json`; one `NewsItem` per filing of
  a kept form (`8-K`, `10-Q`, `10-K`, `N-PORT`, `N-CSR`, `13F-HR`) inside the
  window, `published = acceptanceDateTime` (UTC), `source = "SEC EDGAR"`,
  headline `"{form} — {name}"`, url the filing index, tickers from the map
  plus the `issuers:` list in `news_sources.yaml` (a curated, shareable list of
  constituent issuers per ETF — phase 1 holdings knowledge is the operator's
  list; N-PORT parsing is a later stream). SEC requires a descriptive
  `User-Agent` with a contact; the provider refuses to run without
  `QLAB_EDGAR_CONTACT` set, and says so — silently sending a fake identity is
  the kind of thing this desk does not do.
- **`macro`** (primary). BLS (`bls.gov/feed/bls_latest.rss`), BEA, EIA and
  Treasury press RSS, through the existing RSS machinery but registered as
  their own provider so the stack can name it. A `calendar:` section in the
  config carries *scheduled* releases (FOMC, CPI, payrolls, auctions) — served
  as an `upcoming` block for Atlas, **never as news**: a future-dated record
  would be dropped by the look-ahead gate, and rightly.
- **`gdelt`** (secondary, many publishers). GDELT 2.0 DOC API
  (`api.gdeltproject.org/api/v2/doc/doc?mode=artlist&format=json`), one query
  per keyword rule in the config, `source = <article domain>`. Its job is
  independent publishers: it is what makes a secondary claim corroborable.

## Stream E — the qualitative matrix

### E0. What the matrix is, and is not

A **deterministic, point-in-time table of record properties**: rows are the
universe's tickers (and their asset classes), columns are things the archive
can count without reading a market — coverage, distinct publishers,
corroborated claims, primary documents, filing recency, event proximity (days
to the next scheduled release that maps to the row). It has **no sign and no
direction**, per `qualitative.py`'s doctrine: a column that said "coverage is
heavy and negative" and fed an allocation would be a return forecast wearing
a qualitative label. Each cell carries the claim hashes it was computed from,
so a number on the matrix is traceable to archive rows. It is persisted as a
run of kind `qualitative_matrix` keyed by the window fingerprint, and served
on `/api/research/qualitative` and into `atlas_context`.

### E1. Matrix → views, by rule

`qlab/research/matrix_views.py` maps matrix cells to the **existing** view
types by explicit, bounded, unit-tested rules — for example, an asset whose
corroborated primary-document count in the window exceeds its trailing
baseline emits a `TailView(direction="fatter")` with confidence scaled to the
excess and capped at 0.5; a sleeve whose coverage concentrates on one name
emits a `CorrView` raising that name's correlation to its sleeve, capped. No
rule may emit a return view, because no view type can carry one. Provenance
travels: each view's `source_quote` is replaced by `source_claims` (claim
keys), and `research.apply_views`' provenance gate learns to verify claim keys
against the archive exactly as it verifies quotes against an excerpt today.
The output is a `views` run bound to the matrix run.

### E2. Into the optimizer, through the catalog

`moments.condition(moment_set_id, views_run_id)` produces a new `MomentSet`
whose covariance is `conditioned_moments()` under the views' tilted
probabilities and whose **mean is the original moment set's** (pinned by
construction, re-asserted here). It is logged with lineage to both parents. A
catalog entry `views_conditioned_min_variance` enters at stage **`research`**,
beside `regime_min_variance`, and an ablation arm in
`configs/specs/ablation_v1.yaml` measures it out of sample against HRP, ERC and
the unconditioned arm. Promotion to `operational` — the point at which the
paper policy may select it — requires that evidence, a catalog stage change,
tool-authority review and governance tests, exactly as the README states for
every method. The referee gains one check: an operational solve on a
conditioned moment set must cite a persisted matrix and a views run whose KL
total is within budget, or it fails. Nothing here can create a plan; the
existing gates stay where they are.

## Not in these streams

N-PORT holdings parsing (point-in-time constituents), an LLM-judged goal
gate, and any signed sentiment column. Each is a separate, later decision.
