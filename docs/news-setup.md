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

## Guided setup

```bash
qlab news-setup
```

Asks, in order: whether this desk should read real news at all; then each
source in turn with its tier, what it needs, and what it costs; then the SEC
contact when `edgar` is chosen; then a second confirmation for `gdelt`; then
whether to check the choice live before saving. Nothing is written until the
last question is answered, and a source that cannot work — `alpaca` with no
resolvable credential — is refused with the fix rather than saved to fail on
the first heartbeat.

Answering **no** to the first question is a real answer, not a failure: it
writes `QLAB_NEWS_PROVIDERS=synthetic`, and the desk then labels its narrative
`synthetic (demo)` everywhere it appears.

What is written, to `.env` at the workspace root, is exactly two lines —
`QLAB_NEWS_PROVIDERS` and, when `edgar` is chosen, `QLAB_EDGAR_CONTACT`. Every
other line in the file is left byte for byte as it was, `export` prefixes
included. The contact goes into the User-Agent of EDGAR requests and is sent
to the SEC and nowhere else. The owner reads `.env` at startup, so:

```bash
qlab --restart runtime      # the running desk picks the new lines up
```

The scripted spelling takes no prompts and is what a CI-style caller uses:

```bash
qlab news-setup --providers alpaca,edgar,macro \
    --edgar-contact "Your Name <you@example.org>" --no-verify
```

Without a terminal and without `--providers`, the verb refuses rather than
choosing for you.

### The startup door

Bare `qlab` asks once, and only where the question is worth asking — a live
lane, a terminal, and no `--yes`:

- **Nothing configured**: it says what the desk would read by default (Alpaca
  if a credential resolves, otherwise the labelled fixtures) and offers the
  wizard above.
- **The stack names `edgar` with no contact**: `enter` prompts for the contact
  and saves that one line; `drop` runs this session without `edgar` and saves
  nothing.

Anything else starts silently. `qlab owner` never prompts.

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
export QLAB_NEWS_PROVIDERS=alpaca,edgar,macro
export QLAB_EDGAR_CONTACT="Your Name your@email"   # the SEC requires a contact
```

`gdelt` is deliberately absent from that line — see below.

`QLAB_NEWS_PROVIDERS` (plural) wins over `QLAB_NEWS_PROVIDER` (singular); a
stack of one behaves exactly like the single provider it names.

The plural variable governs the desk's own feed, not just the CLI: the owner's
`news.fetch` reads every member you name and reports each member's outcome
beside the merged window. The internal single-provider API (`fetch_news`)
refuses a stack of more than one rather than quietly reading its first member —
if you see that refusal, the caller wants `fetch_news_stacked`.

A member that goes away is **reported, never absorbed**: the window keeps the
records the living members returned, each member's outcome travels beside them,
and the archive files one batch and one `news_archive` event per member so a
gap is attributable to the source that had it. Only a stack where *every*
member failed is an outage, and that raises the way a single dead provider
always did.

The same rule holds one level down. A provider that reads several feeds —
`macro` reads one per publisher — keeps the feeds that answered and reports the
ones that did not as `partial: <feed>: <error>`. Its records are archived
normally; only a provider whose feeds are *all* dead is a dead member.

### `gdelt` is opt-in

`gdelt` is never part of a shipped default stack, and adding it is a decision
about latency rather than about coverage. Measured on 2026-08-28: one DOC API
request returned in **43 seconds**, and a second **hung past 75 seconds**. The
provider issues one request per configured rule, and a stack member's fetch runs
on the owner's **heartbeat** — so an operator who puts `gdelt` in the stack is
accepting ticks that can take minutes. Name it when you want its corroboration
and can pay for it; leave it out otherwise.

`QLAB_EDGAR_CONTACT` is not optional for `edgar`: the SEC asks that automated
requests identify a contact, and the provider refuses rather than sending an
anonymous one.

## Checking it

```bash
qlab news-check
qlab news-check --provider macro,gdelt      # a stack, or one member of it
qlab news-check --provider acme             # an installed plugin provider
```

`--provider` accepts a plugin name as readily as a built-in one: a pip package
declaring a `qlab.news.providers` entry point is discovered on first use, so
there is no separate registration step and no need to name it in the stack
first.

Reports the provider, whether a credential resolved, and — if the window comes
back empty — whether that is an outage or a genuinely quiet market. Those are
opposite facts and the desk never conflates them: a news outage appears as
`NEWS FEED UNAVAILABLE` with the reason, not as silence.

A stack gets one block per member, so a healthy stack with one dead source
still names the source that died. Illustration of the shape, not a recording of
a real run:

```
news integration: OK
  stack              macro,gdelt

  provider           macro  [OK]
  alpaca credentials absent
  fetched/kept       12/12
  claims             11 (4 well-supported, 11 primary)
  publishers         apps.bea.gov

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
