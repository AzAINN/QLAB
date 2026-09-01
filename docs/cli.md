# The qlab command line

`qlab` is the desk — a long-running workstation over the owner runtime; the rest
are one-shot verbs.

`qlab` is the desk — a long-running workstation over the owner runtime; the
rest are one-shot verbs. Every verb takes `--offline` (refuse the network,
serve cache/synthetic only). The direct-registry verbs refuse to run while an
owner runtime holds the book — alongside a running desk, use `qlab desk`,
`qlab workforce`, and `qlab events`, which speak HTTP.

**The desk**

```bash
qlab                           # the desk: live prices, simulated book, all operations
qlab --restart                 # from the base up: warn, choose runtime|book|everything, type it to agree
qlab --restart=everything --yes  # the scripted spelling; the old desk is archived to .lab-archive/, never deleted
qlab --offline                 # the synthetic no-network demo desk
qlab --alpaca-book             # real prices AND your Alpaca paper book
qlab --glass                   # keep this window read-only for the session
qlab --port 8800               # owner on a non-default port (default 8765)
qlab --claude auto             # Claude workforce startup: offer (default) | auto | off
qlab owner                     # the owner runtime headless, for a desk kept up as a service
```

**Governed autopilot** — proposal-only by default: a cleared plan becomes an
approval request, not a fill.

```bash
qlab run-once --offline --dry-run             # one full cycle: analyze + propose, no trade
qlab watch --interval 15m --offline           # run-once on a loop (e.g. 30s, 15m, 1h)
qlab daily-ops --offline                       # heartbeat: reconcile, risk, reflections, triggers
qlab autopilot --once --offline                # proposal-only daily ops on an NYSE trading morning
qlab recommend --as-of 2026-06-30 --offline    # print one allocation (YYYY-MM-DD), no trade
```

`run-once` and `watch` are the two **CLI verbs** that can book without a click,
and only when the operator has exported `QLAB_AUTOPILOT_EXECUTE=1` for that
process — an out-of-band authorization no agent can set, and one that skips the
confirmation alone: the referee PASS, the cost gate, reconcile and the mandate
still have to clear. `daily-ops` and `autopilot` never trade.

