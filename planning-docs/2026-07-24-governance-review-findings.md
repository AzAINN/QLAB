# Governance & execution deep review — findings (2026-07-24 Q1 sweep)

Source: Codex max-reasoning review, reproduced with in-memory registries.
Triage: [FIX] cheap+correct now · [CORE] serious trader-core, dedicated batch · [DESIGN] intended/acceptable.

1. **Critical — the headless MCP is an agent-reachable, unconfirmed, non-cost-gated execution path.**  
   Files: [.mcp.json:5](/Users/azainmac/codebases/quant-trading-agent/.mcp.json:5), [qlab/mcp/server.py:34](/Users/azainmac/codebases/quant-trading-agent/qlab/mcp/server.py:34), [qlab/mcp/quant_trader.py:73](/Users/azainmac/codebases/quant-trading-agent/qlab/mcp/quant_trader.py:73), [qlab/mcp/quant_trader.py:84](/Users/azainmac/codebases/quant-trading-agent/qlab/mcp/quant_trader.py:84).  
   **Invariant:** no agent-reachable execution; human confirmation and the cost/reconcile gates are mandatory.  
   **Concrete sequence:** with no owner running, a headless agent receives the combined server, obtains a PASS, calls `propose_rebalance`, then `execute_plan(plan_id)`. Neither tool accepts `human_confirmed`; proposal runs neither `reconcile` nor `cost_gate`. I reproduced a plan for which `cost_gate` returned refusal reasons but the MCP execution returned `executed=True` and `reconciled`.

2. **Critical — owner autopilot and CLI paths execute without human confirmation.**  
   Files: [qlab/autopilot/loop.py:77](/Users/azainmac/codebases/quant-trading-agent/qlab/autopilot/loop.py:77), [qlab/autopilot/loop.py:221](/Users/azainmac/codebases/quant-trading-agent/qlab/autopilot/loop.py:221), [qlab/ui/server.py:917](/Users/azainmac/codebases/quant-trading-agent/qlab/ui/server.py:917), [qlab/autopilot/cli.py:49](/Users/azainmac/codebases/quant-trading-agent/qlab/autopilot/cli.py:49).  
   **Invariant:** every execution requires `human_confirmed=True` from the TUI.  
   **Concrete sequence:** `POST /api/run_once` with only `{"offline":true}` defaults `execute=True` and books fills. Likewise, `qlab run-once --offline` executes by default, and `qlab watch` repeats execution indefinitely without per-plan confirmation.

3. **Critical — checked plans are not revalidated against the current book or drawdown at execution.**  
   Files: [qlab/ui/server.py:284](/Users/azainmac/codebases/quant-trading-agent/qlab/ui/server.py:284), [qlab/ui/server.py:307](/Users/azainmac/codebases/quant-trading-agent/qlab/ui/server.py:307), [qlab/trader/plan.py:203](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/plan.py:203), [qlab/trader/plan.py:211](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/plan.py:211).  
   **Invariant:** mandate, reconciliation, cost and trailing-drawdown gates must hold when orders are submitted.  
   **Concrete sequence:** create two checked, PASS-bound plans from the same all-cash snapshot under different decisions; confirm plan 1, then plan 2. Both execute their original full-deployment legs. I reproduced final cash `-10000` and gross exposure `2.0`. Separately, increasing the persisted high-water mark after preview to create a 50% drawdown still allowed the checked plan to execute because execution never refreshes portfolio state.

4. **Critical — execution does not verify that the supplied plan content equals the persisted checked plan.**  
   File: [qlab/trader/plan.py:203](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/plan.py:203).  
   **Invariant:** only the exact persisted checked plan and its reviewed targets may execute.  
   **Concrete sequence:** persist legitimate plan `P` for decision A; construct an `OrderPlan` object with `plan_id=P` but decision B, `{"TSLA":1.0}`, and a TSLA buy leg; log a PASS for B. `execute_plan` checks only P’s stored state, then validates B/the forged targets and executes the forged leg. I reproduced an off-whitelist TSLA position while P’s stored decision remained A.

