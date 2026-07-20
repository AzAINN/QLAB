# Quiet Workstation TUI — Implementation Plan

> **Status: IMPLEMENTED.** This remains the current interface direction; active
> cleanup and algorithm staging are tracked in ../2026-07-19-continuation-ledger.md.

**Goal:** Build a calm, terminal-native operator console for qlab that keeps
portfolio facts, agent work, paper-trading authority, and audit state visible
without turning the screen into a grid of decorative cards.

**Product stance:** The console is the face of a risk-allocation research desk.
It is not an order-book terminal and does not imply tick-level market data. The
paper mandate, data source, and data age remain explicit at the point of action.

## Interface hierarchy

The shell has no top header.

```text
left context spine | switchable center canvas | agent work rail
---------------------------------------------------------------
current event / collapsible timeline
command line                         paper + data + MCP status
```

- **Left spine:** Desk, Market, Research, Audit, followed by the investable
  universe. It carries navigation and instrument context, not KPI cards.
- **Center canvas:** the dominant surface. Desk shows current-vs-target
  allocation and regime; Market shows a focused instrument; Research shows
  runs and solver evidence; Audit shows decisions and governance history.
- **Agent rail:** compact role/state/permission rows above an inspectable Claude
  and tool-activity stream. It is not a chat-bubble interface.
- **Bottom edge:** one current event, an optional timeline drawer, and a command
  input. Global status lives here instead of in a top banner.

## Runtime boundary

The TUI never opens DuckDB. It reads and acts through the existing UI API,
which remains the single paper-book owner for this vertical slice.

```text
Textual TUI --HTTP--> UI runtime --services--> Registry / broker / research
```

Claude Code currently launches the two stdio MCP servers from `.mcp.json`, and
those servers also open DuckDB. Running them while the UI runtime owns the book
would violate the single-writer boundary. The TUI therefore uses a bounded
stateless MCP proxy:

1. safe Claude `ask` sessions stream with a strict empty MCP configuration;
2. governed sessions load only `qlab.mcp.tui_proxy`, which calls the owner HTTP
   API and never opens DuckDB;
3. proxy authority is capped at observation, research, daily ops, and dry
   rebalance proposals;
4. paper execution remains human-confirmed until the R0 referee gate is enforced
   in code.

No unsafe fallback is permitted.

## Build slices

### Slice 1 — observable contracts

- Add cursor-capable registry event reads.
- Add a single `/api/tui` snapshot containing portfolio, market, agents, runs,
  decisions, plans, events, and system status.
- Keep data provenance explicit: source, as-of date, and bar age.
- Route paper actions through existing API operations; do not spawn a second
  direct-registry CLI process.

### Slice 2 — Textual shell

- Add `qlab/tui/` with API client, stream parser, widgets, and app.
- Implement Desk, Market, Research, and Audit center canvases.
- Implement left navigation/universe context and the persistent agent rail.
- Implement a collapsed-by-default event drawer and universal command line.
- Use in-place updates and event-id deduplication.

### Slice 3 — Claude visibility

- Stream `claude -p --output-format stream-json` without blocking layout.
- Give governed sessions only the stateless propose-only MCP proxy.
- Render assistant conclusions, tool activity, lifecycle, and errors as distinct
  work events.
- Never render hidden reasoning.
- Surface MCP configuration health and the governed session's propose-only
  authority.

### Slice 4 — verification

- Unit-test registry/API contracts and Claude stream parsing.
- Headless Textual composition and navigation test.
- API-action test proving the TUI path uses the owner session.
- Full test suite.
- Real-terminal smoke at wide and compact sizes.

## Interaction contract

- `1`–`4`: Desk, Market, Research, Audit.
- `j` / `k`: move through the universe.
- `:` or `Ctrl+P`: focus command input.
- `~`: toggle the event timeline.
- `Esc`: leave command mode or dismiss focused state.
- Execution is never a single unconfirmed hotkey.

Initial commands:

```text
view desk|market|research|audit
symbol TICKER
rebalance dry
rebalance paper
daily
batch
ask PROMPT
governed
timeline
help
```

`rebalance paper` must show an explicit confirmation step and always use the
word "paper".

## Visual rules

- Two major vertical dividers only; internal grouping uses alignment and space.
- One muted accent. Green/red only for directional or execution meaning; amber
  for pending/stale/degraded; strong red for a mandate breach or halt.
- No oversized KPI cards, chat bubbles, neon palette, or permanent proposal
  panel.
- Wide layout uses all three lanes. Compact layout narrows the spine and hides
  the agent rail behind a focus action rather than crushing the center canvas.

## Revisit after v1

- Full granular owner-hosted MCP runtime after R0 governance repair.
- Server-sent events instead of snapshot polling.
- Async solver/backtest jobs with progress.
- Real quote-provider adapter; until then the market surface remains explicitly
  daily-bar/synthetic.
- Proposal/referee schema once R0 governance repair lands.
