# Atlas and the workforce

Atlas is the desk manager: it decides what the desk should look at, starts the
governed work, and says why. This is the detail behind the summary in the
[README](../README.md).

## The read, and what it is allowed to do


Atlas runs continuously inside the owner on a heartbeat (`QLAB_ATLAS_INTERVAL_S`,
default 30s). Each tick it evaluates deterministic triggers against owner facts
and recomposes its **read**: one view across the regime panel, the news record,
and what the workforce concluded.

The read leads with the part a number cannot express — the **tensions**, where
the evidence disagrees with itself. "Prices are calm but the coverage is not"
is the case Atlas exists to surface. Conviction describes how much the evidence
agrees, never how likely a price move is.

Atlas escalates a material disagreement into the same registry-enforced debate
the workforce uses — allowlisted claim, two-round ceiling, adjudication the
reporter waits on. Its own tools are read-only in every mode: Atlas cannot
trade, cannot approve, and holds no tool that builds a plan. What Propose mode
permits is *starting a workflow whose reporter may prepare one* — and that plan
is still checked, referee-bound, and inert until you confirm it. `Ctrl-B` opens
its detail drawer from any view.

Modes: `observe` (monitor and brief), `research` (may start approved research
workflows), `propose` (may request a checked plan for human approval), and
`paused`. The mode is the authority statement and is shown wherever Atlas is.

**A fresh desk starts in `research` with autonomy on**, and that is the whole
point: Observe permits no workflow at all, so a desk that opened there sat
inert. Research is the highest mode that still cannot create a paper plan — the
template gate refuses every plan-creating template below Propose — so Atlas
researches unattended without the execution boundary moving an inch. Reaching a
fill still needs Propose *and* your explicit confirmation.

Dispatching work is not the same as running it. A workflow's phases only advance
while a coordinator walks them, so the owner starts one itself for the run Atlas
just registered — the same governed session a human would start, pointed at the
owner over HTTP. One at a time; a second dispatch is refused with a reason
rather than queued, because N Claude trees on one desk is a cost incident, not
autonomy. Its stream is republished onto the audit bus, so an unattended run is
something you can watch rather than a black box.

    QLAB_ATLAS_AUTONOMOUS=0   # queue work, wait for you to press start
    QLAB_ATLAS_DRIVE=0        # dispatch only; drive runs by hand
    QLAB_LLM_FAST=1           # judgment roles on the quick model

Fast mode trades depth for latency on the judgment roles and is also a toggle in
Settings. It is bounded in the one place that matters: the referee keeps its
tier, because a PASS must never mean *passed on the fast model*.

If no `claude` is on PATH, a dispatch still registers its workflow and says so —
you can resume it by hand with `: workforce`. Absence is reported, never
absorbed.

    qlab tui --claude offer   # default: show readiness, never prompt
    qlab tui --claude auto    # start the workforce after the first snapshot
    qlab tui --claude off     # start only when : workforce GOAL requests it

Inside the desk, the workforce view (key `4`) is a chat: type to the
coordinator and it deploys the five governed roles. Progress is a flowchart —
hover a node for that phase's live summary, elapsed time, and artifacts — and
the console stays quiet, printing one short note per agent (what it settled,
what runs next) and the run's results at the end. Full tool traffic remains on
the timeline (`~`). Follow-up messages continue the same session, the `■ stop`
button interrupts without losing durable phase state, and a run that stalls is
stopped by a watchdog rather than hanging the desk. Stop terminates the entire
Claude/coordinator/Agent/MCP child-process tree, marks the active durable phase
`interrupted`, and fences late child writes until an explicit resume. A
successful Claude exit that leaves a phase open is treated the same way rather
than leaving the desk painted `working`. Owner startup recovers orphaned
`running` rows, and the owner also expires rows older than the coordinator
lease. `: workforce GOAL`, `: workforce status`, `: workforce resume ID`,
`: workforce stop`, and `: workforce abandon [ID]` drive the same machinery
from the command row. Abandon permanently closes unfinished phases but retains
completed evidence, events, and the audit record; it does not delete registry
state.

`: chat MESSAGE` switches the same chat box to a read-only desk assistant —
ask about the portfolio, market, runs, or audit trail conversationally; it
holds observation tools only (no agents, no writes, no execution) and keeps
its own session, separate from the workforce coordinator.

For a core-only install:

    python -m pip install .
    qlab run-once --offline --dry-run

A normal wheel includes the default mandate, universe, experiment spec, and
agent definitions. Runtime state is never written into site-packages.


## Claude Code deployment

The neutral role definitions live in agents/. Regenerate both orchestration
formats with:

    python -m qlab.agents.loader sync

This writes:

- .claude/agents/*.md for Claude Code.
- .bob/personas/*.yaml for IBM Bob.

The generated optimization-runner receives algorithm discovery and staged solve
tools. It can inspect research and offline catalog entries, but the server
rejects attempts to execute them through the staged path.

There are two valid Claude modes:

1. Headless: Claude Code launches the combined qlab server from .mcp.json when
   no owner UI process is running.
2. Workforce desk: qlab tui owns the book and optionally launches a session-local
   `qlab-coordinator` against the qlab-operator proxy. The coordinator deploys
   only the five qlab roles, following the dependency graph the registry
   enforces: analyst, bounded challenger debate, optimizer on the final
   persisted decision, referee gate, then reporter. They can inspect, research,
   persist judgments, and request a dry rebalance preview; only the human-facing
   TUI can confirm paper execution.

Starting a retired standalone quant-lab or quant-trader module now delegates to
the guarded combined server, so those module paths cannot recreate the old
two-writer topology.