5. **Critical — current positions omitted from the target map are never sold.**  
   File: [qlab/trader/plan.py:122](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/plan.py:122).  
   **Invariant:** target weights, gross-exposure and turnover limits must describe the resulting portfolio.  
   **Concrete sequence:** start with five assets at 20% each; target only four of them at 25% each. The loop iterates target tickers only, producing four 5% buys and no sale of the omitted asset. I reproduced turnover reported as `0.20`, cash `-2000`, retained omitted weight `0.20`, and final gross exposure `1.20`.

6. **High — the control drawdown tier is bypassed by interactive previews.**  
   Files: [qlab/governance/referee.py:143](/Users/azainmac/codebases/quant-trading-agent/qlab/governance/referee.py:143), [qlab/ui/server.py:231](/Users/azainmac/codebases/quant-trading-agent/qlab/ui/server.py:231), [qlab/ui/server.py:253](/Users/azainmac/codebases/quant-trading-agent/qlab/ui/server.py:253).  
   **Invariant:** control-tier drawdown forbids increasing gross exposure.  
   **Concrete sequence:** hold 0.80 gross exposure at exactly 10% drawdown; submit fully invested 1.00-gross targets and an agent PASS. `deterministic_referee` returns FAIL, but `rebalance_preview` only checks the persisted latest PASS and calls `build_plan`, which enforces only the 15% trailing breaker. I reproduced an accepted checked preview for this input.

7. **High — Alpaca drawdown is always reported as zero.**  
   File: [qlab/trader/broker.py:140](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/broker.py:140).  
   **Invariant:** drawdown tiers and the trailing kill-switch must use a persistent high-water mark.  
   **Concrete sequence:** an Alpaca account peaks at 100,000 and later reports equity 80,000. The adapter returns `high_water_mark=80,000`, yielding drawdown zero rather than 20%; daily ops, referee and plan construction therefore see no drawdown breach.

8. **High — breaker liquidation is impossible despite being explicitly permitted.**  
   Files: [qlab/governance/referee.py:99](/Users/azainmac/codebases/quant-trading-agent/qlab/governance/referee.py:99), [qlab/trader/plan.py:127](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/plan.py:127), [qlab/trader/plan.py:211](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/plan.py:211), [qlab/ui/server.py:223](/Users/azainmac/codebases/quant-trading-agent/qlab/ui/server.py:223).  
   **Invariant:** the kill-switch refuses non-liquidating orders while preserving liquidation.  
   **Concrete sequence:** at 15% drawdown, zero-weight targets receive a referee PASS in liquidation mode. `build_plan` nevertheless immediately halts and raises before considering the zero gross target; any pre-existing liquidation plan is then rejected because `execute_plan` blocks every plan while halted. An empty target representation is additionally rejected by the preview endpoint.

9. **High — `targets_hash` is not exact.**  
   File: [qlab/state/registry.py:91](/Users/azainmac/codebases/quant-trading-agent/qlab/state/registry.py:91).  
   **Invariant:** PASS, optimizer, judge and plan bindings cover the exact target map.  
   **Concrete sequence:** `{"ACWI":0.5000001,"BNDW":0.4999999}` and `{"ACWI":0.5000004,"BNDW":0.4999996}` are unequal but both hash to `3b94f7f32f5640d4` because values are rounded to six decimals. Either vector can therefore satisfy a PASS or workflow binding created for the other.

10. **High — workflow referee verdicts are not bound to the workflow’s decision.**  
    File: [qlab/state/registry.py:771](/Users/azainmac/codebases/quant-trading-agent/qlab/state/registry.py:771).  
    **Invariant:** referee approval must bind the selected analyst/judge decision as well as its targets.  
    **Concrete sequence:** log PASS verdict V for old decision A and targets T; start a new workflow whose analyst records decision B and whose optimizer produces the same T; complete the referee phase using V. I reproduced the phase reaching `done` even though V’s persisted `decision_id` remained A. Panel workflows similarly fail to require the winning optimizer branch’s matching analyst decision.

