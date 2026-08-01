# The predictor board, measured — the rescue paths stop the bleeding and win nothing

**Date:** 2026-07-31
**Branch:** `worktree-quantum-predictor-surface`
**Result:** the two rescue paths from `planning-docs/2026-07-30-ml-lane.md`
(group-wise ridge penalties, closed-form quantum kernels) **remove the
measured harm of the augmentation but produce no edge over the ridge
baseline.** Everything stays research-stage, admission-gated, advisory.
Reported in full because a null result that closes a ranked hypothesis list
is the useful part.

## Protocol

Same shape as the 2026-07-30 measurement, run through the new board so every
model is paired by construction:

- 12 synthetic seeds (1–12), the owner's own generator (regime chain,
  fat tails, mixed correlations), core universe (20 ETFs), `as_of`
  2026-06-30, 756 return observations.
- Per seed: `run_predictor_board(panel)` — 5 shared purged walk-forward
  outer folds, 21-day embargo, inner-fold hyperparameter search
  (`alphas × alphas` for group-wise; `alphas × map_weights` for kernels).
- Per model: mean outer-fold Spearman IC per seed; wins and the paired t are
  across the 12 seed-level differences vs `ridge:none`.
- Seed-keyed synthetic cache (the 2026-07-30 bug's guard) confirmed active:
  cross-seed IC sd ≈ 0.17 — no zero-variance symptom.

## The table

| model | mean IC | sd | wins vs baseline | paired t | verdict |
|---|---|---|---|---|---|
| ridge:none | 0.0984 | 0.169 | — | — | baseline |
| kernel:linear | 0.0984 | 0.169 | 0/12 (identical) | 0.00 | identity check holds |
| kernel:zz | 0.0766 | 0.161 | 7/12 | −0.56 | no effect |
| groupwise:angle_zz | 0.0703 | 0.142 | 5/12 | −0.69 | no effect |
| groupwise:zz | 0.0656 | 0.122 | 5/12 | −0.79 | no effect |
| kernel:angle | 0.0382 | 0.201 | 4/12 | −1.77 | leans harmful |
| groupwise:angle | 0.0128 | 0.173 | 5/12 | −1.99 | leans harmful |

## What moved, and what did not

**The structural failure is gone.** The 2026-07-30 measurement had explicit
ZZ at **0/12 wins, paired t −4.53** — variance inflation from 48 near-collinear
columns under one global alpha. The kernel form of the same map is 7/12 and
−0.56: statistically indistinguishable from the baseline. The diagnosis was
right, and the fix worked *as a fix*.

**There is still nothing to win.** No variant beats the baseline on mean IC.
The interaction structure the ZZ map encodes either is not present in the
panel's vol dynamics, or is already captured by the raw lagged-risk features.
The angle map remains mildly harmful in both formulations — a smooth
monotone re-basis of already-informative features spends capacity and adds
none, which is now measured twice, two different ways.

**The identity check earned its slot.** `kernel:linear` reproduced
`ridge:none` to the fold level in all 12 seeds — the dual/primal equivalence
the tests pin at 1e-8 held live, which is what makes the rest of the table
interpretable as an effect of the *kernels* rather than of the solver swap.

## Decisions

1. The board ships as built: `research.predictor_board` (owner-only),
   `atlas_context["predictors"]`, and both TUI cards render whatever the
   evidence says — today that is "no admitted model" or a seed-lucky
   champion with its admission verdict attached.
2. No default changes: `predict_vol_ridge` stays the admitted v1 forecaster;
   `augmentation` stays `"none"`.
3. Rescue path #3 from the ml-lane doc — fewer, story-backed pairs instead
   of all `n(n-1)/2` — is the only remaining live hypothesis. More data and
   more folds remain explicitly not worth trying; both failures were
   structural, not statistical.

Sweep artifacts (per-seed ICs): session scratchpad `board_sweep.json`; the
protocol is reproducible from `qlab/research/board.py` with the seeds above.
