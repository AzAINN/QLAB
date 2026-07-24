# qlab Product Roadmap v2 — from governed skeleton to viable desk

**Status:** PROPOSAL — awaiting owner approval. Supersedes nothing; extends
the 2026-07-19 continuation ledger's "next work" list into a product plan.
**Date:** 2026-07-24

---

## 1. Product thesis

qlab's moat is not a strategy; it is **the only desk where AI agents operate
a real investment process and provably cannot cheat**: judgments logged and
challenged, verdicts hash-bound, execution human-gated, statistics deflated.
The viable product is that machine applied to a *realistic* universe with a
*visual* face:

- **Audience now:** the operator (you) + the challenge judges.
- **Audience next:** prosumers who want a governed AI allocation desk for
  their own (paper→real) money; small RIAs who need an auditable research
  process.
- **Pitch:** "AI you can let near your portfolio because the system makes it
  impossible for the AI to lie about its work."

## 2. The agent harness — what it is and how it grows

**Today (fact):** the coordinator genuinely deploys subagents via the Agent
tool — five isolated Claude sessions with least-privilege MCP tools (verified
live). It is *not* one model reasoning sequentially. But the workflow shape is
a fixed pipeline: analyst → {challenger ∥ optimizer} → referee → reporter.
The parallelism the registry DAG allows is minimal.

**Harness v2 — three new workflow kinds (all registry-enforced like the
current one):**

1. **Panel (tournament) runs** — N analyst variants dispatched in parallel,
   each defending a different estimation stance (window 252/504/756, linear
   vs nonlinear shrinkage, regime read). Each branch: analyst → optimizer →
   scored walk-forward evidence. A judge phase compares evidence (not vibes)
   and the referee gates the winner. Everything logs as arms; DSR trial
   counting already handles the multiplicity honestly. *This is "different
   agents trying different things, then optimizing and comparing."*
2. **QA runs** — two new read-only roles:
   - **data-qa**: point-in-time integrity, missing bars, split/dividend
     anomalies, stale caches → verdict on the snapshot.
   - **signal-qa**: look-ahead probes, stationarity checks, correlation of a
     proposed signal with future information → verdict on the signal.
   Both write verdicts, never data. They run before any panel that consumes
   the data/signal.
3. **Debate runs** — bounded multi-round challenger↔analyst exchange with an
   adjudication step, only on the genuinely underdetermined estimation call.

**Plumbing upgrades:** per-role model routing (deep model for
analyst/challenger/referee, fast model for runner/reporter — the loader's
`model:` field already supports it); reflection scoring extended with
realized-alpha-vs-champion; reflection retrieval by regime fingerprint
similarity instead of recency.

## 3. TUI/UX redesign — buttons, dashboard, density

**Stack decision:** stay Textual for the operator console; grow the existing
web client toward the FinceptTerminal-class visual ceiling later. Both speak
the same owner API, so nothing is wasted; a C++/Qt shell is a rewrite the
product does not need before the loop itself is richer. Textual has full
mouse support — buttons, tabs, clickable rows are native.

**Design tokens first.** One `qlab/tui/theme.py` tokens module (FinceptTerminal
`ThemeTokens` pattern): backgrounds darkest→lightest, 3 border weights,
4-level text hierarchy, one amber accent, semantic pos/neg/warn/info, chart
palette. All CSS generated from tokens; no scattered hex.

**Screen map (function keys + clickable tab bar):**

| Key | Screen | Content |
|---|---|---|
| F1 | **Dashboard** (renamed from Desk) | tiled widgets: equity + drawdown tile, allocation bar tile, regime tile, market pulse strip, latest verdict card, active run card, alerts tile. Each tile: title bar, refresh, config. |
| F2 | **Markets** | symbol-linked: braille price chart, quote detail, watchlist; `/`-scoped search (`/etf`, `/stock`) to add symbols |
| F3 | **Research** | runs table, ablation results as ranked cards, window-evidence panel, (later) prediction lane |
| F4 | **Workforce** | chat + phase board + **buttons**: [New review] [Panel run] [Resume] [Stop] |
| F5 | **Book** | positions, plans as cards with [Preview] [Execute] buttons (execute → confirm modal), orders |
| F6 | **Audit** | decisions/verdicts/reflections with detail expansion |
| F7 | **Settings** | mandate view, data providers, agent roster, theme |

**Interaction rules:**
- Every action gets a **button in context**; the `:` command line stays as
  the power-user accelerator. Buttons and commands share one handler layer.
- **No startup modal** — the workforce is started from its own screen's
  button; `--claude offer` default becomes `off`-equivalent (status chip
  shows readiness instead of interrupting).
