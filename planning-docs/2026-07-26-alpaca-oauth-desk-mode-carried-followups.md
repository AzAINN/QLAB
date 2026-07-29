# Carried follow-ups — Alpaca OAuth login and explicit desk mode

Status note for the branch implementing
`2026-07-25-alpaca-oauth-desk-mode-design.md`. Eight tasks shipped with
per-task reviews, six fix rounds, a whole-branch review, a fix wave, and two
scoped re-reviews.

Everything below was **deliberately carried**, not missed. Each was raised by a
review, triaged, and judged not worth blocking the merge. Recorded so the
reasoning survives.

## Look at these first

- **The web client has no desk-mode surface, and its pill actively contradicts
  the owner.** `qlab ui` is now a first-class desk-mode entry point (it takes
  both flags and persists them), but `qlab/ui/index.html` is untouched. Its only
  mode indicator is a checkbox-driven label that is `checked` in markup and
  reads "offline (synthetic)" on load; it never reads `/api/desk_mode`. An
  operator on `qlab ui --alpaca-book` sees "offline (synthetic)" while
  `/api/portfolio` and plan execution are on their real paper account. The
  design scoped the web client out, so this is a known gap rather than a
  regression — but it is the largest remaining honesty hole in the feature.
  Note the related data-lane defect *was* fixed: `/api/run_once` and
  `/api/daily_ops` now clamp their offline flag to the desk mode when the book
  is alpaca, so the dashboard can no longer pair synthetic data with the real
  book. The pill is what remains.

- **Alpaca market data is not wired to the OAuth login.** `data="live"` only
  sets `offline=False`; the provider still comes from `QLAB_DATA_PROVIDER`
  (yfinance by default). Worse, `_fetch_alpaca` reads `ALPACA_API_KEY` /
  `ALPACA_API_SECRET` from the environment directly and never consults
  `resolve_alpaca_credentials()`, so a browser-login-only operator cannot get
  Alpaca prices at all — and the resulting `KeyError` is swallowed by a blanket
  `except Exception: warn; return None`. The docs were corrected to say this
  plainly rather than oversell it; the wiring is the actual fix. The design's
  §3 mapping (`data="live"` → `DataPolicy.alpaca_operational(feed)`) remains
  unimplemented.

- **The startup modal gates the LIVE *data* button on Alpaca *broker*
  credentials.** With no Alpaca login you cannot choose live yfinance data from
  the modal, even though `qlab tui --live` grants exactly that. The corrected
  help text makes the inconsistency visible.

## Windows portability

A dedicated Windows audit of the whole branch diff found one blocker, which was
**fixed** (`qlab/ui/server.py`'s startup banner interpolated the mode chip's
`label`, whose U+00B7 cannot be encoded in cp932 or cp874; the owner is spawned
with `stdout=DEVNULL`, so CPython encoded that line with the locale codepage
under `errors='strict'` and the process died after binding the port and before
`serve_forever()` — on exactly the live modes this branch exists to serve). The
banner now prints ASCII `data=… book=…`. The rest is carried:

