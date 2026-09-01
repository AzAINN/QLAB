# The mind on the desk: what shipped, and what has never run

Completion record, 2026-08-31. Closes the plan and design record of the same
date ([design](2026-08-31-atlas-terminal-design.md),
[plan](2026-08-31-atlas-terminal-plan.md)). 30 commits over `222c293`, ending
at `425d520` plus this record.

## What the operator asked for, and where it stands

The operator's complaint was that the desk feels headless: ATLAS is a chat that
is not a session, and the one verb that gives the real thing takes the desk
away to give it. The four findings of the design record, against what is now in
the tree:

| # | Found | Now |
|---|---|---|
| 1 | The ATLAS chat is one stateless completion wearing a chat's clothes | unchanged, and now stated — the *terminal* is what got a real session |
| 2 | `/cli` gave the real Claude CLI and took the whole desk with it | `/cli` opens a pseudoterminal pane inside the ATLAS column; the sidebar and the PULSE rail stay |
| 3 | The door could not tell a chosen mind from a default | `chosen` rides the owner's llm payload; a desk that never chose is asked, once |
| 4 | Booking still waits on a keystroke after a complete governed analysis | **not built** — the standing authorization is decided in the design record and owned by the next stream |

## What shipped, per task

**A1 — the pty session** (`27a400e`, `d31c63c`). `clients/atlas-tui/src/pty.rs`,
gated `#[cfg(feature = "operator")]`: `PtySession::open` puts `qlab cli` on a
`portable-pty` slave, drops the slave, and a blocking reader thread posts
`PtyEvent::Bytes` / `Exited` / `Failed` onto the existing bus. `Spawn` is a
trait so a scripted `sh` stands in for the child in tests. The child is always
the desk's own verb — `DeskCli` builds `[launcher, "cli"]` byte-identical to
`handoff::argv`, because which tools and which persona a session gets is decided
in `qlab/tui/claude.py` and must not be re-answered where nothing checks it.

**A2 — the terminal widget** (`385a5d7`, `a92f8de`).
`src/ui/widgets/terminal.rs` renders a `&vt100::Screen` through `tui-term`'s
`PseudoTerminal` and nothing else — no IO, no clock, no store. `area` is the
whole pane including its border and the pty is sized to `block.inner(area)`, so
a child is never told a geometry it is not drawn into. The border states who
holds the keyboard in both directions, and a pane under `MIN_W` 43 × `MIN_H` 5
refuses with a sentence naming the width it needs rather than drawing a
terminal whose exit is unsaid.

**A3 — store and bus** (`0ea7597`, `dccdfaa`). The `vt100::Parser` lives in the
store and advances in exactly one place, unreachable from `ui/`, so a frame
stays a pure function of `(store, fx, instant)`. Bytes set `dirty`. `Ended`
carries the sentence that names the status and how to start another. Every
`PtyEvent` is stamped with a monotonic pane id and dropped on arrival if the
pane that produced it is gone — see the defects section, this is the one that
could have killed a live Claude session.

**A4 — the keyboard changes hands** (`2609a29`, `f70b55c`). A new
`Source::AtlasPane` in `input.rs` (the explicit `[Source; 25]` became 26) with
a `KEYMAP` row and a help row. While the pane holds the keyboard every key is
the child's — `q`, `/`, digits and Ctrl-C included — and `Ctrl-]` is the one
key the desk keeps. Ctrl-C stopped being an unconditional `Command::Quit`: a
terminal that cannot interrupt its child is not a terminal, and the quit path
is one key away with the border naming it. On this client `Ctrl-]` arrives as
`KeyCode::Char('5')` + CONTROL (crossterm's legacy C0 mapping, no kitty flags
anywhere in `src/`); both spellings are accepted and neither may be
"simplified" away.

**A5 — ATLAS hosts the pane** (`dff34a1`, `78f3a15`). The main column follows
the *child*: the pane while one runs, today's chat otherwise, and the chat
returns byte-for-byte. `/cli` on an `Ended` pane restarts in place; `c` closes
one and gives the column back. The pane records its inner rect into a
`Cell<Rect>` during draw — this client's own idiom, already used for
`input_row` and `book_word` — and the resize is compared after the frame, so no
store is mutated inside a draw and the layout arithmetic has one home.

