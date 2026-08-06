# qlab — a governed agentic quant research desk

qlab turns research questions into reproducible portfolio experiments, promotes
only reviewed decisions, and books approved paper trades through one auditable
runtime.

The whole design is one boundary:

- **AI agents own judgment** — estimation windows, challenge cases, what the
  news supports.
- **Algorithms own numbers** — estimation, optimization, backtesting, metrics.
- **Deterministic code owns rigor** — point-in-time checks, the mandate, the
  referee gate, execution idempotency, the audit trail.

Nothing an agent says can move that boundary. That is the point of the project.

[![qlab demo — the Atlas workstation on live data](https://img.youtube.com/vi/Mhk9sOC2GfE/maxresdefault.jpg)](https://youtu.be/Mhk9sOC2GfE)

**▶ [Watch the demo](https://youtu.be/Mhk9sOC2GfE)** — the workstation on live
data, a governed workforce run, and the plan stopping at the human gate.

## Start

Python 3.10+ and Rust (for the workstation client).

```bash
python -m pip install -e ".[operator]"
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh   # skip if you have cargo
(cd clients/atlas-tui && cargo build --release)                  # one-time build
qlab tui
```

`qlab tui` starts the owner runtime and opens the **Atlas workstation** — the
Rust/Ratatui client in `clients/atlas-tui`, seven views on `1`–`7`: Desk,
Markets, Book, Research, Workforce, Audit, Settings. The launcher refuses
rather than falling back if the binary is not there.

```bash
qlab tui --classic    # the Textual client, against the same owner
```

`--classic` is the soak valve while the Ratatui workstation is being lived
with — both clients read the same `/api/tui` over HTTP, so it changes which
screen is drawn and nothing about what is running. The Textual client's views
are `1`–`9`: Atlas, Dashboard, Market, Workforce, Research, Book, Audit,
Reference, Settings.

The desk opens on synthetic data with a simulated book, so it runs with no
account at all.

To use real prices and, separately, your real Alpaca paper book:

```bash
alpaca profile login      # browser OAuth, paper-only by construction
qlab tui --live           # real prices, qlab's simulated book
qlab tui --alpaca-book    # real prices and your Alpaca paper book
```

Both are paper-only. There is no live-trading path to select and the browser
login cannot grant one. → [data lanes and whose book](docs/data-and-book.md)

## Atlas

Atlas is the desk manager. It runs on a heartbeat inside the owner, evaluates
deterministic triggers, and composes a **read** across the regime panel, the
news record, and what the workforce concluded. The read leads with **tensions** —
where the evidence disagrees with itself — because that is what a number cannot
say.

A fresh desk starts in `research` mode with autonomy on. Research is the highest
mode that cannot create a paper plan, so Atlas researches unattended without the
execution boundary moving. Dispatching work is not running it, so the owner
starts a governed coordinator for the workflow Atlas registered — one at a time,
with its reasoning republished onto the audit bus so an unattended run is
watchable rather than a black box.

Reaching a fill needs `propose` mode **and** your explicit confirmation.
→ [Atlas, modes, and the workforce](docs/atlas.md)

## One writer, always

DuckDB is both the research registry and the paper book, and exactly one process
opens it.

```
qlab tui
    |
    +-- owner HTTP runtime ---- DuckDB registry and paper book
    |       |
    |       +-- Textual TUI, web client, and atlas-tui observe over HTTP
    |       +-- qlab-operator gives the Claude workforce role-bound HTTP tools
    |
    +-- explicit human confirmation is required for paper execution
```

Every other surface talks HTTP. No MCP tool accepts a raw order.
→ [architecture](docs/architecture.md)

## Honest results

qlab records what it measured, including when that is unflattering.

| Result | Status |
|---|---|
| Simple benchmarks beat the MVSK arms out of sample (2018–2026) | MVSK stays research-stage |
| Quantum-inspired feature augmentation hurts the vol forecast — one variant lost 12 of 12 samples | off by default ([write-up](planning-docs/2026-07-30-ml-lane.md)) |
| Multistart's winning basin appears at restart 2–4 against a budget of 100–160 | early stopping: **71s → 6.5s**, identical answers ([write-up](planning-docs/2026-07-30-optimizer-audit.md)) |

The augmentation's *first* measurement looked like a 4× win. It was an artifact
of a cache that ignored its seed, so a robustness sweep silently returned one
repeated sample. Fixed and guarded by a test — a sweep with zero variance is a
broken sweep, not a strong result.

## Clients

| Surface | What it is |
|---|---|
| `qlab tui` | the Atlas workstation (`clients/atlas-tui`) — Ratatui, armed by default; read-only by construction only in the `--no-default-features` build ([README](clients/atlas-tui/README.md)) |
| `qlab tui --classic` | Textual workstation — the complete surface |
| `qlab ui` | same owner runtime, local web client |
| `qlab desk` / `qlab workforce` / `qlab events` | one-shot CLI verbs over the owner's HTTP and event stream |

Both workstations read the same `/api/tui`, so there is no window where the desk
has two faces that disagree. The Textual client stays reachable behind
`--classic` until the Ratatui one has been soaked on a real desk. Either
workstation can confirm a paper trade, and both are held to the same rule: the
fill needs the last six of the plan's own `targets_hash` typed into a confirm
modal, a referee PASS pinned to that same hash, and an owner that re-validates
the request and refuses it without a persisted approval. Whether the Ratatui
window may write at all is the owner's persisted posture, asked once at startup
— not a launch flag.

## Commands

```bash
qlab run-once --offline --dry-run     # one governed cycle; proposal-only, never books a fill
qlab daily-ops --offline              # reconcile, risk, reflections, triggers; cannot trade
qlab batch configs/specs/ablation_v1.yaml --offline
qlab desk                             # one status card
qlab workforce run "GOAL"             # headless governed run, streamed live
qlab events --kind workflow_phase     # tail the owner's audit bus
python -m pytest                      # full offline suite, no network needed
```

## Further reading

- [Atlas and the workforce](docs/atlas.md) — modes, autonomy, the coordinator,
  the five governed roles
- [Architecture](docs/architecture.md) — one writer, MCP surfaces, the algorithm
  catalog and its stages, the regime indicators, repo map
- [Data lanes and whose book](docs/data-and-book.md) — providers, Alpaca,
  configuration and state
- [News setup](docs/news-setup.md) — making the news window real
- [IBM Bob](docs/ibm-bob.md) — Bob as a governed client of the desk
- [UI validity](UI_VALIDITY.md) — what each surface's numbers do and do not
  support
- [planning-docs/](planning-docs/) — dated status, audits, and superseded plans

## Current direction

Explain *why* MVSK loses before adding solver complexity: lambda sweeps,
estimator sensitivity, bounded news views. The optimizer audit narrowed where to
look — the `n⁴` cokurt tensor is 104 MB at 60 assets, so MVSK is comfortable to
~25 names and should not be attempted past ~50. That is a memory wall, not a
governance preference.

Two smaller measured findings worth acting on: minimum variance pins to the
per-asset cap with an effective 3.7 of 25 positions, so its in-sample volatility
advantage is out-of-sample concentration risk; and the scenario-CVaR LP
overtakes SLSQP past roughly 50 assets while diversifying better.

On operations: real Alpaca paper integration, market-calendar scheduling, the
Bob adapters, and porting more of the surface into `atlas-tui`. The
live-on-Alpaca-book path has still never been exercised end to end.

Promotion of any offline experiment into the desk requires evidence, a catalog
stage change, tool review, and new governance tests.

## License

MIT. See LICENSE.