- **`_config_dir()` hardcodes the XDG path** (`Path.home()/".config"/"alpaca"`).
  If the Alpaca CLI follows the platform convention on Windows (`%APPDATA%`), a
  Windows operator who has run `alpaca profile login` has a profile qlab never
  looks at: `resolve_alpaca_credentials()` returns `None`, the modal's LIVE
  button stays disabled, and `--alpaca-book` refuses with *"run `alpaca profile
  login`"* after they already did. Loud but wrong. Not fixed here because
  probing `%APPDATA%` changes credential-discovery semantics on a real-money
  path and deserves its own approval. `XDG_CONFIG_HOME` is also unhonoured
  (cross-platform, not Windows-specific).
- **`ALPACA_CONFIG_DIR` — the escape hatch for the above — is documented
  nowhere.** Neither `README.md` nor `.env.example` mentions it, and both assert
  the POSIX path. One line in each would make Windows workable today.
- **`save_desk_mode` is non-atomic *and* Windows enforces share modes**, so a
  concurrent save (the TUI process and the owner process both write it) or a
  transient AV/indexer scan raises `PermissionError` (WinError 32) where POSIX
  merely interleaves. `load_desk_mode` catches `OSError`; `save_desk_mode` does
  not, so it would surface as a 500 from the desk-mode route. `os.replace()`
  onto a sibling temp file fixes both this and the torn-file window.
- **`cli.py`'s attached-owner `SystemExit` interpolates `mode.label`.** It cannot
  crash (CPython opens `sys.stderr` with `errors='backslashreplace'`), but a
  cp932 operator reads `LIVE \xb7 ALPACA BOOK`.
- **TUI glyphs (`·`, `■`) are font fidelity only.** Textual writes UTF-8 to a
  real Windows console, so no encode error; legacy conhost with a raster font
  shows replacement boxes.
- **The `OSError` → `AlpacaAuthError` wrap is untested on Windows**, because the
  chmod-based test skips there. Verified by running the affected modules under a
  shim with Windows `os.chmod`/`os.access` semantics: **exit 0, one skip, no
  failure** — the suite does not break on Windows, it just covers slightly less.
- The modal tests' `pilot.pause()` gates on a background-thread fetch are
  platform-neutral but are the likeliest of the new tests to flake on slower
  Windows CI.

## Operational sharp edges

- **A half-set env credential pair now takes down the owner's poll.** The
  partial-pair refusal was deliberately restored for the simulated book (it had
  been swallowed by the short-circuit), but `/api/tui` → `tui_snapshot` →
  `portfolio()` → `get_broker` means a stray single `ALPACA_API_KEY` produces a
  repeating 500 every two seconds and the TUI gets no snapshot at all. It leaks
  nothing (the message names variables, never values), but a startup refusal
  would be a kinder channel than an opaque poll failure.

- **No CLI route back to synthetic once a live mode is persisted** — only
  deleting `desk_mode.json`. This now traps `--online` users too, since that
  flag maps to `DeskMode("live","simulated")` and any flagged mode is saved. A
  `--synthetic` flag is roughly four lines. Judged Minor because the credential
  lapse re-prompts, execution still needs human confirmation, and the mode chip
  names the mode in force.

- **`desk_mode_payload` does two filesystem reads on every 2-second poll**
  (`config.yaml` plus the profile). Caching with invalidation on POST would fix
  it.

## Smaller residuals

- `AlpacaCredentials.__repr__` still carries `source`, an absolute home
  directory path. Not a secret, and `describe_credentials` omits it — but
  `handle_api` serialises `repr(exc)` into 500 bodies, so that path can reach an
  HTTP response.
- `get_broker`'s construction-failure message interpolates the underlying SDK
  exception. Safe today (alpaca-py does not echo credentials), and the same
  route that carried the token leak is now closed at source, but it remains a
  theoretical surface.
- `_render_mode_chip` still owns the `#chat-exit` label and `_sync_chat_input`,
  inherited verbatim from the old `_render_status`, so its name
  under-describes it and three Claude lifecycle hooks call a "mode chip"
  renderer purely for those side effects.
- The `CLAUDE` token in the Settings card lags by up to one poll, because the
  Claude hooks call the chip renderer and no longer re-render Settings.
- The approvals token changed shape (always shown rather than omitted at zero).
- `#settings-system`'s header is split across two tones so an existing
  `startswith("PAPER")` assertion could survive verbatim; its provenance line
  duplicates `#settings-data`'s. Both are the price of relocating assertions
  byte-identically.
- The verdict line loses its PASS/FAIL colour on the timeline, which is
  `markup=False`. The audit table still carries the toned verdict.
- `save_desk_mode` is a non-atomic `write_text`. A partial write degrades to
  `None` → default or re-prompt, which is the safe direction, and there is one
  writer.
- The paper-only integration guard still reads the private `_base_url`. A
  rename degrades to `""`, which fails loud rather than falsely claiming a
  paper account.
- **One more instance of the console-encoding bug class the banner fix named.**
  `_start_market_topics`' background thread prints `f"[qlab] quote topic failed:
  {exc!r}"` to the same DEVNULL-redirected owner stdout, and `exc!r` is not
  guaranteed ASCII. Pre-existing and untouched, but it is the natural next
  target now that the class has a name and a demonstrated consequence (an
  encode error on that stream took the owner down after the port bind).

## Test coverage worth adding

- `desk_mode_payload` when the resolver raises — every auth-error branch is
  covered a layer down, but the owner's translation of them into
  `credentials_ok=False` plus a reason is not.
- `UISession()` with `desk_mode=None` loading a persisted live mode.
- `book="alpaca"` reaching the Alpaca broker *through a UISession route*. The
  negative case (simulated wins over a discoverable token) is tested at both
  layers; the positive case for the book that touches real money is tested only
  at `get_broker`.
- The autouse fixtures do not clear `QLAB_DATA_PROVIDER` or `ALPACA_FEED`. Safe
  today only by accident of a blanket `except` in the data layer; one
  `monkeypatch.delenv` would make it deliberate.
- Because all 59 pilots now pass an explicit mode, `desk_mode=None` — the
  default production path on a first launch — is exercised by only three tests.