**B1 — the owner can say nobody chose** (`53cc4bf`). `qlab/core/llm_config.py`
gains `llm_config_chosen()`; the llm block on `/api/tui` and the `POST
/api/llm` response both carry `chosen: bool`, derived at both sites rather than
stored. A config file that predates the flag reads as chosen — it was written
by somebody. A desk seeded only by `QLAB_LLM_*` reads as *not* chosen, so the
door asks: a desk configured by a stale variable that is never asked defeats
the feature for exactly the operators who tinkered.

**B2 — the door asks which mind** (`f83ade6`, `7b83f74`). `Door::wanted`
returns `Option<Step>` keyed on both `chosen` flags, so a desk with a settled
posture and mode but no mind opens straight at `Step::Model`; answering retires
the door. Codex is listed and refused by name with the reason (its own backend
id, launcher and `~/.codex/config.toml` wiring are a different stream). The
startup poller now fetches the backend catalog beside the news, so the question
does not greet the operator with a list it is still loading.

**B3 — the copy that had become wrong** (`0b6ec5a`, `425d520`).
`settings.rs`'s `handoff_note` claimed `/cli, /build: claude, not <backend>`;
it is `atlas_note` now — `chat: ollama · /cli: claude's verb` on a
local-reasoner desk, and `ATLAS: the chat, or a /cli terminal` on a Claude desk,
which previously got no line at all. `cmd.rs`'s `/cli` picker hint said the
child runs "on this terminal"; it says "in the tab beside the desk".

## Two premises stated to the operator, then disproved

Both were said out loud before they were checked, which is why they are here
rather than quietly corrected.

1. **"The ATLAS chat is a Claude session."** It is not, and never was. The
   owner answers a chat message with one stateless
   `build_backend(choice.backend).complete(system=…, user=…)` — the desk's JSON
   context plus the question, one completion, no tools, no session, nothing to
   resume. `ClaudeSession` (the `--print --output-format stream-json --resume`
   path) serves the workforce coordinator only, and `build_claude_argv`'s
   `chat=True` branch has no live caller. So "it feels headless" was exact, and
   this stream did not fix it: it put a real session in the *terminal* beside
   the chat and left the chat as it is.
2. **"The proposal card stays beside the pane."** Measurement said otherwise.
   The card, the refusals and the your-call pointers live in ATLAS's own WOULD
   DO column, which is precisely the width the pane takes; what stays is the
   shell's PULSE rail — regime, stress, drawdown, gross, drawn for every view.
   The arithmetic in that ruling was right and its justification was wrong,
   which is a worse failure than a wrong number: the design record was amended
   in place (`2fb0c48`) rather than left to read as if it had been checked.

Two more the plan asserted and the work refuted, recorded for the same reason:

- **`CommandBuilder` does not start from an empty environment.**
  `CommandBuilder::new` seeds from `std::env::vars_os()`. Inheritance is what
  the child wants — credentials, locale, certificates — so the five that matter
  (`PATH`, `HOME`, `TERM`, `QLAB_UI_PORT`, `QLAB_BIN`) are set as *overrides*.
  A1 caught it; the plan and every downstream brief were corrected (`62ab7f7`).
- **The controller's EOF diagnosis was wrong.** A held slave descriptor does
  *not* block EOF on macOS. What hangs is reaping before draining: BSD
  `ttyclose` makes the child wait for the tty output queue to drain and the
  master is the only drainer, so `wait()` before any read blocks forever. Six
  probe binaries settled it against the controller. The rule that survived —
  drain to EOF first, reap after; to stop early kill *then* wait — is what
  `watch()` implements.

## The measured layout

Read off the real 120×36 frame, not derived: ATLAS is rail 8 · chat 45 · WOULD
DO 32 · sidebar 33. A terminal confined to the chat's 45 columns would make
Claude unusable, so while a child runs the pane spans chat **and** WOULD DO —
77 columns at 120, 117 at 160, 87 at 96 once the rail is gone — and the sidebar
stays, because it is the desk context that makes this a desk rather than a
terminal and so is the last thing to go. Below `PANE_MIN_W` 60 the sidebar goes
too and the pane takes the whole content width; below the widget's own 43×5 it
refuses. All four numbers are pinned by a test rather than left as prose: a
number checked nowhere is how the stale "the column is 45" survived a whole
task.

## The rulings that shaped the work