- **Bulletin rendering, not paragraphs:** verdict cards, key-number tiles,
  phase chips with elapsed times, one-line bullet summaries (the
  `clean_report_line` normalization already begun). Agent memos render as
  UPPERCASE section labels + bullets, never prose walls.
- **Symbol-group linking:** Markets chart, quote panel, and research panel
  follow one active symbol (FinceptTerminal A–J groups, start with one).

## 4. Data layer — live, multi-provider

- **Provider abstraction** in `qlab/core/data.py`: `yfinance` (default,
  free) | `alpaca` (paper keys unlock IEX real-time quotes + minute bars +
  websocket stream). Provenance tags everywhere, as now.
- **Owner topic bus:** extend the SSE bus into a DataHub-style topic scheme
  (`quote:SPY`, `bars:daily:*`) with per-topic TTL/min-interval policies;
  one fetch fans out to every subscribed panel. The TUI dashboard tiles and
  the web client both subscribe.
- Honesty note: the *decision loop* stays daily/quarterly; live quotes serve
  display and execution realism (marketable-limit pricing), not intraday
  signals.

## 5. Universe realism — ETFs up, stock basket in

1. **ETF core 7 → ~25-40**: activate the 19-candidate pool, extend
   `universe.yaml` with sector/factor/regional ETFs. HRP/ERC scale fine;
   MVSK co-moments stay research-only at this size.
2. **Selection layer (revived):** the relevance/redundancy k-of-N selection
   is solved *exactly* classically at these sizes; it becomes the gate from
   candidate pool → allocation set. (The QAOA formulation stays offline; the
   objective is identical, so the offline lane finally has a staged classical
   twin to compare against.)
3. **Stock basket (research sleeve first):** search-and-add stocks into a
   candidate pool; **factor covariance** for estimation (use the ETF universe
   itself as observed factors — regress stock returns on asset-class/sector
   ETFs; covariance = B·Σ_ETF·Bᵀ + diagonal idiosyncratic). Selection → HRP
   or min-var over selected names; per-name caps in a sleeve mandate.
   Promotion to operational requires the catalog gate + evidence, as always.
4. Dividends/corporate actions correctness in the paper book (total-return
   accounting) before any stock sleeve goes live-paper.

## 6. Quantitative depth

- **PMPT objective family:** downside deviation / target semivariance
  objective form, omega ratio, upside/downside capture, drawdown-at-risk in
  metrics. Cataloged `research` until evidence.
- **Window-evidence tool ("what's the right window?"):** deterministic sweep
  that walk-forward-scores each candidate window/shrinkage combo and hands
  the analyst an evidence table — turning the judgment slot from vibes into
  a defended, data-backed choice. (This is the single best upgrade to the
  analyst role.)
- **Expected portfolio return (honest version):** equilibrium
  reverse-optimization (market-cap-implied returns, the Black-Litterman
  prior) + shrunk historical means + yield building blocks — always shown as
  a **range with uncertainty bands**, never a point forecast; feeds a
  max-utility research arm and the dashboard tile.
- **Regime v2:** 2-3 state Gaussian HMM (+ statistical jump-model variant)
  over returns/vol/turbulence; deterministic threshold stays as fallback and
  cross-check; regime posterior displayed as a strip on the dashboard.
- **Stress scenarios tile:** replay 2008/2020/2022 windows against *current*
  weights — cheap, visual, and the kind of thing a real desk looks at daily.
- **Autopilot mode:** market-calendar scheduler + drift bands + regime
  triggers to pre-validated defensive targets; all actions land as proposals
  unless within pre-approved bands.

## 7. Qualitative lane (news) and prediction lane

- **News → bounded risk views** (unchanged design, now sequenced): quarantined
  extractor → typed, clamped views on vol/correlation/tails → entropy pooling
  with means pinned → conditioned tensors into the unchanged solver stack.
  KL budget + corroboration against hard signals + calibration ledger.
  Qualitative analysis enters as *risk views*, never trade calls.
- **Prediction lane (after portfolio optimization matures):** a `prediction`
  catalog category, research-staged: ridge/lasso on lagged features →
  gradient boosting → SOTA sequence models; targets are *risk/regime*
  quantities first (realized vol, regime posterior), return prediction last
  and always DSR-accounted with purged walk-forward CV. Display: prediction
  vs realized chart + calibration, on the Research screen.

## 8. Product features that make it feel real

- **Monthly memo / report builder:** investor-letter-style markdown/PDF
  generated from the registry (performance, decisions, verdicts,
  reflections). The demo artifact.
