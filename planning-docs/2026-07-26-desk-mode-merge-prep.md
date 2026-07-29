# Merge prep — `worktree-alpaca-oauth-desk-mode` into main

Branch: 26 commits from `6d96be7`. Suite green on the branch (756 passed,
10 skipped). All eight tasks plus a whole-branch review and three fix waves
are reviewed clean. **Not merged deliberately**: main has since gained the
news lane, and a second session has uncommitted work in the same files.

This note exists because the hazards below are the kind that **auto-merge
cleanly and change behaviour** — the failure shape that produced five silent
test failures in the previous cross-branch merge, where every conflict git
actually flagged was trivial and additive.

## Read this before merging

### 1. `.env` loading now intersects credential resolution

Main added `qlab/env.py`, called from `cli.main()` via `load_once()` **before
anything reads a credential**. This branch added `resolve_alpaca_credentials()`,
whose documented precedence is *environment wins over the CLI profile*. Neither
side knows the other exists, and they meet through `os.environ`.

The loader's exact semantics (read them before reasoning about this):

```python
for name, value in parse_env(text).items():
    if not value:                                 # empty .env values are SKIPPED
        continue
    if not override and os.environ.get(name):     # TRUTHINESS, not membership
        continue
    os.environ[name] = value
```

**Why this is dormant today:** the `.env` in the main checkout has
`ALPACA_API_KEY` and `ALPACA_API_SECRET` present but **empty**, so the loader
skips them and `resolve_alpaca_credentials()` treats them as absent.

**What arms it:** filling those two keys in — which is now the documented,
encouraged path. Then:

- `tests/test_desk_cli.py` drives real `cli.main(...)` in five places, so
  `load_once()` runs inside the test process and reads the workspace `.env`.
- This branch's autouse `isolated_alpaca_credentials` fixture **deletes** the
  Alpaca variables. A deleted variable is exactly the case the loader
  populates, because its guard only protects variables that are already
  *truthy*. The isolation is defeated by the intersection of two correct
  designs, not by a bug in either.
- Consequences: precedence tests that assume "no credentials" take the wrong
  branch; `get_broker(book="alpaca")` constructs a **real** Alpaca client
  inside the suite (watch for the wall time jumping from ~76s toward 110s,
  this branch's established live-call signature); and a *partial* fill raises
  `AlpacaAuthError`, which now surfaces as a repeating 500 on the owner's
  2-second poll.

**Fix that works:** neutralise the loader inside the isolation fixture —
patch `qlab.env.load_once` to a no-op, or seed `qlab.env._LOADED` with
`"default"`. **Do not** try setting the variables to `""`: the truthiness
guard ignores that and the file wins anyway. Tests must never read the
operator's `.env`.

Also note `workspace_root()` resolves per-checkout, so this is invisible while
testing inside the worktree (no `.env` there) and appears only once the branch
is merged and the suite runs in the main checkout. That asymmetry is why it
would look like a merge-induced mystery.

### 2. This branch's docs are now factually wrong and must be corrected in the merge

`README.md`, `.env.example`, and
`2026-07-26-alpaca-oauth-desk-mode-carried-followups.md` all state plainly that
**nothing reads `.env`**. That was true when written and is false on main.

The corrected statement, per the loader above: `.env` **is** read at CLI entry;
an explicitly exported variable outranks the file; empty values in the file are
ignored entirely. Note this partially mitigates the carried "Alpaca data lane
not wired to OAuth" item — `_fetch_alpaca` still reads `ALPACA_API_KEY` /
`ALPACA_API_SECRET` directly and never consults the resolver, so browser login
still cannot reach the data lane, but supplying env keys is now easier than the
docs imply.

### 3. Any new TUI pilot the other branch adds will fail after this merge

This branch made the desk mode a startup question: `QlabTui` with
`desk_mode=None` pushes a modal in `on_mount`. All 59 pre-existing pilots were
given `desk_mode=_SYNTH` so the modal never appears in tests. **A pilot written
against pre-merge `app.py` has no such argument**, so after the merge the modal
mounts and steals focus, and the failure looks nothing like its cause.

Fix per new pilot: pass `desk_mode=_SYNTH` (the module-level
`DeskMode("synthetic","simulated")` in `tests/test_tui.py`).

### 4. Contract-union risk in the files both branches touched

Nine files overlap between this branch and main's committed news work:
`.env.example`, `README.md`, `qlab/autopilot/cli.py`, `qlab/tui/app.py`,
`qlab/tui/theme.py`, `qlab/ui/server.py`, `tests/test_desk_cli.py`,
`tests/test_tui.py`, `tests/test_ui.py`. The second session additionally has
uncommitted work in `cli.py`, `app.py`, `server.py`, `claude.py`, `client.py`,
`heartbeat.py` and `README.md`.

Specific places where a clean merge can still be wrong:

- **`qlab/ui/server.py`** — both sides add routes, snapshot keys and
  dispatch-chain entries. The previous cross-branch merge failed exactly here:
  a required-artifacts tuple auto-merged into the *union* of both sides'
  entries, so tests written against either side broke with no conflict marker.
  After merging, grep for any list/tuple/dict that gained entries from both
  sides and ask whether the union is a valid contract.
- **`qlab/tui/app.py`** — this branch **deleted** `#system-status` and
  `#event-strip` and relocated seven assertions into Settings. Anything the
  other branch adds to the old status bar will either resurrect a deleted
  widget or be silently dropped.
- **`qlab/autopilot/cli.py`** — this branch added `--live`, `--alpaca-book`,
  `startup_desk_mode`, `desk_mode_argv` and rewrote `_cmd_tui`/`_cmd_ui`; main
  added `news-check` and `load_once`. Expect real conflicts in the parser
  setup and both command bodies.

## Merge procedure that matches the risk

1. Wait for the second session to commit — a merge touching `server.py`,
   `app.py`, `cli.py` and `README.md` will be refused while those are dirty.
2. Merge, resolve the flagged conflicts, then **read the clean auto-merges in
   the nine overlapping files** with the same care. That is where the last
   cross-branch merge actually broke.
3. Correct the `.env` documentation (§2) as part of the merge commit, not as a
   follow-up.
4. Apply the isolation fix (§1) before running the suite in the main checkout,
   or the result is not trustworthy.
5. Run the full suite on the merged tree and **check the wall time**, not just
   the pass count: ~76s is clean, a jump toward 110s means live Alpaca calls
   have leaked back into tests.
6. Sanity-check the three desk modes against a real account, as the branch's
   own smoke did: `book="simulated"` and `book=None` must return the simulator
   even with a valid OAuth token on disk; `book="alpaca"` must reach
   `https://paper-api.alpaca.markets`.