- **The column follows the child, not the config.** ATLAS keeps one identity and
  two bodies. Keying on the child rather than on the configured mind keeps one
  less piece of state, leaves a Claude desk with no session behaving exactly as
  it does today, and means no frame can claim a terminal that is not there.
- **The child is always `qlab cli`**, never `claude` directly, on both the pane
  and the hand-off. A client assembling its own command line would be a second
  unreviewed answer to "which tools does this session get", living where
  nothing checks it.
- **The pane is operator-only by construction.** The monitoring build contains
  no pty, no spawn and no forwarded keystroke — the same by-absence property it
  already has for writes. In the armed build the pane is posture-gated exactly
  as `/cli` was.
- **Ctrl-C belongs to the child while the pane holds the keyboard.** This
  reversed a rule the codebase argues for at length, so it owes the comment and
  the test it now has.
- **Focus is explicit, visible, and reversible**: `Ctrl-]` out, `i` or a click
  in, and the border states the holder in both directions. A pane that silently
  swallowed `q` would read as a hung client.
- **A child that dies is an answer.** Exit status, a failed spawn and a child
  that ended on its own each reach the operator as a sentence naming what
  happened and how to start another (invariant 4 applied to a process).
- **A dead child takes no keys.** Once the state is `Ended` the keyboard is the
  desk's again — `write` to a dead child emits one sentence per keystroke, so a
  pane left focused after an exit would fill the desk with them.
- **`/build` keeps the full-screen hand-off.** Claude Code editing this checkout
  wants the whole terminal; `handoff.rs` is unchanged and still tested.
- **The door asks once and the owner remembers**, and a door that cannot reach
  the owner refuses rather than guessing. An env-seeded desk counts as never
  having chosen; a config file that predates the flag counts as chosen.
- **Codex is named, not built** — reserved at the door and refused with the
  reason.
- **A2 owns the border copy**, A4 implements the keys it names; A5 wrote exactly
  one new border line, for the `Ended` state, because that is a new state's
  sentence rather than a rewording.

## What the reviews caught that would otherwise have shipped

Six defects, none of which the implementing task's own tests would have found.

1. **A stale pane event could kill a live Claude session** (A3 review,
   Important). `PtyEvent` was anonymous on a bus that outlives the pane that
   produced it, and both apply arms dispatched on "is there a pane" alone.
   Open A → close (`Drop` *signals* the kill but does not join the reader) →
   open B → A's in-flight `Exited` lands on B, and assigning `Gone` drops B's
   live `PtySession`, whose `Drop` kills a running Claude. Not merely a wrong
   label — the reviewer demonstrated it with two tests against unmutated
   source, and found the consequence the controller had missed. A's trailing
   bytes also painted into B's screen. Fixed with a monotonic pane id stamped
   on every event and checked on arrival. Two cheaper candidates were tried and
   refuted: a weak handle has nothing to hold it, and draining at close cannot
   work because at close time the events are not on the bus yet — the staleness
   lives in the bus queue, so the identity has to travel on the event.