- **Alerts:** drift, kill-switch proximity, regime flip, failed workflow →
  dashboard alerts tile + desktop notification.
- **Policy registry:** policies as first-class versioned objects with
  promotion history (research → operational), completing the governance
  story.
- **Benchmark-relative view:** tracking error and capture vs 60/40 on the
  dashboard.

## 9. Phasing (cleanup first, per owner notes)

| Phase | Contents | Exit test |
|---|---|---|
| **P0 cleanup** | theme tokens; Dashboard rename + tiles; buttons everywhere + no startup modal; handler layer shared by buttons/commands; code architecture tidy | every current action clickable; suite green |
| **P1 data+universe** | provider abstraction + Alpaca; topic bus; ETF expansion; selection layer; stock candidate pool + factor covariance (research) | live quotes on dashboard; selection run persisted |
| **P2 quant depth** | window-evidence tool; PMPT forms; expected-return bands (BL); HMM regime + robustness layer (Uncertain state, confirmation delay, ensemble agreement); transaction-cost model + net-alpha referee gate; drawdown circuit-breaker tiers + correlation-spike stress; stress tile; scheduler+triggers | analyst cites evidence table in a run; a plan with net alpha ≤ 0 after costs is refused; autopilot dry cycle fires on schedule |
| **P3 harness v2** | panel runs; QA roles; debate+adjudication; model routing; alpha-scored reflections + similarity recall | one panel run with 3 analyst variants produces a judged winner |
| **P4 lanes** | news→views engine; prediction lane v1 (regression baseline) | a view-conditioned run and a calibrated vol forecast on screen |
| **P5 product** | report builder; alerts; policy registry; web-client growth; packaging + submission assets | monthly memo generated from registry |

Each phase lands as plan → tests → implementation → review, per repo
conventions. Nothing skips the catalog stages or the referee.

## 10. Quant-book alignment (Wayland Zhang, "AI Quantitative Trading")

Reviewed 2026-07-24 (27 chapters/articles fetched). Verdict: **qlab's
estimation and portfolio-construction core already exceeds the book** — HRP,
CVaR optimization, nonlinear shrinkage, co-moment tensors, and
turbulence/absorption regime signals are absent from it or explicitly listed
as omissions. The book's governance model (independent risk veto,
LLM-as-signal-only, human-owns-judgment, "verify correctness without asking
the executor about its intent") is essentially qlab's existing design —
external corroboration that the boundary invariants match production
practice.

The book argues hard for four bands qlab is thin on; each is now placed in
the phasing above:

1. **Transaction-cost realism + net-alpha gate** (its most-repeated theme):
   square-root impact `k·σ·√(Q/ADV)` + commission/spread per name, a
   turnover/cost penalty in the objective, and a referee check that rejects
   any plan whose expected net alpha ≤ 0 after a 1.5× cost safety margin and
   a 0.5× backtest→live haircut. → **P2** (deterministic, no boundary
   tension; replaces today's flat 5 bps).
2. **Expected-return honesty via Black-Litterman equilibrium** — reverse-
   optimized market-implied μ blended with bounded views + uncertainty Ω;
   the natural substrate for the news→views engine. → **P2** (already in
   §6; the book confirms the design).
3. **Prediction-lane hygiene**: purged + embargoed CV (embargo ≈ half the
   label window), vol-scaled triple-barrier labeling, and **meta-labeling**
   — primary model predicts side, secondary model predicts confidence,
   deterministic code maps confidence to size. The side/size split is the
   cleanest possible fit to "the model never sizes trades." Plus
   drift detection (PSI + IC decay) and champion/challenger lifecycle
   gates. → **P4**, all research-staged, IC>0.03 and PBO as admission
   criteria.
4. **Regime-misclassification robustness** (the book devotes two lessons):
   a fourth "Uncertain" state (HMM max-prob <50% or detectors disagree →
   half risk), confirmation delay of 3–5 days, gradual 50→70→90% regime
   transitions, and ≥70% detector-ensemble agreement. → **P2**, wrapped as
   deterministic code around the existing detectors + planned HMM.

Cheap adds folded in: drawdown circuit-breaker tiers (5/10/15%), hard
leverage cap, correlation→1 stress check in the referee (→ P2); trial-count
Sharpe deflation `E[maxSR]=SE·√(2lnK)`, PBO, and Monte-Carlo trade-shuffle
in the metrics/referee (→ P2); IC/IC-stability gating for any signal
admission (→ P4). Skipped deliberately: TWAP/VWAP microstructure, Kyle's λ,
order-book simulation — irrelevant at daily/quarterly cadence.