11. **High — workflow phases are not actually role-bound at the owner boundary.**  
    Files: [qlab/ui/server.py:204](/Users/azainmac/codebases/quant-trading-agent/qlab/ui/server.py:204), [qlab/state/registry.py:659](/Users/azainmac/codebases/quant-trading-agent/qlab/state/registry.py:659), [qlab/tui/claude.py:334](/Users/azainmac/codebases/quant-trading-agent/qlab/tui/claude.py:334).  
    **Invariant:** each governed phase is writable only by its assigned role.  
    **Concrete sequence:** after panel analysts complete, the referee agent—granted generic `workflow_phase` for judge work—can mark every `optimizer-N` phase done with invented targets, then mark judge and referee. Neither HTTP nor registry update receives or checks caller identity; the stored `agent` field is metadata only.

12. **High — reconciliation can claim clean/reconciled when broker and ledger disagree.**  
    Files: [qlab/trader/reconcile.py:12](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/reconcile.py:12), [qlab/trader/plan.py:250](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/plan.py:250), [qlab/trader/plan.py:261](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/plan.py:261).  
    **Invariant:** trading requires a clean broker ledger, and `reconciled` is a verified terminal state.  
    **Concrete sequences:** registry cash 10,000 versus broker cash 0 with identical empty positions returns `clean=True`, because only quantities are compared. Separately, a broker returning order state `accepted` or `rejected` is recorded with that state, but the plan is unconditionally advanced through `filled` to `reconciled`; I reproduced a reconciled plan whose only order states were `accepted`.

13. **High — the mandated order type is ignored.**  
    Files: [mandate.yaml:35](/Users/azainmac/codebases/quant-trading-agent/mandate.yaml:35), [qlab/trader/plan.py:166](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/plan.py:166), [qlab/trader/broker.py:152](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/broker.py:152).  
    **Invariant:** deterministic mandate settings control execution.  
    **Concrete sequence:** the default mandate requires `marketable_limit` and says never plain market orders. The plan merely displays that value; Alpaca always constructs `MarketOrderRequest` with no limit price. Setting `allow_fractional=False` is likewise ineffective because execution remains notional/fractional.

14. **Medium — hedged books are misclassified as all-cash initial deployments.**  
    File: [qlab/trader/plan.py:134](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/plan.py:134).  
    **Invariant:** only genuinely all-cash books may bypass the turnover cap.  
    **Concrete sequence:** current weights `ACWI=+0.50`, `BNDW=-0.50` have net sum zero but gross one. Rebalance to five long 20% weights. The code marks it initial deployment and skips the 0.50 cap even though computed turnover is 1.60.

15. **Medium — `max_orders_per_day` is enforced per plan, not per day.**  
    Files: [qlab/trader/mandate.py:201](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/mandate.py:201), [qlab/trader/plan.py:175](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/plan.py:175).  
    **Invariant:** the configured daily order limit is cumulative.  
    **Concrete sequence:** configure a daily cap of two; execute two separate two-leg plans on the same date. Each plan passes `check_order_count(2)`, leaving four orders that day. I reproduced exactly this result.

16. **Medium — mandate validation has fail-open holes.**  
    Files: [qlab/trader/mandate.py:164](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/mandate.py:164), [qlab/trader/mandate.py:173](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/mandate.py:173), [qlab/trader/mandate.py:195](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/mandate.py:195), [qlab/trader/mandate.py:206](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/mandate.py:206).  
    **Invariant:** malformed limits fail closed and every configured hard limit is enforced.  
    **Concrete sequences:** with `min_weight_per_asset=0.05`, a legal-sum target containing a 0.01 weight passes both `check_targets` and `deterministic_referee`. With YAML `.nan` for trailing drawdown and turnover, a 50% drawdown returns not breached and turnover `999` passes because comparisons against NaN are false.

