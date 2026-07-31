# Atlas as a reasoning desk manager

**Date:** 2026-07-31
**Status:** design accepted, step 1 implemented. Steps 2–5 are not built.
**Decision owner:** Azain, who asked for an Atlas that reasons rather than
looks up.

## Why this is not a loosening of the design boundary

The project's boundary is *AI owns judgment, algorithms own numbers,
deterministic code owns rigor*. Atlas today violates the first clause and it is
worth being precise about how.

`template_for_trigger` maps `regime_flip → regime_review`. That is a judgment
call — "given everything happening, what is worth investigating?" — implemented
as a lookup table. It is not rigor. It is a placeholder that has been mistaken
for an invariant because it happens to be deterministic.

`check_startable` refuses a template the current mode does not permit. *That* is
rigor, and it stays code.

The two were never the same thing; they are already separate functions. Making
Atlas reason means filling in the first with judgment and leaving the second
exactly as it is.

## The split

**Atlas-the-LLM decides:**

- what is worth investigating, and why now rather than later
- which registered template fits the situation — a choice, not a lookup
- how the qualitative record and the quantitative panel relate, including when
  they disagree
- what to tell the operator, and when silence is the right answer
- when the operator's stated risk preference and the evidence have come apart

**The spine stays deterministic, and is what makes the above safe to grant:**

- `check_startable` — mode gates, plan-creation boundary
- the daily workflow budget and trigger dedupe (cost and loop control)
- the referee's `targets_hash` binding
- human confirmation on any fill (invariant 3, untouched)

Atlas may *want* anything. These still refuse it. This is the same arrangement
the workforce coordinator already runs under: an LLM with broad judgment and
narrow hands, unable to trade not because it is instructed not to but because no
such tool exists in its allowlist.

## What actually blocks it: the fact surface is starved

`atlas_facts` returns nine flat fields, mostly booleans:

```
universe                ['ACWI', 'SPY', ...]
data                    {'provider': 'synthetic', 'blocked': False, ...}
portfolio               {'equity': 10000.0, 'drawdown': 0.0, ...}
regime                  {'robust_state': None, 'flip': False}
open_workflows          0
pending_approvals       0
order_anomaly           False
news_window_sufficient  True
news_window_items       19
```

`regime.flip` is a boolean. `news_window_items` is a count. There is no
*content* — nothing a reasoner could form a view about. This surface was
designed for a lookup table and it is perfectly adequate for one.

Meanwhile the material already exists elsewhere and is thrown away:

- `regime_panel().readings` — five indicators each carrying `state`, `signal`,
  its own trailing `threshold`, `percentile`, and a **one-line reasoning
  string** explaining what the number means.
- `desk_read().qualitative_signals` — six unsigned properties of the news
  record, each with its own reason and its own state.
- the grounded news window itself, with corroboration and publisher tiers.
- decisions with realized outcomes, via the reflection loop.

## The key structural rule: do not widen the gate's input

The temptation is to enrich `atlas_facts`. That is wrong.

`atlas_facts` is consumed by `check_startable`, which is the authority gate. A
gate whose input is narrow, boolean, and stable is auditable; a gate reading a
large free-form context is not. Widening it would quietly move the gate into the
same epistemic class as the thing it is supposed to constrain.

So: **`atlas_facts` stays exactly as it is**, and a separate `atlas_context`
composes the rich, abstract surface for the reasoner. Two surfaces, two
purposes. The gate keeps its narrow input; the reasoner gets everything.

That separation is step 1, and it is implemented.

## News as a first-class input

The six qualitative signals carry no sign, and that turns out to be exactly
right for an LLM consumer rather than a limitation.

Deterministic code can honestly say *19 of 20 holdings named, 15% of claims
corroborated, 16 distinct publishers, median record 19h old, credit and
infrastructure unspoken for*. It cannot honestly say what that means.

A reasoner can:

> Coverage is broad but shallow — 15% corroboration means most of this is
> single-outlet takes, so I am not treating it as established. The gap that
> matters is credit: nothing in the record touches LQD or HYG while turbulence
> has been elevated three sessions. That is a blind spot, not a calm reading.

If `corroboration_ratio` arrived pre-labelled "weak sentiment", Atlas would be
laundering a number into a view. Unsigned signals plus a reasoner is the honest
decomposition; signed signals plus a lookup is not.

## The wake loop

An LLM cannot run every 30 seconds. The heartbeat stays deterministic and cheap:
it gathers facts, computes signals, and decides whether anything *material*
changed — a drawdown tier crossing, a regime flip, a drawdown step, a news
window crossing the sufficiency floor, a workflow terminating, or the operator
speaking. Only then does it wake the reasoner.

This also makes "I looked at this overnight" honest: Atlas wakes on events, so
when it says something, something happened.

## What is given up, stated plainly

**Bit-reproducibility of Atlas's choices.** Today "why did the desk start a
rebalance review on Tuesday" has an answer that can be rerun. It will not.

**What is kept is auditability**, which is the property that matters: each Atlas
decision is a registry row carrying the facts hash it saw, the template it
chose, and its stated reasoning. What it knew and what it concluded remain
reconstructable; the output is not replayable into an identical one.

That is an acceptable trade for judgment and it is the same trade already made
with the workforce coordinator. It is **not** acceptable for the gates, which is
why they stay code.

## Risk preference as a standing instruction

A persisted, versioned profile — target volatility band, drawdown tolerance,
per-asset cap, turnover budget, objective form and `risk_aversion` — entering
Atlas's context on every wake.

Worth knowing: `max_utility` with `risk_aversion` is **already an operational
objective form** (`qlab/algorithms/catalog.py`), and nothing in the desk sets
it. It is a built dial with no handle — the same latent-dead-code shape that
produced three real bugs in this repo.

The valuable behaviour is not Atlas adjusting risk to conditions; that is market
timing with extra steps. It is Atlas holding the stated preference fixed and
naming the conflict:

> You have asked for 12% vol. Reaching it in this regime needs 40% in credit,
> which is your per-asset cap. Restate the preference, or accept the
> concentration.

A profile change is a **proposal requiring confirmation**, through the existing
approval machinery. Not because the reasoner is untrusted, but because "raise my
risk tolerance" and "buy this" are the same kind of act — one just takes effect
later. An agent that can move a drawdown kill switch on request is one
persuasive sentence from disabling the only circuit breaker.

## Build order

1. **Enrich the reasoner's surface** (`atlas_context`) — done. Independently
   useful: every client can render it.
2. **The risk profile object** — persisted, versioned, proposal-and-confirm.
3. **LLM Atlas behind a flag**, deterministic Atlas as fallback. Both write the
   same decision rows so they can be compared on identical facts.
4. **The wake loop** — material-change prefilter.
5. **Persistent session and MCP tool tiers** — the conversational surface.

Step 3 carries a condition: run both against identical facts for a period before
the lookup path is removed. If the reasoner's template choices are not better
than the table's, that is worth discovering while the table still exists.

## Not decided

- Whether Atlas's reasoning should be streamed token-by-token to the desk or
  only its conclusions recorded. Streaming is better UX and more tokens.
- What is remembered across sessions beyond the profile, and what is re-asked.
- Whether the reasoner may open a debate directly, or only request one.