2. **Three C0 bytes were silently dropped** (A4 review). `control_byte`
   implemented only the `@`..`_` arithmetic, so Ctrl-`\` (`'4'`), Ctrl-`^`
   (`'6'`) and Ctrl-`_` (`'7'`) — arriving by the same legacy mapping already
   read for `'5'` — produced nothing. Ctrl-`\` is the SIGQUIT escape hatch the
   Ctrl-C ruling assumed exists: a child ignoring SIGINT could not be killed
   from the pane. Ctrl-`_` is readline undo. The unit test had picked `'3'`,
   the one digit that never arrives that way. Fixed with a digit arm placed
   *before* the generic arithmetic — `'4'..='7'` pass `is_ascii()` but stay
   under 0x40 after `to_ascii_uppercase`, so the generic arm would have
   re-swallowed them.
3. **A click froze the desk's own keys** (A5 review, Important F1). On a pane
   below the widget's floor, `atlas.rs` returned before `draw_input`, so the
   input row's rect was never retracted while the pane's was; a click fell
   through to the ask-row branch, `typing()` went true, and the new guard
   swallowed every key. Demonstrated at 50×36, and neither `i` nor `Ctrl-]`
   recovered it. Fixed by retracting the row at the top of `draw`, beside the
   pane and the book word.
4. **A waiting proposal was completely invisible under a pane** (A5 review,
   Important F2). YOUR CALL, the pending count, the plan id and the referee
   line are all in the column the pane covers; there is no approvals chip on
   the status line by design, and the rail's BOOK entry carries no count. An
   operator could watch Claude while the desk held a question nobody could see.
   The count now rides the pane's own top border beside the child's name —
   `2 need your call · 4 BOOK` — dropped rather than clipped where there is no
   room.
5. **The cursor bug the required startup fetch would have created** (B2 review,
   High). A door opening straight at `Step::Model` parks its cursor at row 0,
   and `settle_model_cursor` fired only on the *first* catalog — useless if the
   catalog landed before the door existed. Unreachable at the time, and made
   the likely ordering by the very `poller.backends()` call the same review
   required, so both had to ship in one round. The reviewer ran it and watched
   the cursor sit on `reasoner claude:inherit` instead of the live
   `ollama:qwen2.5:7b now`. Fixing it exposed two more: scrolling against a
   zero-height area (the door would have opened claiming four rows were above
   while there was room for all nine), and an `Esc` that would have trapped the
   operator, because escape funnels through `finish`.
6. **A note that vanished with no marker** (B3 review, Medium). The new tab
   sentence was excluded by `wanted + cost(&said) <= left` *before* it reached
   the hidden-count accounting every other deferred reason gets, so at 38 cells
   — or at any total width ≤117 — it simply was not there, with no `▾ n more`
   to say so. That is the same class of defect this stream had just fixed on
   the pane border. Re-deriving it turned up a second, independent cause: the
   rights section charged a hardcoded one row for the asymmetry line, which is
   37 cells with its space, so at 36 it wrapped and the section drew five rows
   against the four it had paid for. Each was isolated by its own test and each
   reverted alone to prove neither was redundant.

Two smaller ones worth the record: a B3 test was **passing for the wrong
reason** — it armed the store before a snapshot, which recomputes posture, and
never reached SETTINGS, so its claim was unfalsifiable; and the controller's own
*prescribed* assertion shape for A1's env pins passed vacuously with
`QLAB_UI_PORT` unset, and was replaced by a policy test that fails either way.

## What has NEVER RUN LIVE

Recorded plainly, under its own heading, because none of it is covered by the
offline suites and all of it is what an operator meets first.

- **No real `qlab cli` has ever run in the pane.** Every child in every test is
  a scripted `sh`. The stack has been proven end to end with `printf` and with
  an interactive `sh` reading keystrokes back, and the argv is pinned
  byte-identical to `handoff::argv` — but Claude's own CLI, with its alternate
  screen, its resize handling and its own idea of the cursor, has never been on
  the other end of this pty on this machine.
- **No wide-character or SGR fixture exists anywhere.** Nothing in this
  codebase feeds the parser a CJK glyph, an emoji, a combining mark or a colour
  run. `vt100` and `tui-term` are presumed correct on all of it. A pane that
  mis-columns wide characters would look exactly like a working pane until an
  operator typed one.
- **A child's DECSCUSR cursor shape cannot reach the pane at all.** Verified in
  `tui-term-0.3.4/src/vt100_imp.rs`: `impl Screen for vt100::Screen` overrides
  only `cell`, `hide_cursor` and `cursor_position` — there is no `cursor_shape`
  override, so the trait default returns `Default` on every frame, and
  `vt100 0.16` exposes no API to supply one. A child asking for a bar or an
  underline cursor renders as a block. Cosmetic, unfixable from here, and named
  so the first operator to notice it finds it written down.
- **The door's mind question has never been answered against a live owner.**
  Every test builds the payload itself. The one desk this was written on has
  `.lab/llm_config.json` already, so it reads `chosen: true` and opens no door
  — the path that greets a brand-new desk is the one with no live evidence
  behind it.
- **Neither of B3's new Settings sentences has been read on a real desk**, and
  the `Ended`, `Failed` and refusal sentences have only ever been read in
  goldens.

## Suite hygiene: the pty tests exhaust pseudoterminals

The Rust suites open real pseudoterminals, and at full parallelism this machine
runs out of them: `failed to openpty: Os { code: -6 }`, a different test each
run, and it reproduces on the unmodified tree, so it is the harness and not the
diff. Both legs were run with `--test-threads=4`, which is stable. The remedy
is to **serialise the pty tests** — a shared mutex or a serial guard — and on
no account to weaken an assertion to make a flake go away.

## Follow-ups

Carried whole from
`.superpowers/sdd/2026-08-31-atlas-terminal-plan/followups.md`. Each is
deliberately not in this branch; each names the task that surfaced it.

- A1: `is_already_gone(err) -> bool` as a pure predicate so the ESRCH mapping is
  pinned without a `MasterPty` fake (today that branch is untested).
- A1: `kill` is SIGHUP with no escalation — a child ignoring it and the master's
  hangup survives.
- A1: `drop(slave)`'s Linux half is inherited from the manual; the six
  adjudication probes covered macOS only.
- A2 (queued as a doc fix round before A5): `MIN_W`'s derivation is
  misattributed; the "column is 45" justification is stale at the settled 77;
  `draw`'s rustdoc omits the geometry contract.
- A2: no golden pins the refusal's wrapped layout.
- B1: `server.py:1664/1699` the local `chosen` (a `SurfaceModel`) shadows the
  response key `chosen` (a bool) — rename the local to `picked`.
- B1: a damaged `llm_config.json` returns `None` with no sentence naming the
  file (precedent-consistent with `load_desk_mode`; one `record_event` at
  construction would close it).
- Stream-wide, owed at C1: `qlab cli` has never actually run in the pane — every
  test child is a scripted `sh`. Also unproven: wide characters and SGR in the
  pane.
- A2/tui-term: a child's DECSCUSR cursor shape cannot reach the pane (no
  `cursor_shape` override in tui-term's vt100 impl; vt100 0.16 exposes no API)
  — a bar cursor renders as a block. Cosmetic; name it in the README/completion
  record.
- A4: three more assert lines pinning `control_byte` under the KITTY spellings
  ('\\', ']', '^') — correct by construction today but only `_` is incidentally
  covered.
- Suite hygiene: the pty tests open real ptys and race under parallel threading
  in a sandbox; serialise them rather than weakening anything.
- A3: a cheap assertion would close the unpinnable `Failed` exemption — apply a
  `Failed` through the fold on a live pane and assert state/screen/focus are
  byte-identical before and after; trivially passes today, fails the moment that
  arm gains a state effect.
- A5: `waiting_on_you` and `draw_sidebar` count the same questions through two
  copies of one rule — extract a shared counter so the border and the panel
  cannot differ by one.
- A5: a pane shrunk below the widget's floor still has no visible way out (`c`
  closes only an ended child; the border naming the keys is not drawn at that
  width).
- A5: the border count over-counts by one whenever a checked plan has a bound
  but non-covering approval — match `draw_sidebar`'s own `continue`.
- A5: add a narrow-width golden for the border count's drop-not-clip rule
  (pinned by substring assertions only today).
- B2: pin the three startup poller calls (news, rights, backends) with
  `operator_gate.rs`'s existing `production_files_mentioning` idiom — `main.rs`
  is bin-only, so deleting one goes unnoticed today.

## Known gaps carried forward, not fixed

- **A declined question costs a second door.** An operator who picks a mind is
  asked once; one who declines with keep/Esc on the *walked-in* door writes only
  the pair and is asked the mind again on the next run, while the mind-only
  door's own keep/Esc does write. The durability rule is applied to one declined
  question and not to the other — the same question. The blocker is real (one
  `Command` per keystroke); the inconsistency is not defensible, and it is
  recorded in the design record so it is a decision rather than an accident.
- **The border's your-call count can over-count by one** relative to the panel
  it points at, in the ordinary mid-flight state where a checked plan's bound
  approval is not yet covering. It fails safe — it over-counts, never hides,
  which is the opposite direction from the bug it was added to fix — and both
  items are real governance objects. Rated Minor and shipped as such.
- **The startup catalog fetch is unpinned.** `main.rs` is in no test binary, so
  deleting `poller.backends()` goes unnoticed, exactly like the
  `poller.news()`/`poller.rights()` calls beside it. A dropped call degrades to
  the documented ASK-row keystroke rather than a silent break, which is a third
  reason that row exists.
- **The booking keystroke stands.** The standing authorization is decided in the
  design record — authority from a persisted operator record, never from a
  boolean an agent can set, bounded and revocable — and is the next stream's
  work. Invariant 3 is unchanged by this branch.