They are no longer the only clickless path. A running `qlab`/`qlab owner` books
on its own 30-second beat whenever a **standing grant** covers the desk's
current proposal — see [standing authority](#standing-authority) below. No CLI
verb creates a grant.

**Research, experiments, and data**

```bash
qlab batch configs/specs/ablation_v1.yaml --offline   # the reproducible ablation
qlab prewarm --universe core                          # pre-fill the data cache (core | candidates)
qlab news-setup                                       # choose news sources, guided
qlab news-check --provider alpaca                     # authenticate news, show what it returns
```

**Talk to a running desk** — HTTP only, safe alongside `qlab tui`:

```bash
qlab desk                                      # one status card for the whole desk
qlab workforce run "Rebalance the core book and challenge the estimator"
qlab workforce status                          # durable phase state of the latest run
qlab workforce watch --id <id>                 # tail a run someone else is driving
qlab workforce interrupt --id <id>             # freeze an orphan for explicit resume
qlab workforce run --resume <id>               # continue an interrupted run
qlab events --kind workflow_phase              # tail the owner's audit bus
qlab cli                                       # interactive Claude as Atlas, on this desk
qlab build "add a heatmap visual"              # Claude Code on this checkout
```

`qlab cli` opens the real Claude CLI wearing the Atlas persona, granted the
desk manager's own tools from `agents/atlas.md` through the owner-backed proxy
plus read-only web — no shell and no filesystem. `qlab build` opens Claude Code
on this checkout with its own default tools and its own interactive permission
prompts, so every edit is one you approve; if it touches `qlab/` or
`clients/atlas-tui/` you are offered `qlab --restart runtime` and never given
it. The workstation spells both as `/cli` and `/build`, and they no longer do
the same thing to the screen. `/cli` opens the session as a **pane inside the
ATLAS tab** — a real pseudoterminal running that same `qlab cli`, taking the
chat and WOULD DO columns while the desk's sidebar and the PULSE rail stay
beside it; `i` or a click gives it the keyboard, `ctrl-]` takes it back, and
the pane's own border says which of you holds it. `/build`, and `qlab cli`
typed at a shell, still take the whole terminal until the child exits. A desk
that reasons with a local model refuses `/cli` by name — `qlab cli` is a Claude
verb — and the monitoring build has no pane at all.

Both verbs read the rights the desk holds — set on Settings ▸ MODELS, persisted
in `atlas_rights.json` under the state directory. With `web` withdrawn,
`qlab cli` is built without the two web tools; with `build` withdrawn,
`qlab build` refuses and names the panel. The third right, `workflows`, is
refused for the desk chat and for nothing else: nothing on that card binds a
`qlab workforce run`, the owner's own coordinator, or the heartbeat's dispatch.

**Tests**

```bash
python -m pytest                       # full offline suite, no network, no accounts
cd clients/atlas-tui && cargo test     # the Rust client, offline fixtures, no owner
```

## Keys by pane

The workstation's bindings, read from the view sources. Anything that writes is
refused on a desk you have not armed, and on a window started with `--glass` —
the pane still draws, the key just does nothing, and the status line says which
posture you are in.

| pane | keys |
|---|---|
| everywhere | `1`–`9`,`0` jump · `Tab` cycle · `r` refresh · `/` command line · `?` help · `q` quit |
| ATLAS | `i` focus the chat · `Enter` send · `b` book the current proposal · `c` copy · `PgUp`/`PgDn` scroll |
| BOOK | `b` book · `n` next plan · `x` execute (the older two-step path) · `s` sort · `h` heatmap mode · `p` period |
| PRED | `r` run the board · `Enter` open a model · `↑`/`↓` pick |
| WORK | `i` ask · `S` start a workflow · `Enter` open a run |
| AUDIT | `a` answer the waiting approval · `R` reject it |
| SETT | `Tab` between cards · `a` Alpaca login · `t` test it · `m` desk mode / model / method · `k` cardinality · `c` news source · `R` revoke the standing grant (AUTHORITY) · `s` save · `v` verify |
| VIS | `Enter` render the selected artifact · `↑`/`↓` pick · `h`/`j`/`k`/`l` pan |
| MKTS | `s` sort · `↑`/`↓` pick |

`R` on the AUTHORITY card has no confirmation box and nothing to type. That is
deliberate: withdrawing authority is the safe direction, revocation is
idempotent, and a box between an operator and "stop" is a hazard. The
consequence is real — an accidental `R` costs a trip to the route to grant
again.

## Standing authority

A **grant** lets the owner book a proposal it already covers on its own
30-second beat, with no click. It is the operator's authority given in advance,
and it is bounded in every direction.

Granting is not a keystroke and is not a CLI verb. It is one POST that names
every ceiling; there are no defaults, and a missing ceiling refuses:

```bash
curl -sS -X POST http://127.0.0.1:8765/api/desk/authority \
  -H 'Content-Type: application/json' \
  -d '{"allowed_universe": ["SPY", "AGG", "GLD"],
       "max_notional": 25000, "max_turnover": 0.35,
       "max_orders": 8, "max_books_per_day": 2, "ttl_days": 7}'
```

Every one of those keys is required and must be positive; `ttl_days` is 1..30.
`granted_by` is optional and defaults to `operator`. The **method is the
desk's**, never the caller's — a grant pins `mandate.operational_policy` as it
stands when the grant is written, so changing the method later ends that
grant's cover, which is the safe direction.

```bash
curl -sS http://127.0.0.1:8765/api/desk/authority          # what stands, and what is left
curl -sS -X POST http://127.0.0.1:8765/api/desk/authority/revoke \
  -H 'Content-Type: application/json' -d '{"reason": "done for the week"}'
```

Revocation takes a reason and **no grant id**: the owner holds one live grant
and is the only thing that knows which, so a stale card naming an older one is
the hazard. Revoking when nothing stands is a 400 with a sentence, never a 409.

What a grant does **not** move: the referee PASS pinned to the plan's own
`targets_hash`, the mandate, the cost gate, reconcile, and the owner's
execution-time revalidation all still run, in the order they run today. A grant
replaces the per-plan click and nothing else, and `PAPER_AUTO` is the only mode
it can express.

The beat refuses, and says which, when the plan exceeds a ceiling, touches a
symbol outside the grant's universe (the plan is refused **whole**, never
trimmed to fit), is older than 120 seconds, has already started, or when an
anomaly suspends the grant: a halted book, an unclean reconcile, no execution
data permit, or a rejected or expired order in the recent window. An anomaly
input the owner cannot compute counts as an anomaly. Every automatic fill and
every distinct refusal is written to the audit bus (`authority.booked`,
`authority.refused`), and the booked rows *are* the daily budget ledger.

No agent can reach any of this: no MCP tool, chat action tool or proxy verb
creates, edits, reads or consumes a grant, and both write routes refuse a chat
origin. `POST /api/desk/authority` is exactly as unauthenticated as every other
owner route — what bounds a grant is its own ceilings, not the port.
