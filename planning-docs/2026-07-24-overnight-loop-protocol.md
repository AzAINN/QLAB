# Overnight implementation loop — protocol

**Status:** OPERATIVE while the roadmap-v2 loop runs. Read this in full at the
start of every loop iteration. The work list lives in
`.superpowers/sdd/roadmap-v2-ledger.md`; the design detail lives in
`2026-07-24-product-roadmap-v2.md`.

## The iteration (one ledger item, start to finish)

1. **Pick** the first unchecked item in the ledger (respect phase order
   P0→P4). If it is marked `blocked`, take the next one.
2. **Plan** briefly: read only the files the item touches; write the concrete
   edit plan (files, functions, tests) before any code.
3. **Implement.** Delegate to the Codex worker where suitable; author
   governance-critical code (registry, referee, mandate, execution paths)
   yourself and let Codex review it instead:
   - Coding delegation (workspace-write):
     `codex exec -s workspace-write --cd /Users/azainmac/codebases/quant-trading-agent "BRIEF"`
     Run via Bash with `run_in_background: true` (max-reasoning calls take
     5–20 min). The BRIEF must carry: exact files, the repo invariants that
     apply (from CLAUDE.md), the test expectations, and "do not commit; do
     not touch files outside the listed scope."
   - Config defaults are already `gpt-5.6-sol` + `model_reasoning_effort=max`
     — do not override the model name (`-m gpt-5.6` hangs).
4. **Verify.** `python -m pytest -p no:warnings` must be fully green. New
   behavior needs new tests. Never weaken an assertion. If Codex's diff
   fails verification twice, revert its changes (`git restore`) and either
   implement yourself or mark the item `blocked(reason)` in the ledger and
   move on.
5. **Review.** Independent read-only Codex review of the exact diff:
   `codex exec -s read-only --cd <repo> "Adversarially review this diff for
   correctness, boundary violations (single DuckDB writer, referee gate, no
   agent-reachable execution), and test gaps: <git diff>"` — apply verified
   findings only; discard speculation.
6. **Commit** with a conventional message (NO AI-attribution trailers).
   One commit per ledger item. Never push — pushing requires the owner's
   explicit permission, which an overnight loop does not have.
7. **Housekeep.** Tick the ledger item (append one line of outcome notes);
   restart the owner process if `qlab/ui/server.py` or anything it serves
   changed (`kill $(lsof -ti :8765)` then relaunch `nohup .venv/bin/qlab ui
   --port 8765 --no-browser --online`); update memory only for durable
   discoveries.
8. **Schedule** the next iteration with ScheduleWakeup (60–120 s delay,
   same /loop prompt). If a Codex background call is still running at
   iteration end, schedule a longer fallback (1200 s) and let the completion
   notification resume work.

## Hard rules

- **One ledger item per iteration.** Scope creep is the failure mode; note
  discovered work as new ledger lines instead of doing it inline.
- The eight CLAUDE.md invariants apply to every item, especially: tests use
  `Registry(":memory:")`, fail loud, `agents/*.md` + loader sync, paths via
  `qlab/paths.py`, quantum stays offline.
- Broken suite is never committed. If an iteration cannot reach green, it
  reverts and records why.
- Stop the loop (ScheduleWakeup stop:true) when: the ledger is fully
  ticked/blocked, or two consecutive iterations make no progress, or any
  situation needs a decision only the owner can make — leave a clear summary
  in the ledger's `## Log` for the morning.
- Token discipline: Codex (ChatGPT plan) carries implementation and review
  tokens; Claude carries orchestration and verification. Do not paste large
  file bodies into Codex briefs — name paths and let it read. Do not re-read
  unchanged files between iterations.

## Codex worker facts (verified 2026-07-24)

- CLI 0.144.6, logged in via ChatGPT; defaults `gpt-5.6-sol`, reasoning max.
- `-s read-only | workspace-write` selects the sandbox; `--cd` sets the root.
- A trivial call costs ~26k tokens at max reasoning — brief it well, call it
  once per purpose, not iteratively.
- Treat its output as a worker's draft: tests and review decide, not trust.

OWNER DIRECTIVE (2026-07-24, mid-loop): after P4 completes, continue the loop into the P5 quality phase above — deep code review and quality improvement until a sweep comes back clean.
