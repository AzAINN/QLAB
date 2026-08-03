# Two reasoners, one desk — the reconciliation map

**Date:** 2026-08-03 · **Status:** merge-prep record, decision is the operator's
**Sides:** A = origin/main after PR #19 `feat/atlas-reasoner` (+ news archive,
predictor tuning) · B = `worktree-granite-model-picker` (the granite-model-picker
plan: backend layer, per-surface config, picker/door, Ollama role harness).
Base for both: `8febbea`. Produced read-only from committed objects while B's
final build task was still landing; re-run `git merge-tree` after B's last
commit before acting.

## The short version

Textual conflicts are exactly **three**: `qlab/operator/reasoner.py` (add/add),
one adjacency hunk in `qlab/ui/server.py`, and the Rust fixture
`tui_snapshot.json`. Everything else auto-merges. The two reasoners are
**compositional, not competing**: A's forms grounded views (prose + citations
bound to archive hashes + ≤1 template offer; 864 lines; entry `reason()`/
`answer()` via `POST /api/atlas/ask`); B's chooses one template id for one
trigger behind `reasoner_enabled` (224 lines; entry `choose_template()` via the
heartbeat's two-phase tick). No symbol collides; neither imports the other's
machinery.

**The real competition is one seam: which layer owns "which model serves this
surface."** A has `models.py` (catalog, `ModelProvider`, `AnthropicCliProvider`
shelling the claude CLI, `model_selection.json` slots deep/quick/reasoner/chat,
`check_eligible` refusals). B has `llm_backends.py` (`LlmBackend` Protocol,
`OllamaBackend`, `ClaudeCliBackend`, `llm_config.json` surfaces
reasoner/workforce, live availability probes, `RouteDecision.backend`,
`REQUIRED_CLAUDE_ROLES = {"referee"}`). One must win.

## The recommended synthesis

**B's backend layer as transport, A's catalog as judgment, one config file.**
- Widen `LlmBackend.complete` to return A's `Completion` (its
  `stop_reason`/`is_silent`/`raw_model` drive real refusals in `reason()`
  :786-800 and would be lost by B's `-> str`; the adapter must be backend-side).
- `reason()` takes a backend instead of `complete=`; `ModelSpec` carries a
  backend so `check_eligible` can refuse "granite in the deep slot" the way it
  already refuses a small-context model.
- Collapse A's `reasoner` slot onto `llm_config.reasoner`; keep deep/quick/chat
  as slots (Claude-tier aliases per B's own `resolve_route` docstring).
- Keep from A: archive grounding, citation binding, `ReasonedView`, the ask
  surface, catalog eligibility. Keep from B: the backend Protocol + Ollama,
  per-surface config + probes, the referee pin, the two-phase lock discipline,
  template judgment, the Rust picker/door/switcher, the credential routes.
- Target shape: **A's `/api/atlas/ask` running on B's backend layer.** B's
  `atlas_message` then delegates to A's `answer()` (deleting the one duplicated
  inline prompt).

## Per-file resolutions

- `reasoner.py` (add/add): **concatenate, don't choose** — A's module keeps the
  name; B's 224 lines move to `qlab/operator/template_judge.py` (A's AST test
  asserts reasoner.py imports no HTTP-adjacent module, which B's `llm_backends`
  import would fail — the concrete argument for the split).
- `server.py` (one hunk): A's `predictor_board_summary()` verbatim, then B's
  `atlas_context(self, offline, *, facts=None)` signature, then the union of
  added context keys (`predictor_board` + `startable_templates`).
- `tui_snapshot.json`: take A's regenerated file, re-insert B's 8-line `llm`
  block (or regenerate from a merged owner), then re-accept insta snapshots on
  both sides — either side taken whole breaks the other's goldens.
- `atlas.py`, `coordinator.py`: B-only; take as-is.
- `heartbeat.py`: auto-merges correctly (A's `archive_desk_news` lands inside
  B's phase 1, still under the lock) — run one owner-tick test after.

## Post-merge follow-ups (small, named)

1. A's oauth-profile sentence still points at env vars; once B's
   `POST /api/alpaca/credentials` + the door's login form are in, it should name
   that door.
2. A `source="workforce_config"` route can return a model id A's catalog does
   not know — anything feeding `RouteDecision.resolved_model` into
   `models.get_model()` would raise `UnknownModel`; add the mapping or the
   refusal.
3. Two reasoner test suites coexist (A's `tests/test_reasoner.py` 870 L; B's in
   `test_llm_config.py`/`test_ui.py`) — fine once the file split lands.
4. No collision in credentials: B never touched `resolve_alpaca_credentials`;
   A's browser-login fix and B's `AlpacaConsentRequired` describe the same
   state from two surfaces.

---

## ADDENDUM (2026-08-03, post-final-review) — main moved again; amendments

Main is now `1c97b19` (PR #1 `feat/atlas-full-desk`: A's reasoner grew to
1413 lines with workforce/predictor context blocks; new server surfaces
`predictor_board_summary/_detail`, `workforce_summary`, `agent_stream`; the
`_RECORDED_KINDS` parser-vocabulary fix). Branch HEAD `edb2bb5` (+ the
final-wave commit after). `git merge-tree` now: **five conflicts, seven
hunks** — reasoner.py (add/add ×2), server.py ×2, atlas.py ×2,
coordinator.py ×1, the fixture.

**Amended resolutions (supersede §per-file above where they differ):**
- `atlas.py` (was "take as-is" — dead): h1 imports keep both lines; h2 =
  `mode = self.mode` (B's extraction) followed by A's `today = …` line.
- `coordinator.py` — **the one genuinely competing hunk**: take A's
  `_RECORDED_KINDS` tuple and comment verbatim (B's comment is FALSE — the
  Claude parser emits `tool_start`/`tool_result`, never `tool`/`agent`; A
  ships the test that pins it); keep B's five imports above it. Run A's
  parser-vocabulary test after resolving — it catches a botched merge here.
- `server.py` h1: A's FOUR methods verbatim, then B's `atlas_context`
  signature. h2 (new): union `"workforce": self.workforce_summary()` (A) with
  `"startable_templates": …` (B) — dropping A's key silently blanks PR #1's
  feature (`_workforce_block` reads it).
- `reasoner.py`: conclusion stands (concatenate; B's 224 lines →
  `template_judge.py`) but the rationale is amended — concatenation would
  PASS A's AST tests (`llm_backends` is not in FORBIDDEN_IMPORTS); the real
  argument is semantic collision between the two modules' prompt constants.
  Split on clarity.
- Fixture: unchanged (PR #1 touched nothing under `clients/`).

**Strategy unchanged**: PR #1 never touches the model seam — B-transport /
A-catalog stands; PR #1 adds one more consumer (`workforce`) to A's context.
Note: main independently fixed its own mid-dispatch workforce race (764872f);
B's final-wave commit fixes B's — the merge reconciles two parallel fixes of
the same bug.
