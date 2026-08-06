# Granite model picker — stream complete

**Date:** 2026-08-03 · **Status:** built + final-reviewed at `f57a387`, awaiting merge
**Plan:** planning-docs/2026-07-31-granite-model-picker-plan.md (14 tasks, all phases)
**Merge plan:** planning-docs/2026-08-03-two-reasoners-reconciliation-map.md + its addendum

## What shipped (49 commits, worktree-granite-model-picker off 8febbea)

- **Backend layer** (`qlab/operator/llm_backends.py`): `LlmBackend` protocol;
  `OllamaBackend` (local Granite via /api/chat; absence-vs-error discipline;
  `_head` = one collapse→redact→bound gate, six hostile-URL shapes + a
  class-level assertion) and `ClaudeCliBackend`.
- **Per-surface config** (`qlab/core/llm_config.py` + owner routes): reasoner/
  workforce each `{backend, model}`; `POST /api/llm` validated against the live
  catalog ("on validates, off validates nothing, changing-while-enabled
  validates the new pair"); `GET /api/llm/backends`; the `llm` block on
  /api/tui with `probed_at`.
- **Routing** (`model_routing.py`): routes carry their backend;
  `REQUIRED_CLAUDE_ROLES = {"referee"}` — pinned at three independent layers
  (route, coordinator plan, harness constructor), each mutation-proven alone.
- **The reasoner's first real calls**: `atlas_message` answers through the
  configured backend (live-smoked: a grounded desk read); template judgment
  behind `reasoner_enabled` with dual decision rows + `reasoner.divergence`
  events (the atlas-as-llm step-3 comparison condition); the deterministic
  observe survives every failure in the judgment path (chain-audited,
  guard-per-frame, all witnessed).
- **Alpaca login**: owner routes (0600 profile writer with oauth-overwrite
  consent as a typed `AlpacaConsentRequired`; one probe; leak-free at every
  status — header-unsafe and non-ASCII secrets die at one gate); the masked
  Rust form; test-connection verdicts; the consent flow rendering the owner's
  sentence.
- **The picker**: SETTINGS MODELS card (availability + honest age + inherit
  honesty); `/model` with catalog-only suggestions; **the startup door**
  (desk mode → models → credentials; LIVE choosable with login-before-apply;
  Esc always synthetic; `assumed` markers); the `m` switcher (cursor always
  visible by construction); owner `chosen: bool` so the door's trigger means
  what it says.
- **Workforce on Granite**: the Ollama role harness (news-analyst pilot; one
  allowlist gate; per-call clock + calls-per-turn cap; every event field
  bounded); per-dispatch mixed pipelines (one-role graphs → harness,
  multi-role → Claude coordinator; the referee claude twice over); the honest
  event-kind split (`route_pinned` / `route_unregistered` / `fallback_used`);
  invocation rows that cannot lie about their backend (workforce captured
  once per dispatch).

Final state: **pytest 1225 · Rust glass 580 / operator 733 · both legs
fmt/clippy clean · the armed qa.sh pass run clean · ~180 mutation proofs
across the stream, all firing.**

## The merge (the user's decision)

Main moved twice under this stream (PR #19 `feat/atlas-reasoner`, PR #1
`feat/atlas-full-desk`). The reconciliation map + addendum hold the current
truth: five conflicting files, seven hunks, exactly one genuinely competing
(coordinator's `_RECORDED_KINDS` — take main's; B's comment was proven false).
The strategic synthesis stands: **B's backend layer as transport, A's catalog
as judgment, one config file**; A's `/api/atlas/ask` on B's backends is the
target shape. Run A's parser-vocabulary test after the coordinator hunk.
Restart the owner after the merge lands (invariant 8 — many payload changes).

## Post-merge follow-ups (final review's ratified list)

shadow_scorecard's `fallbacks` needs a schema-level fix (notes column or
event-sourced counter) — its current error direction is conservative;
`degrades_result`: module-level `REQUIRED_DEEP_ROLES <= REQUIRED_CLAUDE_ROLES`
assert + a caller or retirement; Rust `Coordinator.note` field + render;
`self.fast` mid-dispatch capture (same shape as the fixed race, pre-existing);
per-card floor sweep in SETTINGS; owner `probed_age_s`; `qlab tui --pick`
passthrough; `note_reasoner_fallback` lock assertion; C2 LoggedIn-note bound;
Alpaca header-capitalization check on first credentialed soak;
`/api/models/invocations` reader if wanted; qa_capture `--dry-run` CI pin.

## Ratified deviations (do not "fix")

Harness-less/multi-role ollama workforce **runs on claude and records**
(refusing would brick governed reviews; the substitution is recorded per-role
and per-dispatch). E1's refusal survives only where the harness genuinely
cannot serve. The E2 cooperative stop and the C2 consent-box limits are
documented honest bounds.
