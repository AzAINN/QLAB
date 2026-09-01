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

**Governed autopilot** — proposal-only; these never book a fill:

```bash
qlab run-once --offline --dry-run             # one full cycle: analyze + propose, no trade
qlab watch --interval 15m --offline           # run-once on a loop (e.g. 30s, 15m, 1h)
qlab daily-ops --offline                       # heartbeat: reconcile, risk, reflections, triggers
qlab autopilot --once --offline                # proposal-only daily ops on an NYSE trading morning
qlab recommend --as-of 2026-06-30 --offline    # print one allocation (YYYY-MM-DD), no trade
```

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
| SETT | `Tab` between cards · `a` Alpaca login · `t` test it · `m` desk mode / model / method · `k` cardinality · `c` news source · `s` save · `v` verify |
| VIS | `Enter` render the selected artifact · `↑`/`↓` pick · `h`/`j`/`k`/`l` pan |
| MKTS | `s` sort · `↑`/`↓` pick |
