# IBM Bob

Bob is IBM's agentic SDLC environment — the Bob IDE, and Bob Shell for the
terminal. In qlab it has one job, stated precisely because the whole project is
an argument about authority: **Bob is a governed client of the desk, never an
authority inside it.**

That is not a limitation imposed on Bob. It is the same boundary every
orchestrator here lives behind. qlab's rigor is enforced by deterministic code —
the mandate, the referee gate, execution idempotency, one DuckDB writer — so
that swapping the model or the IDE driving the desk cannot change what the desk
is allowed to do. Bob inherits that guarantee by construction.

## Why Bob is a good fit for this desk

A quant desk is an SDLC problem wearing a trading hat. The repetitive work here
is not placing orders — it is regenerating role adapters after a prompt edit,
restarting the owner after changing code it serves, keeping the offline suite
green, and adding a catalog entry at the right stage. Those are the invariants
most easily broken by moving fast, and they are exactly what Bob's rules and
skills exist to hold. Meanwhile the judgment work — which estimation window,
which regime call, what the news actually supports — stays with the governed
roles and their evidence trail.

Bob also brings an approval model that composes with qlab's rather than
competing with it. Bob asks before it acts; qlab refuses unless a human
confirms. Two independent gates on the same action is the correct number for a
desk that touches a book.

## The connection: `.bob/mcp.json`

Bob attaches to the running desk through the same propose-only MCP proxy the
Claude workforce uses. Start an owner runtime, then open the project in Bob:

    qlab              # or headless: qlab owner

`.bob/mcp.json` points Bob at `qlab.mcp.tui_proxy`, which never opens DuckDB and
talks to the owner over HTTP only. Bob can read the portfolio, the regime panel,
the audit trail, the research runs, and the algorithm catalog; it can request a
*dry* rebalance preview. It cannot execute a paper trade, because no MCP tool
here accepts a raw order and execution requires `human_confirmed=True` from the
TUI.

The `alwaysAllow` list in that file is the governance boundary made concrete:
pure observation is auto-approved, and every tool that persists a decision,
starts a workflow, runs a solver, fetches from the network, or previews a plan
stops for an explicit human click. The second entry, the combined `qlab` server,
ships `disabled: true` — it is the no-owner headless path, and enabling it while
an owner is alive is refused by the port guard rather than quietly creating a
second writer.

## Roles from one source

`agents/*.md` is the single source of truth for the org chart.
`python -m qlab.agents.loader sync` projects it into `.claude/agents/*.md` for
Claude Code and `.bob/personas/*.yaml` for Bob, so the two orchestrators cannot
drift apart on what a role is or what it may touch.

Bob's own mode system is a close match for this: a custom mode is a role
definition plus instructions plus a deterministic set of permitted tools, and
its tool groups (`Read`, `Edit`, `Execute`, `MCP`, `Skill`, …) make an
**MCP-only** mode expressible directly — no filesystem, no editor, no shell,
which is precisely what every qlab role already asserts about itself.

Being straight about current state: `.bob/personas/*.yaml` is qlab's neutral
projection of a role, not yet a file Bob loads. Emitting real
`.bob/custom_modes.yaml` from the same source is the next step, and it is
tracked with the open schema questions in
[planning-docs/2026-07-26-ibm-bob-integration-options.md](../planning-docs/2026-07-26-ibm-bob-integration-options.md)
alongside the seams a Bob Shell coordinator backend would attach to. The model
invocation record already carries a `backend` column, so a Bob-served phase is
auditable the day it exists.


See the [README](../README.md) for how Bob sits beside the other clients.
