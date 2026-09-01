# What an extensive pre-push review found

Record, 2026-09-01, written before pushing 94 commits (`581e23c..b94ef3f`) to
the public remote. Four reviewers ran in parallel — secrets, repository
hygiene, code cleanliness, documentation truth — plus the controller's own
pass. Everything they found that was *fixed before the push* is in the commits
around this one. This file records what was **found and deliberately not fixed
here**, so it is a decision rather than an omission.

## Correctness, in priority order

1. **The book route strands a half-filled book, and it is the sibling of a bug
   this session already fixed.** `qlab/ui/server.py:5797-5810`: when
   `execute_plan_with_approval` raises, the handler invalidates the approval
   and re-raises. Its reasoning is sound for a plan that never reached the
   broker — a live approval must not stay spendable against a plan that just
   proved it cannot execute. But `execute_plan` sets the plan `submitted`
   *before* iterating legs (`qlab/trader/plan.py:216`), and accepts that state
   again on a later call so a crash mid-execution replays by
   `client_order_id` without double-booking. So a broker error on leg 2 of 20
   invalidates the very authority the resume path needs, leaving legs at the
   venue and no governed way to finish them.
   The One Desk stream fixed exactly this in `qlab/governance/proposal.py`
   (`withdraw_orphans` now skips `state == "submitted"`); this second site was
   missed. **Fix:** the same skip — if the plan is `submitted`, leave the
   approval alone and say so in the raised error.
2. **An operator's holdings cap can refuse the desk's own safety rebalance.**
   `qlab/trader/mandate.py:617` exempts the defensive basket from
   `max_holdings` at load, but the plan path (`qlab/trader/plan.py:194`) does
   not apply the exemption. With an operator-set `max_holdings=5`, the
   regime-triggered defensive rebalance is refused — the cap silently disables
   the protection it was meant to sit beside.
3. **Contenders past the third vanish without a word.** `qlab/ui/server.py:740`
   truncates the contender list before the loop, so the fourth and later reach
   neither `opened`, nor `skipped`, nor the event — contradicting the docstring
   three lines above. Either count them as skipped or say the cap in the event.

## Dead seams (invariant 10)

- `clients/atlas-tui/src/pty.rs:215` — `DeskCli::new` is constructed only by
  the file's own `#[cfg(test)]` module. The same shape as `handoff::Child::Cli`,
  which this branch removed, from the same task. Give it a call site or fold it
  into `from_env`.
- `clients/atlas-tui/src/net/write.rs` — `atlas_mode`, `atlas_pause`,
  `atlas_resume`, `atlas_autonomy`, `workforce_fast`, `base()`: six owner-facing
  wrappers whose only callers are `tests/operator_gate.rs`. Pre-existing, not a
  regression, but a reader opening the write path finds five unreachable POSTs.
  Delete them, or wire the desk-mode controls they were written for.
- `qlab/state/registry.py:104` — `APPROVAL_KINDS` has no caller;
  `create_approval_request` persists whatever `kind` it is handed. Use it as the
  guard or drop it.

## Tests that cannot fail

- `qlab/governance/proposal.py:151` — the referee-PASS→`targets_hash` binding on
  the *read* path is uncovered: dropping the comparison leaves 386 tests green.
  Execution re-validates independently, so this is the displayed hash rather
  than a fill hole — but it is the number the operator confirms against.
- `clients/atlas-tui/tests/golden_visuals.rs:461` — the "renders identically in a
  monitoring build" test asserts only into its own glass snapshot; if the legs
  diverged, `cargo insta` would record a new glass snapshot and both stay green.
  Compare against the armed snapshot with `include_str!`.
- `clients/atlas-tui/tests/golden_atlas.rs:594` — the sole assertion sits after a
  `continue`, so a layout change that stops clipping makes every iteration skip.
- `tests/test_mcp_server.py:838` — an all-negative census with no positive
  control; it passes on empty sets.

## Fail-loud (invariant 4)

`qlab/autopilot/cli.py:900` — `build_atlas_cli_argv` is called uncaught while
`atlas_cli_tools`/`atlas_persona` raise `RuntimeError`, so a malformed
`agents/atlas.md` grant makes `qlab cli` print a traceback. Its sibling
`_cmd_build` raises `SystemExit(str(exc))`; match it.

## Documentation, fixed here

The autopilot's `QLAB_AUTOPILOT_EXECUTE=1` hatch was undocumented while README
and `CLAUDE.md` promised these verbs "never book a fill"; `.env.example` claimed
qlab does not read `.env` when `qlab/env.py` loads it as the first statement of
`main()`; `docs/data-and-book.md` opened by saying the desk starts offline when
it starts live; `docs/architecture.md` still advertised the retired Textual and
web clients; three planning-docs asserted since-fixed vulnerabilities in flat
present tense. Each is corrected in the commits beside this one, the planning
records by a dated banner rather than a rewrite.

## Hygiene, fixed here

`.lab-archive/` — 107 MB of runtime state, 2,166 files including a 42 MB DuckDB
— sat unignored beside the ignored `.lab/`; `git add -A` staged 2,182 paths and
now stages 17. `.claude/worktrees/` was ignored only in `.git/info/exclude`,
which does not travel. Three demo tapes hard-coded a home directory; 84
absolute-path links in a governance record rendered dead on GitHub; the
manifest had no `[project.urls]`.

## Decided, not changed

**The Python floor.** `pyproject.toml` declares `requires-python = ">=3.10"`
and `docs/install.md` says 3.10+. Nothing in `qlab/` uses a post-3.10 feature,
so the *package* claim is true — but the *suite* is not: `tests/test_ui.py:2276`
asserts json's "trailing comma" wording, which only CPython 3.13 emits, and CI
pins 3.13. A stranger on 3.11 installs happily and then sees a red suite.
Left as-is deliberately: narrowing `requires-python` to 3.13 would shrink the
supported audience to fix a test, and loosening the assertion is a code change
better made with a 3.10 CI leg beside it than in the minutes before a push.
Whichever is chosen, `docs/install.md` should say which version the *suite*
needs.

## Judged clean

No secrets, credentials, tokens, private keys, DSNs or real financial
identifiers anywhere in the 94 commits; every URL is loopback; `.env` is
gitignored and untracked; all 94 commit messages are free of AI-attribution
trailers; no binary is tracked and no vendored dependency exists; the 67 insta
goldens total 334 KB; `LICENSE` matches the manifest; every documented HTTP
route exists with the documented method; the desk's execution gate holds
exactly as written, and the book route is absent from both MCP tool surfaces.
