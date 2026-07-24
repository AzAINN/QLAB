# News-as-signal — implementation plan (P4.5–P4.7)

**Status:** ACTIVE. Direct build (not the self-paced loop). Extends the
qualitative lane from "operator pastes text" to "a feed + a Claude agent's
qualitative judgement, bounded to risk views."

## The invariant this must not break

News never produces a return forecast, a direction, or a trade call. The
Claude agent's judgement is confined to **which risk-shape views (vol /
correlation / tail) the news supports**, each schema-typed, mean-pinned,
confidence-capped (≤0.7), provenance-checked, KL-budgeted, and research-only.
Untrusted text in → bounded risk views + a human summary out. Prompt injection
in a headline can at worst move the risk model within the KL budget — which is
exactly the blast radius the architecture caps.

## P4.5 — News feed + agent-read wiring

**Feed core (isolated module):** `qlab/news/feed.py` mirrors `qlab/core/data.py`'s
provider seam. `NewsItem{source, published, headline, summary, url, tickers}`
with provenance. Providers: `synthetic` (deterministic offline fixtures, the
default and the test path) and `rss` (online; stdlib `urllib`+`xml.etree`
parse of RSS2/Atom, optional `feedparser` accel, loud refusal offline).
`configs/news_sources.yaml` maps a small curated feed set to the cross-asset
universe (Fed/ECB/Treasury→bonds, broad-market→ACWI, commodities→GSG/GLD).

**Owner tool:** `news.fetch(as_of, universe, lookback_hours)` → recent
provenance-tagged items, cache-backed, offline-synthetic. In `OWNER_LAB_TOOLS`
and the proxy.

**Agent surface:** the news-extractor gains `news.fetch` as a second READ tool
(fetching provenance-tagged text is what a news extractor does; it still holds
no market-number, registry-write, solve, workflow, or trade tool). Its brief:
fetch → write a short qualitative **news-risk summary** (the human judgement)
→ construct ≤3 typed risk views quoting the fetched items → `research.apply_views`
with the fetched text as `excerpt` (the provenance gate checks quotes against
real fetched items, not operator paste).

## P4.6 — Corroboration haircut

Before entropy pooling, `research.apply_views` computes the deterministic
hard-signal regime (turbulence / absorption / vol) and **haircuts each view's
confidence** when it contradicts the hard data (a "fatter-tail / higher-vol"
view in a calm regime, or vice versa) by a mandated factor; agreement keeps
full confidence. The haircut is reported in the run so the audit shows news
was corroborated against hard signals — TradingAgents' corroboration instinct,
made deterministic and boundary-safe.

## P4.7 — Calibration ledger + dry=false research arm

**Calibration:** applied views persist with their `as_of` and target; a
resolver (reflection-loop shaped) later scores whether the realized risk moment
over the horizon moved toward the view (did "fatter tails" precede realized fat
tails?). A per-source / per-view-type calibration score accumulates.

**dry=false:** the conditioned moments (means pinned) flow into a **research
arm** — a news-conditioned min-variance/MVSK arm in the ablation, DSR-counted
honestly, staged research, never operational. This closes the loop:
news → views → conditioned tensors → research backtest → calibration score.

## Execution partition (no file conflicts)

- Codex A (isolated): `qlab/news/feed.py`, `qlab/news/__init__.py`,
  `configs/news_sources.yaml`, `tests/test_news.py`.
- Codex B (isolated): `qlab/news/calibration.py`, `tests/test_calibration.py`
  (pure scoring functions).
- Claude (boundary/surface): `news.fetch` tool + allowlist + proxy;
  extractor role + loader sync; the P4.6 haircut in `apply_views`; the P4.7
  registry persistence, dry=false conditioning gate, and research arm.

Each feature: verify green → independent Codex review → one commit. Q1 review
sweeps (governance/data/tui) run read-only in the background and fold in after.
