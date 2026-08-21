# News setup

The desk reads a point-in-time news window and interprets it alongside the
quantitative panel. This is how to make that window real rather than synthetic.

## The three providers

| provider | what it is | when it is used |
|---|---|---|
| `synthetic` | deterministic fixtures | always offline, and whenever nothing better resolves |
| `alpaca` | Alpaca's news API, symbol-tagged, real date ranges | a live desk with a resolvable Alpaca credential |
| `rss` | public feeds, keyword-matched to the universe | when named explicitly |

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
export QLAB_NEWS_PROVIDER=alpaca   # or rss, or synthetic
```

An explicit setting always wins. Naming a provider is an instruction, so the
desk does not second-guess it — including naming `synthetic` on a live desk if
you deliberately want fixtures.

## Checking it

```bash
qlab news-check
```

Reports the provider, whether a credential resolved, and — if the window comes
back empty — whether that is an outage or a genuinely quiet market. Those are
opposite facts and the desk never conflates them: a news outage appears as
`NEWS FEED UNAVAILABLE` with the reason, not as silence.

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
