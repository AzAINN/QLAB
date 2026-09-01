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
reporter waits on. Its evidence tools are read-only in every mode: Atlas cannot
trade, cannot approve, and holds no tool that builds a plan — the four chat
action tools below start governed work and nothing else. What Propose mode
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
something you can watch rather than a black box. The same rule holds for
research started by hand: **one research workflow runs at a time**, and a second
start is refused by name (409) rather than opening a second proposal against the
same book.

### Starting work from the chat, and the three rights

Atlas can start its own work from the chat. Its action tools are four and are
named: `workflow.start`, `workflow.resume`, `atlas.task.create`, and the
read-only `approvals.list`. They are the one deliberate exemption to "Atlas
holds no tool that writes" — starting a governed run is the thing the desk
manager is for, and every run it starts is still gated by the template gate, the
mode, and the referee. Work it queued that nobody answered expires rather than
accumulating.

Three rights narrow this from the desk (Settings ▸ MODELS; `GET`/`POST
/api/atlas/rights`, persisted in `atlas_rights.json` under the state directory,
all three granted when the file is absent):

| right | what withdrawing it does |
|---|---|
| `web` | the chat and `qlab cli` are built without `WebSearch`/`WebFetch` |
| `workflows` | the owner refuses `workflow.start`, `workflow.resume` and `atlas.task.create` — **for the desk chat only** |
| `build` | `qlab build` and the `/build` key refuse and name the panel |

The asymmetry is the part to read carefully: a `qlab workforce run`, the owner's
own coordinator, the heartbeat's autonomous dispatch and a non-Claude reasoner
making its own owner call are bound by none of these. Rights are an operator's
stated intent, exactly like the posture — not a security boundary.

### Watching the book: `portfolio_watch` and the scout

A held name's qualitative record moving between windows — coverage,
corroboration, primary documents — is the `held_record_change` trigger, and it
maps to the `portfolio_watch` template: analyst → **scout** → reporter. The
template creates no plan and touches no weight, which is what lets a desk in
Research mode start it at all.

The scout phase is the `contender-scout` role, and it has eyes, not hands. Its
whole grant is `WebSearch`, `WebFetch`, `registry.recent_decisions` and
`registry.log_decision`; no data, moments, solver, backtest, verdict, preview or
order tool is reachable from it. On a backend without web tools it refuses the
phase by name rather than answering from memory. Every claim in its memo carries
a URL it actually fetched, and "nothing found" is a permitted finding.

Its excerpts reach the desk only through the provenance-gated news lane. A
contender *outside* the current universe becomes a `universe_change` approval —
answered one at a time on AUDIT or from the ATLAS chat, and a name enters the
mandate only by that answer.

    QLAB_ATLAS_AUTONOMOUS=0   # queue work, wait for you to press start
    QLAB_ATLAS_DRIVE=0        # dispatch only; drive runs by hand
    QLAB_LLM_FAST=1           # judgment roles on the quick model

Fast mode trades depth for latency on the judgment roles and is also a toggle in
Settings. It is bounded in the one place that matters: the referee keeps its
tier, because a PASS must never mean *passed on the fast model*.

If no `claude` is on PATH, a dispatch still registers its workflow and says so —
you can resume it by hand with `: workforce`. Absence is reported, never
absorbed.

On the Atlas workstation the WORKFORCE view (key `7`) is where a governed run
is read: the pipeline, the phase each role is in, and what it settled. The
console stays quiet — full tool traffic remains on the AUDIT view — and the
run's durable phases live in the registry, so nothing here is the run's state,
only its picture. A run that stalls is stopped by a watchdog rather than
hanging the desk. Stop terminates the entire Claude/coordinator/Agent/MCP
child-process tree, marks the active durable phase `interrupted`, and fences
late child writes until an explicit resume. A successful Claude exit that
leaves a phase open is treated the same way rather than leaving the desk
painted `working`. Owner startup recovers orphaned `running` rows, and the
owner also expires rows older than the coordinator lease. Headless, the same
machinery is driven by `qlab workforce run "GOAL"` and the owner's own
coordinator.

Three ways a run starts, and all three end at the same gate:

- the operator, from WORKFORCE or by approving a queued task;
- Atlas, from the chat, with `workflow.start` — which requires the `workflows`
  right *and* a registered `template_id`. The mode gate is attached to the
  template, so a start without one is refused by name;
- the beat, unattended, for every trigger kind except those in
  `_UNBOUNDED_TRIGGERS` — today only `held_record_change`, which can mint one
  task per held name per window and so is announced and left `queued` for a
  human (or for Atlas, from the chat) until a mint-site bound exists. Briefs
  and alerts (`owner_startup`, `data_recovered`, `kill_switch`,
  `new_research_run`) fire once per condition and start as they always have;
  `_WORKFLOW_TRIGGERS` is the separate set the daily budget counts.

The ATLAS view's chat box is the desk assistant and the command row at once.
`/ask` asks the desk what it would do, `/do` takes a proposal it is offering,
`/approve` and `/execute` open the hash-bound boxes, `/clear` empties this
window's chat pane. `/cli` opens the real Claude CLI wearing the Atlas persona
through the owner-backed proxy — owner tools plus read-only web, no shell and
no filesystem — and `/build` opens Claude Code on this checkout. What each does
to the screen is now different. `/cli` runs its child on a pseudoterminal
**inside the ATLAS tab**: the pane takes the chat and WOULD DO columns, the desk
sidebar and the PULSE rail stay drawn beside it, `i` or a click hands the
keyboard over, `ctrl-]` takes it back, and `c` closes a pane whose child has
ended. `/build` still leaves the workstation and gives the child the whole
terminal until it exits. A window the desk declined operator authority to is
offered neither, and `/cli` alone is also refused by name on a desk that
reasons with a local model — `qlab cli` is a Claude verb.

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
2. Owner desk: the `qlab` owner runtime holds the only registry handle and
   drives its own `qlab-coordinator` against the `qlab-operator` proxy. The
   coordinator deploys only the qlab roles a template declares, following the
   dependency graph the registry enforces: analyst, bounded challenger debate,
   optimizer on the final persisted decision, referee gate, then reporter. They
   can inspect, research, persist judgments, and request a dry rebalance
   preview. No role can book a fill: the BOOK box on the Atlas workstation is
   the one confirmation, bound to the plan's own `targets_hash`, and the owner
   re-validates the approval and the referee PASS before any leg is sent.

Starting a retired standalone quant-lab or quant-trader module now delegates to
the guarded combined server, so those module paths cannot recreate the old
two-writer topology.

