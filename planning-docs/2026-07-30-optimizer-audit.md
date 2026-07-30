# Optimizer audit — speed, quality, determinism

**Date:** 2026-07-30
**Scope:** every staged solver (`hrp`, `risk_parity`, `classical`,
`classical_multistart`, `cvar_lp`) on a common synthetic panel.
**Status:** one finding fixed in code; the rest are guidance.

Every number here was measured, not estimated. The harness builds a correlated
return panel (market factor + idiosyncratic noise, 750 observations), estimates
the same `MomentSet`, and runs each solver three times under
`Constraints(max_weight=0.40)`.

## The matrix

`eff N` is 1/HHI — the effective number of positions, which is the honest way
to read concentration when a cap is binding.

| solver | n | median s | ann vol | max w | eff N | determ | feasible |
|---|---|---|---|---|---|---|---|
| hrp | 7 | 0.0001 | 0.1358 | 0.365 | 4.51 | yes | yes |
| risk_parity | 7 | 0.0012 | 0.1495 | 0.228 | 6.42 | yes | yes |
| classical | 7 | 0.0008 | 0.1272 | 0.400 | 3.20 | yes | yes |
| classical_multistart | 7 | 0.0339 | 0.1350 | 0.400 | 3.01 | yes | yes |
| cvar_lp | 7 | 0.0060 | 0.1276 | 0.400 | 3.12 | yes | yes |
| hrp | 25 | 0.0004 | 0.1229 | 0.127 | 16.00 | yes | yes |
| risk_parity | 25 | 0.0055 | 0.1382 | 0.074 | 22.22 | yes | yes |
| classical | 25 | 0.0056 | 0.0909 | 0.400 | 3.68 | yes | yes |
| classical_multistart | 25 | 6.7491 | 0.1565 | 0.336 | 5.05 | yes | yes |
| cvar_lp | 25 | 0.0089 | 0.0912 | 0.364 | 3.89 | yes | yes |
| hrp | 60 | 0.0007 | 0.1300 | 0.059 | 41.45 | yes | yes |
| risk_parity | 60 | 0.0133 | 0.1428 | 0.032 | 54.02 | yes | yes |
| classical | 60 | 0.0223 | 0.0962 | 0.304 | 6.19 | yes | yes |
| cvar_lp | 60 | 0.0166 | 0.1041 | 0.252 | 7.50 | yes | yes |

**Every solver is deterministic and every solution is feasible.** The constraint
layer holds under all of them, which is the one result that had to come back
clean.

## Finding 1 — multistart burned 90% of its time for nothing (fixed)

`classical_multistart` was two to three orders of magnitude slower than
everything else: 6.7s at n=25 against 5.6ms for `classical`. Isolating the cost
showed it is not the `n⁴` cokurt contraction — one objective evaluation is
0.85ms — but the restart count. Each restart costs a flat ~20ms, and the budget
is `max(8, 4n)`: **100 restarts at n=25, 160 at n=40.**

So: are the restarts worth it? First measurement said no — on a single-factor
panel at λ=0.3, restart counts from 1 to 200 returned the *identical* objective
to eight decimal places. That reading was wrong, and it is worth recording why:
a single-factor MVSK landscape is effectively convex, so it cannot discriminate.

On genuinely frustrated landscapes the restarts matter a great deal:

| landscape | 1 restart | 4n restarts | gain |
|---|---|---|---|
| single-factor, λ=0.3 | -0.033613 | -0.033613 | 0.00% |
| single-factor, λ=3.0 | -0.364449 | -0.493376 | 26.13% |
| 5-factor, λ=3.0 | -1.329877 | -2.399409 | 44.57% |
| 5-factor, λ=10.0 | -4.511409 | -8.129358 | 44.50% |
| 5-factor, λ=10.0, n=40 | -0.310542 | -0.707130 | 56.08% |

So the fix is not a smaller budget. Sweeping the restart index shows **the
winning basin is found at restart 2–4 on every landscape**, convex or not — and
then 96 to 156 further restarts change nothing.

