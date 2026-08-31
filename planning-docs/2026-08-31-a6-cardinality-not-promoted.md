# A6 (cardinal min-variance) met the promotion threshold and was not promoted

Date: 2026-08-31 · Task G2 of the One Desk plan · status: **negative result, recorded**

## The pre-registered gate

Promote `cardinal_min_variance` from `research` to `operational` if, in one
offline run of `configs/specs/ablation_v1.yaml`:

- A6's sortino ≥ B2's (HRP), **and**
- A6's max drawdown is no worse than B2's by more than 1 percentage point.

## The numbers (offline, seed 7, isolated registry)

| arm | what it is | sortino | max drawdown | ann return | ann vol | deflated Sharpe |
|-----|-----------|--------:|-------------:|-----------:|--------:|----------------:|
| A1  | min-variance, all 7 names | 0.6984 | −0.2093 | 0.0344 | 0.0732 | 0.8420 |
| B2  | HRP (the real bar)        | 0.6565 | −0.2063 | 0.0330 | 0.0749 | 0.8127 |
| B3  | equal risk contribution   | 0.5611 | −0.2105 | 0.0282 | 0.0748 | 0.7369 |
| **A6** | **exact 4-of-7, then min-variance** | **0.9485** | **−0.1763** | **0.0560** | **0.0867** | **0.9570** |

The gate is met on both legs: sortino +0.2920 over B2, drawdown 3.00pp
*better*, not worse. A6 also tops every other arm in the matrix.

## Why that is not believed

The brief says a dominating A6 is evidence of a bug until shown otherwise. Two
checks were run.

**Look-ahead: none found.** Handing `solve_arm` the full 2008–2024 panel and
handing it the panel truncated at the same `as_of` produces byte-identical
weights at every date tested. `DataSnapshot` truncates at construction and the
selection reads nothing but the moment set estimated from that window. This is
now a regression test
(`test_the_a6_basket_is_identical_whether_or_not_the_future_is_in_the_panel`).

**The selection is a constant, not a mechanism.** Over the 57 rebalances with a
full 756-day window, A6 picks the *same* basket 56 times:

```
56  ('ACWI', 'BNDW', 'GLD', 'VNQ')
 1  ('ACWI', 'BNDW', 'GLD', 'GSG')
```

So the arm does not contain 57 out-of-sample selection decisions. It contains
**one**, repeated. Everything the ablation reports about A6 is the performance
of a single fixed sub-universe, and n=1 is not evidence that k-of-N selection
works — it is evidence that this one basket did well on this one panel.

**Read that constancy correctly: it is a property of the generator, not a
discovery about k-of-N, and the selector is not broken.** In
`qlab/core/data.py::_synthetic_prices` each asset's annualised vol and drift are
derived from `md5(ticker)`, so relative volatilities are *stationary for the
whole history*; the correlation structure comes from fixed factor loadings; and
consecutive quarterly rebalances share about 92% of a 756-day window. The
selection objective keys on exactly those two things — inverse vol and
|correlation| — so a near-constant basket is the *necessary* consequence of a
stationary panel read through overlapping windows. Nobody should later "fix"
the selector to make it churn: churn on this data would be the bug. What it does
mean is that this panel cannot exercise selection, so it cannot be the panel
that promotes it.

**The margin shrinks across draws — but this sweep is weaker than it looks.**
Re-running A1/B2/A6 on four synthetic panels. State the limitation plainly: the
seed does **not** resample the per-asset volatility profile. `ann_vol` and
`drift` come from `md5(ticker)` and are identical in every seed; only the factor
loadings, the regime path and the shocks resample. Since the selector keys on
inverse vol and |correlation|, the seed sweep largely re-runs the *same
selection problem* against different noise. It is a check on luck-of-the-path,
not on luck-of-the-basket. A real robustness sweep has to vary the volatility
profile itself — different and wider ticker sets — and that has not been run.

