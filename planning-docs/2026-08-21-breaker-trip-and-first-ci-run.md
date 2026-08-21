# 2026-08-21 — the breaker tripped on a phantom drawdown, and CI's first run

Two operational findings from the day the desk became one command, recorded
per invariant 11.

## The kill switch fired on a drawdown no market produced

Minutes after the one-command-desk merge and an owner restart onto the live
default, the workstation went full-frame red: **HALTED**, drawdown tier
BREAKER, trailing drawdown 61.1%. Equity read $10,148 against a high-water
mark of $26,104.

The $26k mark was set while the simulated book was valued under a different
pricing lane; the restart repriced the same positions on live marks. No
position lost value — the "drawdown" was the gap between two pricing regimes
sharing one breaker baseline. The mandate's breaker cannot tell the
difference, and halting was the correct reflex for the number it was shown.

Two things were already true in the code by the time this manifested:

- `Registry.update_high_water_mark` is **lane-scoped** (`mark_lane`): the
  first ratchet anchors the lane, and a cross-lane valuation *replaces* the
  mark rather than competing with a peak priced in different units. Pinned by
  `test_a_cross_lane_valuation_cannot_ratchet_a_phantom_high_water_mark`.
- `reset_book` nulls `mark_lane`, so a fresh book re-anchors to whichever
  feed prices it next.

What the fix deliberately does **not** do is retro-heal a mark contaminated
before lane-scoping existed: a pre-column mark is anchored, not rebased,
because silently rebasing every upgraded desk would erode legitimate breaker
baselines. A desk carrying a phantom mark from that era trips once and is
cleared with `POST /api/reset` — which is what was done here. The alarm
itself worked exactly as designed: the halted desk was unmissable.

## CI's first run: one real signal, two environment gaps

The workflow's inaugural run (PR #5's merge commit): Rust green on
ubuntu/macos with clippy and fmt clean, and three findings —

1. **rust · windows**: 5 failures, all in source-scanning authority tests
   (write-call-site census, credential-plaintext locality, one-producer
   rules). Cause: CRLF checkouts on the Windows runner feeding byte-exact
   scans. Remedy: `.gitattributes` forcing LF everywhere.
2. **python · all three OSs**: the CI env installed no `trader` extra, so the
   alpaca credential-error tests saw the "install qlab[trader]" refusal
   instead of the errors they pin. Remedy: `trader` joins the CI install —
   import-availability only, no keys.
3. **python · all three OSs**: CI ran 3.12 while the suite is developed on
   3.13 and asserts on stdlib error wording (json's "trailing comma", and
   four `test_llm_config` failures suspected of the same class). Remedy: CI
   runs 3.13. If the llm_config four stay red on 3.13, they are not a
   version-wording issue and get their own investigation.

A workflow's first run failing on environment rather than on code is the
workflow doing its job: none of these could have been found on the machine
the suite was written on.