`ClassicalMultistartSolver` now stops after 12 consecutive restarts with no
improvement (a 3–4× margin over the worst case observed, because one wasted
restart costs 20ms and one premature stop costs 26–56% of objective):

| landscape | full | early | speedup | objective delta | restarts run |
|---|---|---|---|---|---|
| single-factor λ=0.3, n=25 | 3.735s | 0.441s | 8.5× | 0.000000% | 12/100 |
| 5-factor λ=3.0, n=25 | 3.872s | 0.469s | 8.2× | 0.000000% | 14/100 |
| 5-factor λ=10.0, n=40 | 71.141s | 6.515s | 10.9× | 0.000000% | 15/160 |

**8–11× faster, bit-identical answers.** The n=40 case is the one that matters:
71s is not a solve an operator waits for, and 6.5s is.

The temperature schedule is still computed over the full budget, so an early
stop takes a *prefix* of the same starts rather than a different, hotter set —
stopping changes how many points are explored, never which. `patience=None`
restores the exhaustive sweep. Diagnostics report `n_starts`, `n_starts_run`,
and `stopped_early`, so an audit reading a budget of 160 cannot believe 160
restarts happened.

## Finding 2 — min-variance concentrates to the cap, and that is the design

`classical` produces the lowest volatility at every size (0.0909 at n=25 against
HRP's 0.1229) and pays for it in concentration: max weight sits exactly on the
0.40 cap, with an effective 3.68 positions out of 25. At n=60 it is 6.19 out of
60. HRP gives 16.00 and 41.45 respectively.

This is the textbook min-variance behaviour, not a bug — the estimator loads
onto whichever assets have the smallest estimated covariance, and estimation
error there is exactly where it is largest. It is worth stating plainly because
the volatility column makes `classical` look like a free win, and it is not:
**the vol advantage is in-sample, the concentration is out-of-sample risk.**

Guidance: the operational policy default (`hrp`) is the right default. Reach for
`min_variance` when the covariance estimate is genuinely trusted — short
lookback, post-denoising, few assets — and expect a 3–6 name portfolio.

## Finding 3 — the CVaR LP scales better than SLSQP

Not expected, and worth using. `cvar_lp` is slower than `classical` at n=7
(6.0ms vs 0.8ms) and n=25 (8.9ms vs 5.6ms), but **faster at n=60** (16.6ms vs
22.3ms), while producing similar volatility (0.1041 vs 0.0962) with meaningfully
better diversification (eff N 7.50 vs 6.19) — from a tail objective rather than
a variance one.

The crossover is the LP's linear scaling against SLSQP's quadratic-ish
constraint handling. On a universe past ~50 names, scenario-CVaR is the
cheaper *and* the better-diversified choice.

## Finding 4 — the `n⁴` cokurt tensor is the real ceiling on MVSK

At n=25 the cokurt is 390,625 entries (3.1 MB); at n=60 it is 12,960,000
(104 MB), and building it via `einsum` over 750 observations dominates
everything else. MVSK is not a large-universe method and should not be presented
as one. `mvsk_multistart` and `mvsk_vol_target` are both `research` stage, which
is correct — but the reason is worth recording: it is a memory wall, not a
governance preference.

Practical ceiling: **MVSK is comfortable to ~25 assets, painful at 40, and
should not be attempted past ~50** without a factor-structured cokurt
approximation.

## What was not measured

- Out-of-sample quality. Every number here is in-sample on synthetic data, so
  the volatility column ranks solvers on the objective they were given, not on
  whether that objective was the right one. Ranking allocators honestly needs
  walk-forward with a deflated Sharpe, which belongs with the ML lane work.
- `dirac3` (research, external service) and the offline quantum arms.
- Real market data. The synthetic panel has a clean factor structure; real
  covariance matrices are worse conditioned, which typically helps HRP and hurts
  min-variance further.

## Reproducing

The benchmark harness is not checked in — it is scratch. The findings that
matter are encoded as tests in `tests/test_solvers.py`:
`test_early_stopping_finds_the_same_optimum_as_the_full_budget` and its
companion `test_this_landscape_actually_needs_restarts`, which exists to keep
the first from being vacuous if the landscape ever stops discriminating.