| seed | A1 sortino | B2 sortino | A6 sortino | A6 − B2 | A6 max_dd | B2 max_dd |
|-----:|-----------:|-----------:|-----------:|--------:|----------:|----------:|
| 7    | 0.6984 | 0.6565 | 0.9485 | **+0.2920** | −0.1763 | −0.2063 |
| 11   | 0.1313 | 0.0611 | 0.2883 | +0.2272 | −0.2119 | −0.2887 |
| 23   | −0.6178 | −0.6471 | −0.5753 | +0.0718 | −0.4050 | −0.4021 |
| 42   | −0.4404 | −0.4268 | −0.4234 | +0.0034 | −0.3818 | −0.4094 |

The sweep is not broken — there is real variance, and the *sign* is positive on
all four draws — but the headline seed-7 margin is the best of four and is two
orders of magnitude larger than the worst.

**The confidence intervals overlap almost completely.** One scale trap to avoid:
`sortino_ci` is a **per-period (daily)** statistic, while the `sortino` column in
the table above is **annualised**. Both are given here at both scales so the two
cannot be misread as the same number.

| arm | sortino (annualised) | sortino CI (per-period) | sortino CI (annualised, ×√252) |
|-----|---------------------:|------------------------:|-------------------------------:|
| A1  | 0.6984 | [0.0004, 0.0963] | [0.0063, 1.5287] |
| B2  | 0.6565 | [−0.0010, 0.0958] | [−0.0159, 1.5208] |
| A6  | 0.9485 | [0.0195, 0.1079] | [0.3096, 1.7129] |

A6's annualised interval `[0.31, 1.71]` contains essentially all of B2's
`[−0.02, 1.52]`. The point estimate separates the arms; the interval does not.

**The win is a return outcome from a return-blind selector.** A6 earns 0.0560
annual against A1's 0.0344 while running *more* risk (ann vol 0.0867 vs
0.0732). The selection objective sees only volatilities and correlations — no
means — so it cannot have targeted that return. Concentrating from 7 names into
4 raises dispersion; on this draw the dispersion paid.

## A structural blocker, independent of the evidence

Promotion as specified would also have shipped a wrong-number bug.
`OperationalPolicy.arm()` builds `Arm(arm_id, objective, solver)` and **drops
`params`**. A `cardinal_min_variance` policy registered that way would hand the
autopilot (`qlab/autopilot/loop.py`, `champion = policy.arm()`) an arm with no
`cardinality`, so the desk would run plain full-universe min-variance under the
cardinal name. Fixing that means adding `params` to `OperationalPolicy`.

Beyond that, `get_operational_policy` requires `agent_usable`, which requires a
non-`None` `agent_tool`. No agent tool can express `k` today: `backtest.run`
and `algorithms.solve` have no cardinality parameter, and `selection.run`
selects without allocating. Declaring either would claim an agent-reachable
path that does not exist — and widening what an agent may *execute* is exactly
what the design boundary forbids doing casually.

## Decision

**`cardinal_min_variance` stays at `research`.** It is not registered in
`_POLICIES`. Arm A6 stays in `ablation_v1.yaml` so the measurement is
reproducible and the next run can be compared against this one.

What would change the answer: a panel on which selection is a live choice at all
— a wider candidate universe than seven names, with a volatility profile that
actually varies across draws rather than being pinned by `md5(ticker)` — a
positive margin that survives that sweep, and an execution path that carries `k`
end to end.

## Postscript: exactly-k is now checked at delivery

Fix round 1 closed a real hole found in review. `select_k_of_n` certifies a
k-name basket, but long-only min-variance may then park one of those names on
its lower bound, so the *delivered* plan could hold fewer than `k` names above
the 1e-4 threshold that `Mandate.check_targets` and the trader both count at —
the "count is a lie" failure the module exists to prevent. The policy now counts
its own delivered names at 1e-4 and refuses, naming both counts.

Checked against the numbers above: over every rebalance of all four seeds, A6
delivered exactly 4 names every time, so the recorded results are unaffected. The
failure mode is not exotic, though — about 4% of well-conditioned random
covariances park a selected name, and one such matrix is pinned as a test.
