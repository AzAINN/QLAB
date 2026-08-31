# The mind on the desk: a real CLI inside ATLAS, and a door that asks which mind

Design record, 2026-08-31. Binding authority for the plan of the same date.

## What the operator found

Verified against `main` at `222c293`:

1. **The desk feels headless, and it is — more so than first stated.** The
   ATLAS chat is not a Claude session at all. `qlab/ui/server.py:5259` answers
   a chat message with one stateless `build_backend(choice.backend).complete(
   system=…, user=…)` call: the desk's JSON context plus the operator's
   question, one completion, no tools, no session, nothing to resume.
   `ClaudeSession` (`qlab/tui/claude.py:1411`) — the `--print
   --output-format stream-json --resume` path — serves the workforce
   coordinator only, and `build_claude_argv`'s `chat=True` branch has no live
   caller. So the operator's "it feels headless" is exact: the chat is a
   single completion wearing a chat's clothes.
2. **`/cli` gives the real thing and takes the whole desk.** `handoff.rs`
   pauses the reader, leaves the alternate screen, spawns the child, and only
   repaints the workstation when the child exits. For the whole of a Claude
   session the desk — proposal card, audit stream, your-call pointers — is
   gone from the screen.
3. **The door already asks which mind — and cannot tell a choice from a
   default.** `ui/door.rs:180` walks Posture → Mode → `Step::Model`, offering
   the live catalog and posting `POST /api/llm` → `.lab/llm_config.json`. But
   `Door::wanted` (`door.rs:259`) keys the whole walk on the *desk mode's*
   `chosen` flag, and `startup_llm_config` collapses an absent config into
   `DEFAULT_LLM_CONFIG` with `llm_payload()` serving both identically — so
   the owner cannot say "nobody has chosen a mind", and the desk cannot ask
   about the model alone. What the choice does *not* do is change anything
   about the tab: picking Claude today yields the same single-completion chat
   a local model yields.
4. **Booking still waits on a keystroke** after a complete governed analysis.
   Decided in this record, built in the next stream (see *Decided, not built*).

## Rulings

- **The mind decides the tab's shape.** ATLAS keeps one identity and two
  bodies: with a local reasoner the main column is today's chat; with Claude
  the main column is a live terminal running the desk's own `qlab cli`. The
  desk sidebar — proposal card, tensions, your-call pointers — stays in both.
  That sidebar is what makes an agent session *on the desk* rather than
  beside it, and it is why the pane is not simply a bigger hand-off.
- **The child is always the desk's own verb.** The pane spawns `qlab cli`,
  never `claude` directly. Which tools, which MCP config, which persona a
  session gets is decided in `qlab/tui/claude.py` and tested there; a client
  assembling its own command line would be a second unreviewed answer to that
  question living where nothing checks it. `handoff.rs` already holds this
  rule and the pane inherits it verbatim.
- **The pane is operator-only by construction.** The `--no-default-features`
  monitoring build contains no pty, no spawn and no forwarded keystroke — the
  same by-absence property that build already has for writes. In the armed
  build the pane is still posture-gated exactly as `/cli` is today.
- **Focus is explicit, visible, and reversible.** While the pane holds the
  keyboard every key belongs to the child — digits, `/`, `q` included — and
  the frame says so on the pane's own border. One named key returns focus to
  the desk. A pane that silently swallowed `q` would read as a hung client.
- **A child that dies is an answer.** Exit status, a spawn that failed, and a
  child that ended on its own each reach the operator as a sentence naming
  what happened and how to start another. Invariant 4 applies to a process
  exactly as it applies to a route.
- **The door asks once and the owner remembers.** The first open with no
  persisted reasoner choice asks which mind runs Atlas; the answer is
  persisted by the owner, and the MODELS card stays the place to change it. A
  door that cannot reach the owner refuses rather than guessing — a desk that
  silently defaulted would be answering a question it was built to ask.
- **`/build` keeps the full-screen hand-off.** Claude Code editing this
  checkout wants the whole terminal and is a different kind of session from
  the desk manager. `/cli` becomes the pane; `handoff.rs` remains for
  `/build`, unchanged and still tested.
- **Codex is named, not built.** It needs its own backend id, launcher, and
  `~/.codex/config.toml` MCP wiring; the door reserves the choice and refuses
  it with the reason until that stream lands.

## Surfaces

| Surface | Today | After |
|---|---|---|
| ATLAS, local reasoner | chat log + input + sidebar | unchanged |
| ATLAS, Claude | the same chat, fed by a headless `--print` turn | a live `qlab cli` terminal in the main column, desk sidebar beside it |
| `/cli` | leaves the workstation, returns on exit | opens the pane in ATLAS |
| `/build` | leaves the workstation, returns on exit | unchanged |
| First open | whatever `llm_config` already said | a door: which mind runs Atlas, persisted by the owner |
| Settings ▸ MODELS | backend picker + rights | unchanged; still where the choice is changed |
| Monitoring build | no writes | no writes, no pty, no spawn |

## Decided, not built — the standing authorization

The operator's booking decision, recorded here so the next stream argues from
it rather than re-deciding it: **a bounded standing authorization**, granted
once on the desk and persisted like an approval, under which the *owner* books
a plan that has completed a governed analysis and carries a referee PASS
pinned to its own `targets_hash`.

The property that must not be lost: authority comes from a persisted operator
record, never from a boolean an agent can set. The agent gains no execute
tool — it finishes the analysis, and the authorization does the rest. The
grant carries bounds the operator sets (turnover per book, books per day, an
expiry), is revocable from the desk, and is auto-revoked by the kill switch or
a drawdown breach. Every automatic fill is evented and stated on the desk.
CLAUDE.md invariant 3 gains that second, recorded form of confirmation and
keeps its refusal of any agent-reachable execution path.

## Out of scope, by decision

Codex (named above); the standing authorization (next stream); a hosted or
remote MCP transport and the owner authentication it would require; scrollback
search or a multiplexer inside the pane; replacing the local chat.