17. **Medium — requested Alpaca execution silently becomes simulation.**  
    File: [qlab/trader/broker.py:166](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/broker.py:166).  
    **Invariant:** execution venue and credential failures must fail loudly.  
    **Concrete sequence:** set both Alpaca credential variables, then make Alpaca initialization fail through a missing package, invalid setup, or constructor error. The broad `except Exception` silently returns `SimulatedPaperBroker`; I reproduced this fallback.

18. **Low — the cost gate accepts negative expected cost.**  
    File: [qlab/governance/referee.py:49](/Users/azainmac/codebases/quant-trading-agent/qlab/governance/referee.py:49).  
    **Invariant:** malformed cost decompositions must refuse rather than create an exemption.  
    **Concrete sequence:** `n_legs=1`, one 1,000-notional leg, and `expected_cost.total=-100` returns no refusal reasons: the negative total is finite, below the cap, and produces a negative hurdle.

COVERAGE: reviewed all requested modules plus CLI, MCP exposure, role allowlists and owner routes; ran the focused trader/registry/autopilot/UI suite successfully (96 tests) and reproduced the adversarial sequences above with isolated in-memory registries.


[2mtokens used[0m
275,318
Reviewed current checkout `7049a77`; the unrelated modified planning document was untouched. Prior context only located relevant paths—every finding below was reverified against current code.

1. **Critical — the headless MCP is an agent-reachable, unconfirmed, non-cost-gated execution path.**  
   Files: [.mcp.json:5](/Users/azainmac/codebases/quant-trading-agent/.mcp.json:5), [qlab/mcp/server.py:34](/Users/azainmac/codebases/quant-trading-agent/qlab/mcp/server.py:34), [qlab/mcp/quant_trader.py:73](/Users/azainmac/codebases/quant-trading-agent/qlab/mcp/quant_trader.py:73), [qlab/mcp/quant_trader.py:84](/Users/azainmac/codebases/quant-trading-agent/qlab/mcp/quant_trader.py:84).  
   **Invariant:** no agent-reachable execution; human confirmation and the cost/reconcile gates are mandatory.  
   **Concrete sequence:** with no owner running, a headless agent receives the combined server, obtains a PASS, calls `propose_rebalance`, then `execute_plan(plan_id)`. Neither tool accepts `human_confirmed`; proposal runs neither `reconcile` nor `cost_gate`. I reproduced a plan for which `cost_gate` returned refusal reasons but the MCP execution returned `executed=True` and `reconciled`.

2. **Critical — owner autopilot and CLI paths execute without human confirmation.**  
   Files: [qlab/autopilot/loop.py:77](/Users/azainmac/codebases/quant-trading-agent/qlab/autopilot/loop.py:77), [qlab/autopilot/loop.py:221](/Users/azainmac/codebases/quant-trading-agent/qlab/autopilot/loop.py:221), [qlab/ui/server.py:917](/Users/azainmac/codebases/quant-trading-agent/qlab/ui/server.py:917), [qlab/autopilot/cli.py:49](/Users/azainmac/codebases/quant-trading-agent/qlab/autopilot/cli.py:49).  
   **Invariant:** every execution requires `human_confirmed=True` from the TUI.  
   **Concrete sequence:** `POST /api/run_once` with only `{"offline":true}` defaults `execute=True` and books fills. Likewise, `qlab run-once --offline` executes by default, and `qlab watch` repeats execution indefinitely without per-plan confirmation.

3. **Critical — checked plans are not revalidated against the current book or drawdown at execution.**  
   Files: [qlab/ui/server.py:284](/Users/azainmac/codebases/quant-trading-agent/qlab/ui/server.py:284), [qlab/ui/server.py:307](/Users/azainmac/codebases/quant-trading-agent/qlab/ui/server.py:307), [qlab/trader/plan.py:203](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/plan.py:203), [qlab/trader/plan.py:211](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/plan.py:211).  
   **Invariant:** mandate, reconciliation, cost and trailing-drawdown gates must hold when orders are submitted.  
   **Concrete sequence:** create two checked, PASS-bound plans from the same all-cash snapshot under different decisions; confirm plan 1, then plan 2. Both execute their original full-deployment legs. I reproduced final cash `-10000` and gross exposure `2.0`. Separately, increasing the persisted high-water mark after preview to create a 50% drawdown still allowed the checked plan to execute because execution never refreshes portfolio state.

4. **Critical — execution does not verify that the supplied plan content equals the persisted checked plan.**  
   File: [qlab/trader/plan.py:203](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/plan.py:203).  
   **Invariant:** only the exact persisted checked plan and its reviewed targets may execute.  
   **Concrete sequence:** persist legitimate plan `P` for decision A; construct an `OrderPlan` object with `plan_id=P` but decision B, `{"TSLA":1.0}`, and a TSLA buy leg; log a PASS for B. `execute_plan` checks only P’s stored state, then validates B/the forged targets and executes the forged leg. I reproduced an off-whitelist TSLA position while P’s stored decision remained A.

5. **Critical — current positions omitted from the target map are never sold.**  
   File: [qlab/trader/plan.py:122](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/plan.py:122).  
   **Invariant:** target weights, gross-exposure and turnover limits must describe the resulting portfolio.  
   **Concrete sequence:** start with five assets at 20% each; target only four of them at 25% each. The loop iterates target tickers only, producing four 5% buys and no sale of the omitted asset. I reproduced turnover reported as `0.20`, cash `-2000`, retained omitted weight `0.20`, and final gross exposure `1.20`.

6. **High — the control drawdown tier is bypassed by interactive previews.**  
   Files: [qlab/governance/referee.py:143](/Users/azainmac/codebases/quant-trading-agent/qlab/governance/referee.py:143), [qlab/ui/server.py:231](/Users/azainmac/codebases/quant-trading-agent/qlab/ui/server.py:231), [qlab/ui/server.py:253](/Users/azainmac/codebases/quant-trading-agent/qlab/ui/server.py:253).  
   **Invariant:** control-tier drawdown forbids increasing gross exposure.  
   **Concrete sequence:** hold 0.80 gross exposure at exactly 10% drawdown; submit fully invested 1.00-gross targets and an agent PASS. `deterministic_referee` returns FAIL, but `rebalance_preview` only checks the persisted latest PASS and calls `build_plan`, which enforces only the 15% trailing breaker. I reproduced an accepted checked preview for this input.

7. **High — Alpaca drawdown is always reported as zero.**  
   File: [qlab/trader/broker.py:140](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/broker.py:140).  
   **Invariant:** drawdown tiers and the trailing kill-switch must use a persistent high-water mark.  
   **Concrete sequence:** an Alpaca account peaks at 100,000 and later reports equity 80,000. The adapter returns `high_water_mark=80,000`, yielding drawdown zero rather than 20%; daily ops, referee and plan construction therefore see no drawdown breach.

8. **High — breaker liquidation is impossible despite being explicitly permitted.**  
   Files: [qlab/governance/referee.py:99](/Users/azainmac/codebases/quant-trading-agent/qlab/governance/referee.py:99), [qlab/trader/plan.py:127](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/plan.py:127), [qlab/trader/plan.py:211](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/plan.py:211), [qlab/ui/server.py:223](/Users/azainmac/codebases/quant-trading-agent/qlab/ui/server.py:223).  
   **Invariant:** the kill-switch refuses non-liquidating orders while preserving liquidation.  
   **Concrete sequence:** at 15% drawdown, zero-weight targets receive a referee PASS in liquidation mode. `build_plan` nevertheless immediately halts and raises before considering the zero gross target; any pre-existing liquidation plan is then rejected because `execute_plan` blocks every plan while halted. An empty target representation is additionally rejected by the preview endpoint.

9. **High — `targets_hash` is not exact.**  
   File: [qlab/state/registry.py:91](/Users/azainmac/codebases/quant-trading-agent/qlab/state/registry.py:91).  
   **Invariant:** PASS, optimizer, judge and plan bindings cover the exact target map.  
   **Concrete sequence:** `{"ACWI":0.5000001,"BNDW":0.4999999}` and `{"ACWI":0.5000004,"BNDW":0.4999996}` are unequal but both hash to `3b94f7f32f5640d4` because values are rounded to six decimals. Either vector can therefore satisfy a PASS or workflow binding created for the other.

10. **High — workflow referee verdicts are not bound to the workflow’s decision.**  
    File: [qlab/state/registry.py:771](/Users/azainmac/codebases/quant-trading-agent/qlab/state/registry.py:771).  
    **Invariant:** referee approval must bind the selected analyst/judge decision as well as its targets.  
    **Concrete sequence:** log PASS verdict V for old decision A and targets T; start a new workflow whose analyst records decision B and whose optimizer produces the same T; complete the referee phase using V. I reproduced the phase reaching `done` even though V’s persisted `decision_id` remained A. Panel workflows similarly fail to require the winning optimizer branch’s matching analyst decision.

11. **High — workflow phases are not actually role-bound at the owner boundary.**  
    Files: [qlab/ui/server.py:204](/Users/azainmac/codebases/quant-trading-agent/qlab/ui/server.py:204), [qlab/state/registry.py:659](/Users/azainmac/codebases/quant-trading-agent/qlab/state/registry.py:659), [qlab/tui/claude.py:334](/Users/azainmac/codebases/quant-trading-agent/qlab/tui/claude.py:334).  
    **Invariant:** each governed phase is writable only by its assigned role.  
    **Concrete sequence:** after panel analysts complete, the referee agent—granted generic `workflow_phase` for judge work—can mark every `optimizer-N` phase done with invented targets, then mark judge and referee. Neither HTTP nor registry update receives or checks caller identity; the stored `agent` field is metadata only.

12. **High — reconciliation can claim clean/reconciled when broker and ledger disagree.**  
    Files: [qlab/trader/reconcile.py:12](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/reconcile.py:12), [qlab/trader/plan.py:250](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/plan.py:250), [qlab/trader/plan.py:261](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/plan.py:261).  
    **Invariant:** trading requires a clean broker ledger, and `reconciled` is a verified terminal state.  
    **Concrete sequences:** registry cash 10,000 versus broker cash 0 with identical empty positions returns `clean=True`, because only quantities are compared. Separately, a broker returning order state `accepted` or `rejected` is recorded with that state, but the plan is unconditionally advanced through `filled` to `reconciled`; I reproduced a reconciled plan whose only order states were `accepted`.

13. **High — the mandated order type is ignored.**  
    Files: [mandate.yaml:35](/Users/azainmac/codebases/quant-trading-agent/mandate.yaml:35), [qlab/trader/plan.py:166](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/plan.py:166), [qlab/trader/broker.py:152](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/broker.py:152).  
    **Invariant:** deterministic mandate settings control execution.  
    **Concrete sequence:** the default mandate requires `marketable_limit` and says never plain market orders. The plan merely displays that value; Alpaca always constructs `MarketOrderRequest` with no limit price. Setting `allow_fractional=False` is likewise ineffective because execution remains notional/fractional.

14. **Medium — hedged books are misclassified as all-cash initial deployments.**  
    File: [qlab/trader/plan.py:134](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/plan.py:134).  
    **Invariant:** only genuinely all-cash books may bypass the turnover cap.  
    **Concrete sequence:** current weights `ACWI=+0.50`, `BNDW=-0.50` have net sum zero but gross one. Rebalance to five long 20% weights. The code marks it initial deployment and skips the 0.50 cap even though computed turnover is 1.60.

15. **Medium — `max_orders_per_day` is enforced per plan, not per day.**  
    Files: [qlab/trader/mandate.py:201](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/mandate.py:201), [qlab/trader/plan.py:175](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/plan.py:175).  
    **Invariant:** the configured daily order limit is cumulative.  
    **Concrete sequence:** configure a daily cap of two; execute two separate two-leg plans on the same date. Each plan passes `check_order_count(2)`, leaving four orders that day. I reproduced exactly this result.

16. **Medium — mandate validation has fail-open holes.**  
    Files: [qlab/trader/mandate.py:164](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/mandate.py:164), [qlab/trader/mandate.py:173](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/mandate.py:173), [qlab/trader/mandate.py:195](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/mandate.py:195), [qlab/trader/mandate.py:206](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/mandate.py:206).  
    **Invariant:** malformed limits fail closed and every configured hard limit is enforced.  
    **Concrete sequences:** with `min_weight_per_asset=0.05`, a legal-sum target containing a 0.01 weight passes both `check_targets` and `deterministic_referee`. With YAML `.nan` for trailing drawdown and turnover, a 50% drawdown returns not breached and turnover `999` passes because comparisons against NaN are false.

17. **Medium — requested Alpaca execution silently becomes simulation.**  
    File: [qlab/trader/broker.py:166](/Users/azainmac/codebases/quant-trading-agent/qlab/trader/broker.py:166).  
    **Invariant:** execution venue and credential failures must fail loudly.  
    **Concrete sequence:** set both Alpaca credential variables, then make Alpaca initialization fail through a missing package, invalid setup, or constructor error. The broad `except Exception` silently returns `SimulatedPaperBroker`; I reproduced this fallback.

18. **Low — the cost gate accepts negative expected cost.**  
    File: [qlab/governance/referee.py:49](/Users/azainmac/codebases/quant-trading-agent/qlab/governance/referee.py:49).  
    **Invariant:** malformed cost decompositions must refuse rather than create an exemption.  
    **Concrete sequence:** `n_legs=1`, one 1,000-notional leg, and `expected_cost.total=-100` returns no refusal reasons: the negative total is finite, below the cap, and produces a negative hurdle.

COVERAGE: reviewed all requested modules plus CLI, MCP exposure, role allowlists and owner routes; ran the focused trader/registry/autopilot/UI suite successfully (96 tests) and reproduced the adversarial sequences above with isolated in-memory registries.



---
## Resolution status (2026-07-24)

FIXED: #1 (headless human-gate + cost gate, 58a6282), #3/#4/#5/#8/#14 +
#6/#7 (trader-core batch, 9a0ab7f), #9/#15 (aded939), #10 (this commit),
#12/#16/#17/#18 (88d9251).

DOCUMENTED, not code-fixed:
- #2 autopilot executes without human confirmation — BY DESIGN. The mandated
  autopilot (qlab run-once / daily-ops) is the "policy operated under a hard
  mandate" thesis; its protection is the referee + cost gate + reconcile +
  drawdown tiers + kill switch, not human confirmation. Human confirmation
  gates the INTERACTIVE surfaces (TUI, headless MCP), which is where an agent
  could otherwise reach execution. Not a bug.
- #11 workflow phases not role-bound at the owner HTTP boundary — accepted
  design limitation, documented in qlab/tui/claude.py: branch-phase authority
  is coordinator-prompt-level while the ARTIFACT contracts (targets binding,
  decision binding, PASS binding) are registry-enforced. Owner-side caller
  identity is a larger change tracked for a future auth pass.
- #13 Alpaca submits MarketOrderRequest, ignoring order_type=marketable_limit
  — real, but only exercisable with live Alpaca credentials and untestable in
  the offline suite; deferred to the live-Alpaca integration work rather than
  shipped as an unverified execution change.
