# News setup

The desk reads a point-in-time news window and interprets it alongside the
quantitative panel. This is how to make that window real rather than synthetic.

## The providers

| provider | what it is | tier | when it is used |
|---|---|---|---|
| `synthetic` | deterministic fixtures | — | always offline, and whenever nothing better resolves |
| `alpaca` | Alpaca's news API, symbol-tagged, real date ranges | secondary | a live desk with a resolvable Alpaca credential |
| `rss` | public feeds, keyword-matched to the universe | secondary | when named explicitly |
| `edgar` | SEC filings, dated by acceptance time | primary | when named; needs `QLAB_EDGAR_CONTACT` |
| `macro` | official releases — BLS, BEA, EIA, Treasury | primary | when named; no credential |
| `gdelt` | many publishers, one article per domain | secondary | when named; no credential |

`edgar` and `macro` are the primary sources: a primary claim stands on its own,
where a secondary one needs a second publisher before the desk calls it
established. `gdelt` is what supplies that second publisher, since a single
wire cannot corroborate itself.

`synthetic` is not a degraded `alpaca`. It is a fixture generator, and the desk
labels it `synthetic (demo)` everywhere it appears, because a deterministic
narrative next to real prices would be worse than no narrative at all.

## Making it real

News follows the data lane. If the desk is on **live** data and an Alpaca
credential resolves, news uses Alpaca automatically — you do not set anything.

So the whole setup is: sign in, and put the desk on a live lane.

```bash
# 1. Sign in. Browser OAuth, paper-only by construction — this flow cannot
#    reach a live account even if you wanted it to.
alpaca profile login

# 2. Start the desk on the live data lane. The book stays simulated unless you
#    also pass --alpaca-book, so this reads real prices and real news without
#    touching your paper account.
qlab
```

Confirm it in **Settings → DESK MODE**: it reports the resolved credential, and
the Atlas read's footer names the provider it actually used.

### Or with API keys instead

```bash
export ALPACA_API_KEY=...
export ALPACA_API_SECRET=...
```

Either credential works — the resolver checks the environment first, then the
CLI profile. Note the CLI names its secret `ALPACA_SECRET_KEY` while qlab uses
`ALPACA_API_SECRET`; they are the same value under different names.

### Or force a specific provider

```bash
export QLAB_NEWS_PROVIDER=alpaca   # or rss, edgar, macro, gdelt, synthetic
```

An explicit setting always wins. Naming a provider is an instruction, so the
desk does not second-guess it — including naming `synthetic` on a live desk if
you deliberately want fixtures.

## Stacking providers

One source is one publisher's view. Name several and the desk reads all of
them, in order, and merges the windows:

```bash
export QLAB_NEWS_PROVIDERS=alpaca,edgar,macro,gdelt
export QLAB_EDGAR_CONTACT="Your Name your@email"   # the SEC requires a contact
```

`QLAB_NEWS_PROVIDERS` (plural) wins over `QLAB_NEWS_PROVIDER` (singular); a
stack of one behaves exactly like the single provider it names.

A member that goes away is **reported, never absorbed**: the window keeps the
records the living members returned, each member's outcome travels beside them,
and the archive files one batch and one `news_archive` event per member so a
gap is attributable to the source that had it. Only a stack where *every*
member failed is an outage, and that raises the way a single dead provider
always did.

`QLAB_EDGAR_CONTACT` is not optional for `edgar`: the SEC asks that automated
requests identify a contact, and the provider refuses rather than sending an
anonymous one.

## Checking it

```bash
qlab news-check
qlab news-check --provider macro,gdelt      # a stack, or one member of it
```

Reports the provider, whether a credential resolved, and — if the window comes
back empty — whether that is an outage or a genuinely quiet market. Those are
opposite facts and the desk never conflates them: a news outage appears as
`NEWS FEED UNAVAILABLE` with the reason, not as silence.

A stack gets one block per member, so a healthy stack with one dead source
still names the source that died:

```
news integration: OK
  stack              macro,gdelt

  provider           macro  [OK]
  alpaca credentials absent
  fetched/kept       31/31
  claims             28 (12 well-supported, 28 primary)
  publishers         bls.gov, eia.gov, home.treasury.gov

  provider           gdelt  [NOT WORKING]
  alpaca credentials absent
  error              HTTPError: HTTP Error 503: Service Unavailable
```

The overall line reads OK when any member answered — one living member is
still a record — and the exit code follows it.

## What the desk does with it

The window is **grounded** before anything interprets it: the point-in-time
boundary is enforced, each record is hashed so an edited headline is a new
record rather than a silent rewrite, and stories are clustered so corroboration
is visible. Only then does Atlas read it.

Two consequences worth knowing:

- A single-source story is visible in the window but is not promoted as
  established. Primary documents and multi-publisher stories are.
- Atlas never turns a headline into a weight. The news read informs a
  qualitative view; targets come from the optimizer, and every trade still
  needs your explicit approval.

## Troubleshooting

**"provider is 'alpaca' but no credential resolves"** — run
`alpaca profile login`, or export the two env vars. The message names both.

**News says `synthetic (demo)` on a live desk** — no credential resolved. Check
Settings → DESK MODE; it will say `not signed in` and name the reason.

**`alpaca news credentials unusable: …`** — a profile exists but does not parse,
or declares itself live. A malformed credential source is reported as a fault
rather than as absence, because those need different fixes.

**Empty window, no error** — a genuinely quiet 48 hours for this universe. The
read will say the qualitative record is thin rather than inventing a story.
