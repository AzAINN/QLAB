"""The qlab quiet-workstation operator console."""

from __future__ import annotations

import json
import subprocess
import threading
from datetime import datetime
from typing import Any

from rich.markup import escape
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    ContentSwitcher,
    DataTable,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Static,
)

from qlab.tui.claude import ClaudeEvent, ClaudeSession
from qlab.tui.client import gather_snapshot
from qlab.tui.formatting import (
    braille_chart, clean_report_line, demojibake, money, pct, phase_elapsed,
)
from qlab.paths import workspace_root


_WORKSPACE_ROOT = workspace_root()
_DEFAULT_TICKERS = ["ACWI", "BNDW", "GSG", "IGF", "GLD", "VNQ", "EMB"]

# ---------------------------------------------------------------------------
# Palette — one Bloomberg-inspired high-contrast scheme, defined once and shared
# by the CSS (below) and every inline markup color. Amber phosphor on true
# black, saturated up/down, a cyan interaction accent. Bright enough to read at
# a glance across a wide terminal; changing a role's colour means changing it
# here only.
# ---------------------------------------------------------------------------
BG        = "#03060b"   # canvas — near-black
BG_PANEL  = "#070c13"   # side rails / raised panels
BG_RAISED = "#0f1926"   # table headers, dialogs, input wells
SEL_BG    = "#14273b"   # selected row / highlighted list item
BORDER    = "#1d2b3b"   # quiet dividers
BORDER_HI = "#3a5069"   # active dividers / focus
TEXT      = "#cdd9e6"   # default body text (cool white)
TEXT_HI   = "#f6fafe"   # bright emphasis
MUTED     = "#8797a8"   # secondary text
DIM       = "#586777"   # tertiary / captions
AMBER     = "#ffb020"   # primary Bloomberg amber (accent, titles bar)
AMBER_HI  = "#ffcf66"   # bright amber (section titles, focus)
GOLD      = "#eb9a2e"   # secondary amber/orange (queued, warnings)
UP        = "#1fe07b"   # positive / done
DOWN      = "#ff5257"   # negative / failed
CYAN      = "#38ccff"   # interactive accent / working
_VIEWS = ("desk", "market", "workforce", "research", "audit")
_AGENT_NAMES = (
    "moments-analyst", "challenger", "optimization-runner", "referee", "reporter",
)
# The governed pipeline as a flowchart: (phase, agent, short label). The phase
# matches the durable workflow_steps.phase; the agent matches the spawned role.
_FLOW = (
    ("analyst", "moments-analyst", "analyst"),
    ("challenger", "challenger", "challenger"),
    ("optimizer", "optimization-runner", "optimizer"),
    ("referee", "referee", "referee"),
    ("reporter", "reporter", "reporter"),
)
_AGENT_TO_PHASE = {agent: phase for phase, agent, _ in _FLOW}
_PHASE_SHORT = {phase: short for phase, _, short in _FLOW}
# What each phase contributes, in one clause — the "what just happened" half of
# the per-phase console note. Mirrors qlab.state.registry's dependency DAG.
_PHASE_DID = {
    "analyst": "chose the estimation window, shrinkage, and regime call, and "
               "logged that judgment",
    "challenger": "argued the opposing case and attached it to the decision",
    "optimizer": "ran the cataloged operational algorithm and produced target "
                 "weights",
    "referee": "independently checked constraints, benchmarks, and the "
               "target binding",
    "reporter": "compiled the human-facing recommendation",
}
# One state → (glyph, colour) table shared by the flowchart and the agent rail.
_STATE_STYLE = {
    "working": ("●", CYAN),
    "queued": ("◐", GOLD),
    "waiting": ("◐", GOLD),
    "done": ("✓", UP),
    "failed": ("×", DOWN),
    "blocked": ("!", AMBER),
    "idle": ("◌", DIM),
}
_PULSE_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
# Bus events that mean durable state changed and a full refresh is worth it.
_REFRESH_EVENT_KINDS = {
    "workflow_started", "workflow_phase", "referee_verdict",
    "plan_built", "order_filled", "decision_logged", "ablation_complete",
}


def workforce_note(phase: str, status: str, summary: str,
                   done_phases: set[str]) -> tuple[str, str]:
    """The two-line note printed when one agent finishes: done, then next.

    Pure so the wording is testable without a running app. ``done_phases``
    is the set of phases already complete *including* this one; the follow-up
    line is derived from the same dependency graph the registry enforces, so
    the operator is never told a stage is next that the gate would refuse.
    """
    short = _PHASE_SHORT.get(phase, phase)
    detail = " ".join(str(summary or "").split())[:220]
    did = _PHASE_DID.get(phase, "completed its phase")

    if status == "failed":
        return (f"{short} failed — {detail or 'no reason recorded'}",
                "The run stops here; durable phase state is kept, so : workforce "
                "resume ID continues it once the cause is fixed.")
    if status == "blocked":
        return (f"{short} blocked — {detail or 'a governance gate was not met'}",
                "A hard gate refused, so nothing downstream may proceed. Nothing "
                "was traded; this is the gate working.")

    # The worker's own summary is the specific account; the role clause is only
    # the fallback, so the note never repeats what the summary already says.
    head = f"{short} done — {detail or did}"
    if phase == "analyst":
        nxt = ("Next: the challenger and the optimizer both start now, in "
               "parallel — neither depends on the other, only on this "
               "estimation call.")
    elif phase in ("challenger", "optimizer"):
        other = "optimizer" if phase == "challenger" else "challenger"
        nxt = (
            "Next: the referee gate, now that both parallel phases are in."
            if other in done_phases else
            f"Next: waiting on the {other} (running in parallel), then the "
            "referee gate."
        )
    elif phase == "referee":
        nxt = ("Next: the reporter compiles the recommendation. A PASS is bound "
               "to these exact weights; execution still needs you.")
    else:
        nxt = ("Run complete — the results print below. Any paper trade remains "
               "a separate, explicitly confirmed action.")
    return head, nxt


# The regime a run selected is a first-class read for the operator, so it gets
# its own colored line rather than living only in a hover card.
# The analyst's five-level regime ladder, most to least stressed, as a red→cyan
# heat scale. Kept in sync with qlab.tui.claude._ANALYST_REGIMES.
_REGIME_TONE = {
    "crisis": "#ff3b47",
    "stress": "#ff8c42",
    "neutral": "#e6c84f",
    "calm": "#1fe07b",
    "expansion": "#38ccff",
}


def _regime_readout(steps_by_phase: dict) -> tuple[str, str, str] | None:
    """The analyst's regime, its one-line reasoning, and the news summary, or None.

    Sourced from the durable analyst-phase artifacts the agent persists, so the
    operator sees the exact call the run made rather than a re-derived one.
    """
    artifacts = (steps_by_phase.get("analyst", {}) or {}).get("artifacts") or {}
    regime = str(artifacts.get("regime") or "").strip()
    if not regime:
        return None
    reasoning = " ".join(str(artifacts.get("regime_reasoning") or "").split())
    summary = " ".join(str(artifacts.get("regime_summary") or "").split())
    return regime, reasoning, summary


def _regime_line(steps_by_phase: dict, *, indent: str = "") -> str | None:
    """Rich-markup regime block: the coloured call, its reasoning, and the 1-3
    line news backdrop that informed it (from market_news), or None."""
    readout = _regime_readout(steps_by_phase)
    if readout is None:
        return None
    regime, reasoning, summary = readout
    tone = _REGIME_TONE.get(regime.lower(), "#ffb020")
    line = f"{indent}[#38ccff]◆ REGIME[/]  [bold {tone}]{escape(regime.upper())}[/]"
    if reasoning:
        line += f"  [#8797a8]{escape(reasoning[:200])}[/]"
    if summary:
        line += (f"\n{indent}  [#9a7f4a]news backdrop[/]  "
                 f"[#cdd9e6]{escape(summary[:320])}[/]")
    return line


def _extract_targets(steps_by_phase: dict) -> dict:
    """The reviewed target weights: the referee's if it re-published them (they
    are then gate-bound), otherwise the optimizer's. Empty when neither ran."""
    for phase in ("referee", "optimizer"):
        targets = (steps_by_phase.get(phase, {}).get("artifacts") or {}).get("targets")
        if isinstance(targets, dict) and targets:
            return targets
    return {}


def _format_targets(targets: dict, limit: int = 8) -> str:
    """Target weights as a compact, largest-first line: 'AAPL 30.0% · GLD 20.0%'."""
    ordered = sorted(targets.items(), key=lambda kv: -float(kv[1]))[:limit]
    return " · ".join(f"{ticker} {float(weight):.1%}" for ticker, weight in ordered)


# The completion summary is written for an operator, not a quant: a coloured
# banner, one friendly headline, the regime, a plain line per agent, the
# recommendation, and what it means. Everything below is pure so the wording is
# unit-tested without a running app, and built only from the durable record.
def _result_banner(status: str) -> tuple[str, str]:
    """The coloured headline banner for the run's terminal state."""
    return {
        "complete": ("#1fe07b", "WORKFORCE COMPLETE"),
        "blocked": ("#ffb020", "STOPPED AT A SAFETY GATE"),
        "failed": ("#ff5257", "STOPPED ON AN ERROR"),
    }.get(status, ("#eb9a2e", "ENDED EARLY"))


def _result_headline(status: str, verdict: str) -> str:
    """One friendly sentence: what the run achieved, read first."""
    if status == "complete":
        if verdict.upper() == "PASS":
            return ("The desk reviewed the portfolio and has a referee-approved "
                    "recommendation ready for you.")
        return "The desk finished its review — the recommendation is below."
    if status == "blocked":
        return "A safety gate stopped the run before any trade. Nothing was traded."
    if status == "failed":
        return "The run hit an error before finishing. Its completed steps are saved."
    return "The run ended before completing. The steps it reached are below."


# What each role does, in words an operator reads at a glance. The completion
# summary leads with these and enriches only from the durable fact each phase
# owns — never the raw agent summary, which is written for the audit trail.
_PHASE_PERSON = {
    "analyst": ("Analyst",
                "read the market regime and set the estimate window and shrinkage"),
    "challenger": ("Challenger",
                   "argued the opposite case to keep that call honest"),
    "optimizer": ("Optimizer", "computed the target allocation weights"),
    "referee": ("Referee",
                "independently re-checked the result against the mandate"),
    "reporter": ("Reporter", "wrote the recommendation for you"),
}


def _agent_brief(phase: str, step: dict | None,
                 run_status: str) -> tuple[str, str, str, str]:
    """A friendly ``(glyph, colour, name, action)`` line for one phase.

    Plain language, enriched with the single durable fact that phase owns (the
    algorithm, the verdict, whether a plan was prepared) and, only when it broke,
    a short cleaned reason — so the reader learns what happened and where.
    """
    name, action = _PHASE_PERSON.get(phase, (phase.title(), "ran its phase"))
    state = str((step or {}).get("status", "idle"))
    glyph, colour = _STATE_STYLE.get(state, ("◌", "#586777"))
    artifacts = (step or {}).get("artifacts") or {}

    if not step or state in ("idle", "queued", "waiting"):
        return "◌", "#586777", name, "did not run"
    if state == "working":
        return glyph, colour, name, "was still running when the run stopped"
    if state in ("failed", "blocked"):
        reason = clean_report_line(
            " ".join(str(step.get("summary") or "").split()))[1][:90]
        base = ("hit an error and stopped the run" if state == "failed"
                else "was refused by a safety gate")
        return glyph, colour, name, base + (f" — {reason}" if reason else "")

    if phase == "optimizer":
        algo = str(artifacts.get("algorithm_id")
                   or artifacts.get("algorithm") or "").strip()
        if algo:
            action = f"computed the target weights using {algo}"
    elif phase == "referee":
        result = str(artifacts.get("verdict") or "").upper()
        if result == "PASS":
            action = "re-checked the result against the mandate and approved it"
        elif result:
            action = ("re-checked the result against the mandate and did not "
                      "approve a trade")
    elif phase == "reporter" and artifacts.get("plan_id"):
        action = "wrote the recommendation and prepared a paper trade to confirm"
    return glyph, colour, name, action


def _result_meaning(status: str, verdict: str, has_targets: bool,
                    has_plan: bool) -> str:
    """One or two sentences on what the outcome signifies for the operator."""
    if status == "complete" and verdict.upper() == "PASS":
        if has_plan:
            return ("These weights passed every mandate check. Nothing has traded "
                    "yet — confirm the paper trade yourself to act on them.")
        return ("These weights passed every mandate check, but no paper trade was "
                "prepared, so nothing has traded.")
    if status == "complete":
        return ("The review finished without an approved trade, so the result "
                "above is for your consideration only — nothing has traded.")
    if status == "blocked":
        return ("A hard safety limit was hit, so the run stopped on purpose before "
                "proposing a trade. This is the guardrail working as intended.")
    if status == "failed":
        return "Nothing was traded. You can resume the run once the cause is fixed."
    return "Nothing was traded. You can resume the run to finish it."


def _verdict_cell(verdict: dict | None) -> str:
    """Compact referee token for the audit table: 'PASS·source' or '—'."""
    if not verdict:
        return "—"
    label = str(verdict.get("verdict", "")).upper() or "—"
    source = str(verdict.get("source", "")).strip()
    return f"{label}·{source}" if source else label


def _reflection_cell(reflection: str | None) -> str:
    """First ~50 chars of a decision's reflection, 'pending' when unresolved."""
    text = str(reflection or "").strip()
    return text[:50] if text else "pending"


class FlowNode(Static):
    """One agent in the workforce flowchart.

    The node shows the phase's live state on its face; hovering reveals the
    full current update (summary, elapsed, artifacts) both as a native tooltip
    and expanded in the work rail. The flowchart replaces the streamed Claude
    narrative — no block of coordinator text is ever dumped into the console.
    """

    def __init__(self, phase: str, agent: str, short: str):
        super().__init__(id=f"flow-{phase}", markup=True)
        self.phase = phase
        self.agent = agent
        self.short = short

    def on_enter(self, event: events.Enter) -> None:
        app = self.app
        detail = getattr(app, "_flow_details", {}).get(self.phase) or (
            f"{self.agent}\n\nnot yet started")
        app._set_selected_work(f"{self.short.upper()} · {self.agent}\n\n{detail}")


class NavMenu(Static):
    """The 1–5 view switcher in the spine.

    It renders one text line per view (in ``_VIEWS`` order), so a click selects
    the view on the clicked row — the click's y within the widget *is* the row
    index. Keyboard 1–5 still work; this only adds the mouse path a Static lacks.
    """

    def on_click(self, event: events.Click) -> None:
        index = int(event.y)
        if 0 <= index < len(_VIEWS):
            self.app.action_view(_VIEWS[index])


class PaperConfirmScreen(ModalScreen[bool]):
    """Explicit confirmation for the only mutating action exposed by v1."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    CSS = """
    Screen {
        layout: vertical;
        background: #03060b;
        color: #cdd9e6;
    }

    #workspace {
        height: 1fr;
    }

    #spine {
        width: 24;
        min-width: 16;
        padding: 1 1 0 1;
        background: #070c13;
        border-right: solid #1d2b3b;
    }
    #wordmark {
        height: 3;
        color: #ffb020;
        text-style: bold;
    }
    #nav {
        height: 7;
        color: #8797a8;
    }
    #universe-label {
        height: 2;
        margin-top: 1;
        color: #586777;
        text-style: bold;
    }
    #universe {
        height: 1fr;
        background: transparent;
        border: none;
        scrollbar-size: 0 0;
    }
    #universe ListItem {
        height: 1;
        padding: 0;
        color: #8797a8;
    }
    #universe ListItem.-highlight {
        background: #14273b;
        color: #ffcf66;
        text-style: bold;
    }

    #canvas {
        width: 1fr;
        height: 1fr;
        padding: 1 2;
        background: #03060b;
    }
    .canvas-view {
        width: 1fr;
        height: 1fr;
        overflow-y: auto;
        scrollbar-size: 1 1;
    }
    .canvas-title {
        height: 1;
        color: #ffcf66;
        text-style: bold;
        background: #0f1926;
    }
    #desk-content, #market-content {
        height: 1fr;
    }
    #workforce-content {
        height: auto;
        max-height: 32%;
        padding: 1 2;
        margin-top: 1;
        background: #070c13;
        border: round #1d2b3b;
    }
    #flow-row {
        height: 6;
        margin-top: 1;
        align: left middle;
        overflow-x: auto;
        overflow-y: hidden;
        scrollbar-size: 1 0;
    }
    FlowNode {
        width: 13;
        height: 4;
        padding: 0;
        border: round #1d2b3b;
        color: #8797a8;
        content-align: center middle;
        text-align: center;
    }
    FlowNode.-working {
        border: round #38ccff;
        background: #0a2233;
        color: #d7f4ff;
        text-style: bold;
    }
    FlowNode.-queued { border: round #5a4420; color: #ffcf66; }
    FlowNode.-done { border: round #1f6a44; background: #08160f; color: #7cf0b4; }
    FlowNode.-failed { border: round #6a2325; background: #1a0a0b; color: #ff9c9e; }
    FlowNode.-blocked { border: round #6a4c18; color: #ffd98a; }
    .flow-arrow {
        width: 3;
        height: 4;
        content-align: center middle;
        text-align: center;
        color: #586777;
    }
    #workforce-console {
        height: 1fr;
        margin-top: 1;
        padding: 0 1;
        background: transparent;
        border: none;
        border-top: solid #1d2b3b;
        color: #cdd9e6;
        scrollbar-size: 1 1;
    }
    #chat-row {
        height: 3;
        margin-top: 1;
    }
    #chat-mode {
        width: 6;
        height: 3;
        padding: 1 0 0 1;
    }
    #chat-input {
        width: 1fr;
        height: 3;
        border: round #1d2b3b;
        padding: 0 1;
        background: #070c13;
        color: #f6fafe;
    }
    #chat-input:focus {
        border: round #ffb020;
    }
    #chat-input:disabled {
        border: round #16212e;
        background: #060a10;
        color: #55657a;
    }
    #chat-exit {
        width: 10;
        height: 3;
        min-width: 8;
        margin-left: 1;
        background: #0f1926;
        color: #cdd9e6;
        border: round #1d2b3b;
    }
    #chat-exit:hover {
        background: #14273b;
        color: #f6fafe;
    }
    #research-summary, #audit-summary {
        height: 6;
        color: #cdd9e6;
    }
    #runs-table, #audit-table {
        height: 1fr;
        background: transparent;
        border: none;
        scrollbar-size: 1 1;
    }
    DataTable > .datatable--header {
        background: #0f1926;
        color: #ffb020;
        text-style: bold;
    }
    DataTable > .datatable--cursor {
        background: #14273b;
        color: #ffcf66;
        text-style: bold;
    }

    #agent-rail {
        width: 40;
        min-width: 30;
        padding: 1 1 0 2;
        background: #070c13;
        border-left: solid #1d2b3b;
    }
    #agent-label, #work-label {
        height: 2;
        color: #586777;
        text-style: bold;
    }
    #agent-list {
        height: 16;
    }
    #work-label {
        margin-top: 1;
    }
    #selected-work {
        height: 1fr;
        color: #cdd9e6;
        overflow-y: auto;
        scrollbar-size: 1 1;
    }

    #timeline {
        height: 12;
        display: none;
        padding: 0 2;
        background: #070c13;
        border-top: solid #1d2b3b;
        color: #8797a8;
        scrollbar-size: 1 1;
    }
    #event-strip {
        height: 1;
        padding: 0 1;
        background: #0f1926;
        color: #8797a8;
    }
    #command-row {
        height: 2;
        padding: 0 1;
        background: #070c13;
    }
    #command {
        width: 1fr;
        height: 1;
        border: none;
        padding: 0;
        background: transparent;
        color: #f6fafe;
    }
    #command:focus {
        border: none;
    }
    #system-status {
        width: auto;
        min-width: 28;
        height: 1;
        text-align: right;
        color: #8797a8;
    }

    /* Responsive: wider terminals give the side rails more room; narrower ones
       shed the agent rail first, then compress the spine. Charts live in the
       center canvas, which is always 1fr, so every extra column widens them. */
    Screen.wide #spine { width: 30; }
    Screen.wide #agent-rail { width: 48; }
    Screen.wide #agent-list { height: 18; }

    Screen.compact #spine {
        width: 20;
    }
    Screen.compact #agent-rail {
        width: 32;
        padding-left: 1;
    }
    Screen.narrow #agent-rail {
        display: none;
    }
    Screen.narrow #spine {
        width: 17;
    }
    Screen.narrow.agent-focus #spine,
    Screen.narrow.agent-focus #canvas {
        display: none;
    }
    Screen.narrow.agent-focus #agent-rail {
        display: block;
        width: 1fr;
        border-left: none;
        padding-left: 2;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    CSS = """
    PaperConfirmScreen {
        align: center middle;
        background: #03060bcc;
    }
    #paper-dialog {
        width: 68;
        height: auto;
        padding: 2 3;
        background: #0f1926;
        border: solid #eb9a2e;
    }
    #paper-dialog-title {
        color: #f6fafe;
        text-style: bold;
        margin-bottom: 1;
    }
    #paper-dialog-copy {
        color: #cdd9e6;
        margin-bottom: 2;
    }
    #paper-dialog-actions {
        height: 3;
        align-horizontal: right;
    }
    #paper-dialog-actions Button {
        margin-left: 1;
    }
    """

    def __init__(self, plan_id: str = ""):
        super().__init__()
        self.plan_id = plan_id

    def compose(self) -> ComposeResult:
        with Vertical(id="paper-dialog"):
            yield Static("EXECUTE PAPER REBALANCE", id="paper-dialog-title")
            yield Static(
                f"Plan {self.plan_id or '—'}. Paper capital only. The deterministic mandate, decision-bound "
                "referee PASS, reconciliation, and idempotent order path are "
                "enforced. Human confirmation and the full action trail remain.",
                id="paper-dialog-copy",
            )
            with Horizontal(id="paper-dialog-actions"):
                yield Button("Cancel", id="cancel-paper")
                yield Button("Execute paper", id="confirm-paper", variant="warning")

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-paper")


class ClaudeWorkforceScreen(ModalScreen[bool]):
    """One quiet startup choice: launch the constrained Claude workforce or wait."""

    BINDINGS = [Binding("escape", "later", "Later", show=False)]

    CSS = """
    ClaudeWorkforceScreen {
        align: center middle;
        background: #03060bcc;
    }
    #workforce-dialog {
        width: 72;
        height: auto;
        padding: 2 3;
        background: #0f1926;
        border: solid #3a5069;
    }
    #workforce-dialog-title {
        color: #f6fafe;
        text-style: bold;
        margin-bottom: 1;
    }
    #workforce-dialog-copy {
        color: #cdd9e6;
        margin-bottom: 2;
    }
    #workforce-dialog-actions {
        height: 3;
        align-horizontal: right;
    }
    #workforce-dialog-actions Button {
        margin-left: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="workforce-dialog"):
            yield Static("START CLAUDE WORKFORCE?", id="workforce-dialog-title")
            yield Static(
                "qlab will launch the Claude CLI as a constrained coordinator. "
                "It deploys the analyst, challenger, optimizer, referee, and "
                "reporter through the owner-backed MCP proxy and runs the pipeline "
                "autonomously — no mid-run questions. Progress shows on the "
                "flowchart (hover a node for detail); it receives no code, shell, "
                "filesystem, or paper-execution tools.",
                id="workforce-dialog-copy",
            )
            with Horizontal(id="workforce-dialog-actions"):
                yield Button("Later", id="workforce-later")
                yield Button("Start workforce", id="workforce-start", variant="primary")

    def action_later(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "workforce-start")


class QlabTui(App[None]):
    """Border-light terminal workspace for portfolio and agent operations."""

    TITLE = "qlab operator"

    CSS = """
    Screen {
        layout: vertical;
        background: #03060b;
        color: #cdd9e6;
    }

    #workspace {
        height: 1fr;
    }

    #spine {
        width: 24;
        min-width: 18;
        padding: 1 1 0 1;
        background: #070c13;
        border-right: solid #1d2b3b;
    }
    #wordmark {
        height: 3;
        color: #f6fafe;
        text-style: bold;
    }
    #nav {
        height: 7;
        color: #8797a8;
    }
    #universe-label {
        height: 2;
        margin-top: 1;
        color: #9a7f4a;
    }
    #universe {
        height: 1fr;
        background: transparent;
        border: none;
        scrollbar-size: 0 0;
    }
    #universe ListItem {
        height: 1;
        padding: 0;
        color: #8797a8;
    }
    #universe ListItem.-highlight {
        background: #14273b;
        color: #f6fafe;
        text-style: bold;
    }

    #canvas {
        width: 1fr;
        height: 1fr;
        padding: 1 2;
        background: #03060b;
    }
    .canvas-view {
        width: 1fr;
        height: 1fr;
        overflow-y: auto;
        scrollbar-size: 1 1;
    }
    .canvas-title {
        height: 2;
        color: #ffcf66;
        text-style: bold;
    }
    #desk-content, #market-content {
        height: 1fr;
    }
    #workforce-content {
        height: auto;
        max-height: 30%;
        padding: 1 2;
        background: #070c13;
        border: round #1d2b3b;
    }
    #flow-row {
        height: 6;
        margin-top: 1;
        align: left middle;
        overflow-x: auto;
        overflow-y: hidden;
        scrollbar-size: 1 0;
    }
    FlowNode {
        width: 12;
        height: 4;
        padding: 0;
        border: round #1d2b3b;
        color: #8797a8;
        content-align: center middle;
        text-align: center;
    }
    FlowNode.-working {
        border: round #38ccff;
        background: #0a2233;
        color: #f6fafe;
        text-style: bold;
    }
    FlowNode.-queued { border: round #6b5836; color: #bda879; }
    FlowNode.-done { border: round #3f6b53; color: #bfe0cd; }
    FlowNode.-failed { border: round #7d3a3a; color: #edb6b6; }
    FlowNode.-blocked { border: round #8a6a2f; color: #ecd4a5; }
    .flow-arrow {
        width: 3;
        height: 4;
        content-align: center middle;
        text-align: center;
        color: #586777;
    }
    #workforce-console {
        height: 1fr;
        margin-top: 1;
        padding: 0 1;
        background: transparent;
        border: none;
        border-top: solid #1d2b3b;
        color: #cdd9e6;
        scrollbar-size: 1 1;
    }
    #chat-row {
        height: 3;
        margin-top: 1;
    }
    #chat-mode {
        width: 6;
        height: 3;
        padding: 1 0 0 1;
    }
    #chat-input {
        width: 1fr;
        height: 3;
        border: round #1d2b3b;
        padding: 0 1;
        background: #070c13;
        color: #f6fafe;
    }
    #chat-input:focus {
        border: round #ffb020;
    }
    #chat-input:disabled {
        border: round #16212e;
        background: #060a10;
        color: #55657a;
    }
    #chat-exit {
        width: 10;
        height: 3;
        min-width: 8;
        margin-left: 1;
        background: #0f1926;
        color: #cdd9e6;
        border: round #1d2b3b;
    }
    #chat-exit:hover {
        background: #1d2b3b;
        color: #f6fafe;
    }
    #research-summary, #audit-summary {
        height: 7;
        color: #cdd9e6;
    }
    #runs-table, #audit-table {
        height: 1fr;
        background: transparent;
        border: none;
        scrollbar-size: 1 1;
    }
    DataTable > .datatable--header {
        background: #0f1926;
        color: #8797a8;
        text-style: none;
    }
    DataTable > .datatable--cursor {
        background: #14273b;
        color: #f6fafe;
    }

    #agent-rail {
        width: 38;
        min-width: 31;
        padding: 1 1 0 2;
        background: #070c13;
        border-left: solid #1d2b3b;
    }
    #agent-label, #work-label {
        height: 2;
        color: #9a7f4a;
    }
    #agent-list {
        height: 16;
    }
    #work-label {
        margin-top: 1;
    }
    #selected-work {
        height: 1fr;
        color: #cdd9e6;
        overflow-y: auto;
        scrollbar-size: 1 1;
    }

    #timeline {
        height: 10;
        display: none;
        padding: 0 2;
        background: #070c13;
        border-top: solid #1d2b3b;
        color: #8797a8;
        scrollbar-size: 1 1;
    }
    #event-strip {
        height: 1;
        padding: 0 1;
        background: #0f1926;
        color: #8797a8;
    }
    #command-row {
        height: 2;
        padding: 0 1;
        background: #070c13;
    }
    #command {
        width: 1fr;
        height: 1;
        border: none;
        padding: 0;
        background: transparent;
        color: #f6fafe;
    }
    #command:focus {
        border: none;
    }
    #system-status {
        width: auto;
        min-width: 28;
        height: 1;
        text-align: right;
        color: #8797a8;
    }

    Screen.compact #spine {
        width: 19;
    }
    Screen.compact #agent-rail {
        width: 31;
        padding-left: 1;
    }
    Screen.narrow #agent-rail {
        display: none;
    }
    Screen.narrow #spine {
        width: 18;
    }
    Screen.narrow.agent-focus #spine,
    Screen.narrow.agent-focus #canvas {
        display: none;
    }
    Screen.narrow.agent-focus #agent-rail {
        display: block;
        width: 1fr;
        border-left: none;
        padding-left: 2;
    }
    """

    BINDINGS = [
        Binding("1", "view('desk')", "Desk", show=False),
        Binding("2", "view('market')", "Market", show=False),
        Binding("3", "view('workforce')", "Workforce", show=False),
        Binding("4", "view('research')", "Research", show=False),
        Binding("5", "view('audit')", "Audit", show=False),
        Binding("6", "agent_focus", "Agents", show=False),
        Binding("j", "next_symbol", "Next symbol", show=False),
        Binding("k", "previous_symbol", "Previous symbol", show=False),
        Binding("colon", "command", "Command", show=False),
        Binding("ctrl+p", "command", "Command", show=False),
        Binding("tilde", "timeline", "Timeline", show=False),
        Binding("escape", "escape", "Back", show=False),
        Binding("ctrl+q", "quit", "Quit", show=False),
    ]

    def __init__(
        self,
        client,
        *,
        offline: bool = True,
        refresh_interval: float = 2.0,
        owned_server: subprocess.Popen | None = None,
        claude_start: str = "offer",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.client = client
        self.offline = offline
        self.refresh_interval = refresh_interval
        self.owned_server = owned_server
        self.claude_start = claude_start
        self.active_view = "desk"
        # Placeholder until the first snapshot; the owner's mandate universe
        # (market.assets) replaces it so a config change never desyncs the TUI.
        self.universe_tickers: list[str] = list(_DEFAULT_TICKERS)
        self.active_ticker = self.universe_tickers[0]
        self.snapshot: dict[str, Any] = {}
        self._refreshing = False
        self._action_running = False
        self._event_ids: set[str] = set()
        self._runs_signature: tuple = ()
        self._audit_signature: tuple = ()
        self._audit_decisions: dict[str, dict] = {}
        self._claude_buffer = ""
        self._claude_saw_delta = False
        self._claude_offer_handled = False
        self._pending_plan_id = ""
        self._agent_focus = False
        self._agent_states: dict[str, str] = {}
        # Flowchart state: phase -> state token, and phase -> hover detail.
        self._flow_states: dict[str, str] = {}
        self._flow_details: dict[str, str] = {}
        # Which durable run the flowchart is bound to. A new run clears this and
        # sets _pending_workflow, so the chart never keeps painting the previous
        # run's outcome in the seconds before the coordinator calls workflow.start.
        self._active_workflow_id = ""
        self._pending_workflow = False
        self._seen_workflow_ids: set[str] = set()
        self._phase_reported: dict[str, str] = {}
        self._results_printed = False
        self._pulse = 0
        self._live_stream_stop = False
        self._console_partial = ""
        self._chat_sessions = {"workforce": "", "chat": ""}
        self._chat_mode = "workforce"
        self.claude = ClaudeSession(
            self._receive_claude_event,
            cwd=_WORKSPACE_ROOT,
            runtime_url=getattr(client, "base_url", "http://127.0.0.1:8765"),
            offline=offline,
        )

    def compose(self) -> ComposeResult:
        with Horizontal(id="workspace"):
            with Vertical(id="spine"):
                yield Static("qlab\n[dim]operator console[/]", id="wordmark", markup=True)
                yield NavMenu(id="nav", markup=True)
                yield Static("UNIVERSE", id="universe-label")
                yield ListView(
                    *(ListItem(Label(ticker)) for ticker in _DEFAULT_TICKERS),
                    id="universe",
                )

            with ContentSwitcher(initial="desk", id="canvas"):
                with Vertical(id="desk", classes="canvas-view"):
                    yield Static("[#ffb020]\u258d[/] DESK", classes="canvas-title", markup=True)
                    yield Static(id="desk-content", markup=True)
                with Vertical(id="market", classes="canvas-view"):
                    yield Static("[#ffb020]\u258d[/] MARKET", classes="canvas-title", markup=True)
                    yield Static(id="market-content", markup=True)
                with Vertical(id="workforce", classes="canvas-view"):
                    yield Static("[#ffb020]\u258d[/] WORKFORCE", classes="canvas-title", markup=True)
                    yield Static(id="workforce-content", markup=True)
                    with Horizontal(id="flow-row"):
                        for _index, (_phase, _agent, _short) in enumerate(_FLOW):
                            if _index:
                                yield Static("\u2192", classes="flow-arrow")
                            yield FlowNode(_phase, _agent, _short)
                    yield RichLog(id="workforce-console", wrap=True,
                                  markup=True, max_lines=400)
                    with Horizontal(id="chat-row"):
                        yield Static(id="chat-mode", markup=True)
                        yield Input(
                            placeholder="message the coordinator — Enter sends",
                            id="chat-input")
                        yield Button("exit", id="chat-exit")
                with Vertical(id="research", classes="canvas-view"):
                    yield Static("[#ffb020]\u258d[/] RESEARCH", classes="canvas-title", markup=True)
                    yield Static(id="research-summary", markup=True)
                    yield DataTable(id="runs-table", cursor_type="row")
                with Vertical(id="audit", classes="canvas-view"):
                    yield Static("[#ffb020]\u258d[/] AUDIT", classes="canvas-title", markup=True)
                    yield Static(id="audit-summary", markup=True)
                    yield DataTable(id="audit-table", cursor_type="row")

            with Vertical(id="agent-rail"):
                yield Static("AGENTS", id="agent-label")
                yield Static(id="agent-list", markup=True)
                yield Static("SELECTED WORK", id="work-label")
                yield Static(
                    "No active workforce.\n\nUse [bold]: workforce GOAL[/] to let "
                    "Claude coordinate the five governed qlab roles.",
                    id="selected-work",
                    markup=True,
                )

        yield RichLog(id="timeline", wrap=True, markup=False, max_lines=500)
        yield Static("waiting for runtime snapshot", id="event-strip")
        with Horizontal(id="command-row"):
            yield Input(placeholder=": command or Ctrl-P", id="command")
            yield Static("PAPER · CONNECTING", id="system-status")

    def on_mount(self) -> None:
        self.query_one("#runs-table", DataTable).add_columns("run", "kind", "created")
        self.query_one("#audit-table", DataTable).add_columns(
            "time", "object", "state", "verdict", "reflection", "detail")
        universe = self.query_one("#universe", ListView)
        universe.index = 0
        self._console_write(
            "[#9a7f4a]workforce — type a goal below and the coordinator runs the "
            "five governed roles autonomously. Watch the flowchart above; hover a "
            "node for its live update. [bold]■ stop[/] interrupts; durable state "
            "survives.[/]")
        self._render_chat_mode()
        self._render_flow()
        self.query_one("#audit-table", DataTable).zebra_stripes = True
        self.query_one("#runs-table", DataTable).zebra_stripes = True
        self._render_nav()
        self._render_agents()
        self._start_refresh()
        if self.refresh_interval > 0:
            self.set_interval(self.refresh_interval, self._start_refresh)
            self.set_interval(0.25, self._tick_pulse)
        self._start_live_stream()

    def on_unmount(self) -> None:
        self._live_stream_stop = True
        self.claude.stop()
        if self.owned_server is not None and self.owned_server.poll() is None:
            self.owned_server.terminate()

    def on_resize(self, event: events.Resize) -> None:
        width = event.size.width
        # Four responsive tiers. Charts live in the always-1fr center canvas, so
        # each tier's job is only to size the side rails; wider terminals hand
        # the extra columns straight to the plots.
        self.screen.set_class(width >= 200, "wide")
        self.screen.set_class(110 <= width < 150, "compact")
        self.screen.set_class(width < 110, "narrow")
        if width >= 110 and self._agent_focus:
            self._agent_focus = False
            self.screen.remove_class("agent-focus")
        # A resize changes how much room a chart has; repaint the view that owns
        # one so it fills the new space instead of waiting for the next poll.
        if self.snapshot and self.active_view in ("market", "desk"):
            self._render_market() if self.active_view == "market" else self._render_desk()

    # -- snapshot refresh -------------------------------------------------
    def _start_refresh(self) -> None:
        # A running owner action holds the owner's dispatch lock; polling
        # /api/tui behind it would only pile up timeouts in the event strip.
        if self._refreshing or self._action_running:
            return
        self._refreshing = True

        def run() -> None:
            try:
                snapshot = gather_snapshot(self.client, offline=self.offline)
                self.call_from_thread(self._apply_snapshot, snapshot)
            except Exception as exc:
                self.call_from_thread(
                    self._write_local_event, "api.error", {"error": repr(exc)})
            finally:
                self.call_from_thread(self._finish_refresh)

        threading.Thread(target=run, daemon=True).start()

    def _finish_refresh(self) -> None:
        self._refreshing = False

    def _start_live_stream(self) -> None:
        """Subscribe to the owner's SSE bus so state changes land instantly.

        Polling stays on as the fallback; this only makes the desk react the
        moment a phase flips or a verdict lands instead of at the next tick.
        """
        if not hasattr(self.client, "stream"):
            return

        def run() -> None:
            import time

            while not self._live_stream_stop:
                try:
                    for event in self.client.stream("/api/stream"):
                        if self._live_stream_stop:
                            return
                        self.call_from_thread(self._apply_live_event, event)
                except Exception:
                    pass  # owner restarting or unreachable; retry quietly
                time.sleep(2.0)

        threading.Thread(target=run, daemon=True).start()

    def _apply_live_event(self, event: dict) -> None:
        # Console notes are raised by _ingest_events, which both the SSE stream
        # and the snapshot poll feed — whichever delivers a phase event first
        # writes its note, and the other is deduped by id.
        self._ingest_events([event])
        if str(event.get("kind", "")) in _REFRESH_EVENT_KINDS:
            self._start_refresh()

    # -- workforce console -------------------------------------------------
    def _console_write(self, line: str) -> None:
        self.query_one("#workforce-console", RichLog).write(line)

    def _render_chat_mode(self) -> None:
        chip = self.query_one("#chat-mode", Static)
        if self._chat_mode == "chat":
            chip.update("[bold #38ccff]CHAT[/]")
        else:
            chip.update("[bold #ffb020]WORK[/]")
        self._sync_chat_input()

    def _sync_chat_input(self) -> None:
        """Lock the coordinator input while a session is running, reopen when done.

        A running turn already refuses a new message (_chat_send returns early),
        so the box would only mislead — the operator could type into it and see
        nothing sent. Disabling it makes the state visible: it greys out during a
        run and reopens, with the mode's own prompt, the moment the turn ends and
        a message can actually be sent.
        """
        field = self.query_one("#chat-input", Input)
        running = self.claude.running
        field.disabled = running
        if running:
            field.placeholder = "agents are working — input reopens when the run ends"
        elif self._chat_mode == "chat":
            field.placeholder = "ask qlab anything — read-only · : workforce to switch"
        else:
            field.placeholder = "message the coordinator — Enter sends · : chat to switch"

    def _note_workflow_phase(self, payload: dict) -> None:
        """One short paragraph per completed agent: what happened, what's next.

        Driven by the owner's durable phase events rather than Claude's
        narrative, so the operator's running account of the pipeline is the
        same record the registry keeps.
        """
        phase = str(payload.get("phase") or "")
        status = str(payload.get("status") or "")
        if phase not in _PHASE_SHORT:
            return
        # Only the run this session launched narrates itself. Startup replays a
        # window of history, and a resumed desk may see another client's run;
        # neither should scroll past as if it were happening now.
        workflow_id = str(payload.get("workflow_id") or "")
        if self._active_workflow_id:
            if workflow_id != self._active_workflow_id:
                return
        elif not self._pending_workflow or workflow_id in self._seen_workflow_ids:
            return
        if self._phase_reported.get(phase) == status:
            return
        self._phase_reported[phase] = status
        if status == "working":
            self._console_write(
                f"[#38ccff]▶ {escape(_PHASE_SHORT[phase])}[/] "
                f"[#9a7f4a]working[/]")
            return
        if status not in ("done", "failed", "blocked"):
            return
        done = {name for name, state in self._flow_states.items() if state == "done"}
        if status == "done":
            done.add(phase)
        head, nxt = workforce_note(
            phase, status, str(payload.get("summary") or ""), done)
        glyph, tone = {
            "done": ("✓", "#1fe07b"),
            "failed": ("×", "#ff5257"),
            "blocked": ("!", "#ffb020"),
        }[status]
        self._console_write(f"[{tone}]{glyph} {escape(head)}[/]")
        self._console_write(f"[#8797a8]  {escape(nxt)}[/]")

    def _console_stream_text(self, text: str) -> None:
        """Append streamed narrative, emitting only completed lines."""
        self._console_partial += text
        *complete, self._console_partial = self._console_partial.split("\n")
        for line in complete:
            if line.strip():
                self._console_write(f"[#cdd9e6]{escape(demojibake(line))}[/]")

    def _console_flush(self) -> None:
        if self._console_partial.strip():
            self._console_write(f"[#cdd9e6]{escape(self._console_partial)}[/]")
        self._console_partial = ""

    def _tick_pulse(self) -> None:
        """Animate working-state glyphs only while something is running."""
        self._pulse += 1
        states = set(self._agent_states.values())
        workflows = self.snapshot.get("workflows", []) if self.snapshot else []
        running = workflows and workflows[0].get("status") == "running"
        if "working" in states or running or self.claude.running:
            self._render_agents()
            self._render_flow()
            if self.active_view == "workforce":
                self._render_workforce()

    def _apply_snapshot(self, snapshot: dict) -> None:
        self.snapshot = snapshot
        assets = snapshot.get("market", {}).get("assets", [])
        self._sync_universe([row["ticker"] for row in assets])
        if assets and self.active_ticker not in {row["ticker"] for row in assets}:
            self.active_ticker = assets[0]["ticker"]
        self._render_nav()
        self._render_universe(assets)
        self._render_desk()
        self._render_market()
        self._render_workforce()
        self._render_research()
        self._render_audit()
        if not self._action_running:
            self._agent_states = {
                str(agent.get("name")): str(agent.get("state", "idle"))
                for agent in snapshot.get("agents", [])
            }
        self._render_agents()
        self._render_status()
        self._ingest_events(snapshot.get("events", []))
        self._maybe_offer_workforce()

    # -- renderers --------------------------------------------------------
    def _render_nav(self) -> None:
        labels = (
            ("1", "desk"), ("2", "market"), ("3", "workforce"),
            ("4", "research"), ("5", "audit"),
        )
        lines = []
        for key, view in labels:
            if view == self.active_view:
                lines.append(f"[bold #f6fafe]› {key}  {view.title()}[/]")
            else:
                lines.append(f"[#8797a8]  {key}  {view.title()}[/]")
        self.query_one("#nav", Static).update("\n".join(lines))

    def _sync_universe(self, tickers: list[str]) -> None:
        """Adopt the owner's universe so the spine follows the mandate config."""
        if not tickers or tickers == self.universe_tickers:
            return
        view = self.query_one("#universe", ListView)
        index = view.index or 0
        self.universe_tickers = list(tickers)
        if len(view.children) != len(tickers):
            view.remove_children()
            view.mount(*(ListItem(Label(ticker)) for ticker in tickers))
        view.index = min(index, len(tickers) - 1)

    def _render_universe(self, assets: list[dict]) -> None:
        by_ticker = {row["ticker"]: row for row in assets}
        view = self.query_one("#universe", ListView)
        for ticker, item in zip(self.universe_tickers, view.children):
            row = by_ticker.get(ticker)
            label = item.query_one(Label)
            if row:
                sign = "+" if row["change_1d"] >= 0 else ""
                label.update(f"{ticker:<5} {row['price']:>7.2f} {sign}{row['change_1d']:.1%}")
            else:
                label.update(ticker)

    def _render_desk(self) -> None:
        if not self.snapshot:
            return
        portfolio = self.snapshot.get("portfolio", {})
        market = self.snapshot.get("market", {})
        regime = market.get("regime", {})
        current = portfolio.get("weights", {})
        targets = portfolio.get("target_weights", {})
        equity = float(portfolio.get("equity", 0.0))
        cash = float(portfolio.get("cash", 0.0))
        drawdown = float(portfolio.get("drawdown", 0.0))
        kill_at = float(portfolio.get("kill_switch_at", 0.0))

        dd_col = DOWN if drawdown >= kill_at * 0.5 else TEXT
        lines = [
            f"[bold #f6fafe]{money(equity)}[/]   [#8797a8]cash {money(cash)}[/]",
            f"[#8797a8]drawdown[/] [{dd_col}]{pct(drawdown)}[/]   "
            f"[#8797a8]kill switch[/] [#eb9a2e]{pct(kill_at, 0)}[/]",
            "",
        ]
        # The allocation bars are the desk's chart; widen them with the window.
        avail_w, _ = self._plot_region("#desk-content")
        bar_w = max(16, min(44, avail_w - 30))
        lines.append(
            f"[#9a7f4a]{'CURRENT ALLOCATION':<{bar_w + 6}}      CURRENT   TARGET[/]")
        held_outside = [t for t in current if t not in self.universe_tickers
                        and abs(float(current[t])) > 0.0005]
        for ticker in [*self.universe_tickers, *held_outside]:
            cur = float(current.get(ticker, 0.0))
            target = targets.get(ticker)
            target_text = "  —  " if target is None else f"{float(target):5.1%}"
            filled = min(bar_w, max(0, round(cur * bar_w)))
            track = "░" * (bar_w - filled)
            # Amber up to the target; anything above target is an overweight and
            # reads red, so drift is visible at a glance, not just in the number.
            if target is not None and cur > float(target):
                tpos = min(filled, round(float(target) * bar_w))
                bar = (f"[#ffb020]{'█' * tpos}[/][#ff5257]{'█' * (filled - tpos)}[/]"
                       f"[#33475a]{track}[/]")
            else:
                bar = f"[#ffb020]{'█' * filled}[/][#33475a]{track}[/]"
            lines.append(
                f"[#cdd9e6]{ticker:<5}[/] {bar}  [#f6fafe]{cur:6.1%}[/]  →  "
                f"[#cdd9e6]{target_text}[/]")

        lines.extend([
            "",
            "[#9a7f4a]MARKET REGIME[/]",
            f"[bold]{str(regime.get('regime', 'unknown')).upper()}[/]   "
            f"[#8797a8]{escape(str(regime.get('method', 'unavailable')))}[/]",
            f"realized volatility signal  {pct(regime.get('signal'))}",
            f"stress threshold             {pct(regime.get('threshold'))}",
            "",
        ])

        plans = self.snapshot.get("plans", [])
        if plans:
            plan = plans[0]
            turnover = plan.get("pre_trade", {}).get("turnover")
            lines.extend([
                "[#9a7f4a]LATEST PROPOSAL[/]",
                f"[bold]{escape(plan['plan_id'][:12])}[/]   "
                f"state {escape(str(plan.get('state', 'unknown')))}   "
                f"turnover {pct(turnover)}",
                "Use [bold]: view audit[/] to inspect the decision and order trail.",
            ])
        else:
            lines.extend([
                "[#9a7f4a]PROPOSAL[/]",
                "No active rebalance proposal.",
            ])

        source = str(market.get("source", "unknown")).upper()
        policy = self.snapshot.get("policy") or {}
        lines.extend([
            "",
            f"[#9a7f4a]POLICY[/]  [#cdd9e6]{escape(str(policy.get('label', policy.get('id', '—'))))}[/] "
            "[#586777]· MVSK research only[/]",
            f"[#9a7f4a]DATA[/]  [#cdd9e6]{source} · {market.get('frequency', 'unknown')} · "
            f"as of {market.get('as_of', '—')} · age {market.get('bar_age_days', '—')}d[/]",
        ])
        self.query_one("#desk-content", Static).update("\n".join(lines))

    def _plot_region(self, widget_id: str) -> tuple[int, int]:
        """Cells available to a chart in ``widget_id`` — its live size, or a
        size derived from the terminal when layout has not settled yet."""
        try:
            region = self.query_one(widget_id).size
            avail_w, avail_h = int(region.width), int(region.height)
        except Exception:
            avail_w = avail_h = 0
        if avail_w <= 0:
            # Terminal minus the two side rails and padding; a floor keeps the
            # very first pre-layout paint sane.
            avail_w = max(40, self.size.width - 34)
        if avail_h <= 0:
            avail_h = max(16, self.size.height - 8)
        return avail_w, avail_h

    def _render_market(self) -> None:
        if not self.snapshot:
            return
        market = self.snapshot.get("market", {})
        assets = {row["ticker"]: row for row in market.get("assets", [])}
        row = assets.get(self.active_ticker)
        if not row:
            return
        portfolio = self.snapshot.get("portfolio", {})
        current = portfolio.get("weights", {}).get(self.active_ticker, 0.0)
        target = portfolio.get("target_weights", {}).get(self.active_ticker)
        history = [float(x) for x in row.get("history", []) if x is not None]
        up = float(row.get("change_1d", 0.0)) >= 0
        dir_col = UP if up else DOWN

        # The chart takes the whole width and most of the height; the readouts
        # underneath get what's left. Both track the terminal size, so a wider
        # or taller window is spent on the plot. The left price gutter and the
        # two-line time axis are reserved out of the plot's cells first, so the
        # braille curve and its labels always align.
        avail_w, avail_h = self._plot_region("#market-content")
        hi = max(history) if history else 0.0
        lo = min(history) if history else 0.0
        mid = (hi + lo) / 2.0
        # Y axis (price): a right-aligned gutter carrying the high, midpoint, and
        # low ticks. Its width is the widest of the three labels so every row's
        # plot area starts at the same column.
        gutter = max(len(money(hi)), len(money(mid)), len(money(lo))) if history else 0
        chart_w = max(24, avail_w - gutter - 2)
        chart_h = max(6, min(30, avail_h - 14))
        rows = braille_chart(history, chart_w, chart_h)
        last_row = len(rows) - 1
        mid_row = last_row // 2
        as_of = str(market.get("as_of", "—"))

        lines = [
            f"[bold #f6fafe]{escape(self.active_ticker)}[/]  "
            f"[bold #f6fafe]{money(row.get('price'))}[/]  "
            f"[{dir_col}]{'▲' if up else '▼'} {pct(row.get('change_1d'))} today[/]"
            f"    [#9a7f4a]HIGH[/] [#cdd9e6]{money(hi)}[/]  "
            f"[#9a7f4a]LOW[/] [#cdd9e6]{money(lo)}[/]  "
            f"[#9a7f4a]{len(history)} DAILY BARS[/]",
            "",
        ]
        for i, bar in enumerate(rows):
            if not history:
                tick = ""
            elif i == 0:
                tick = money(hi)
            elif i == last_row:
                tick = money(lo)
            elif i == mid_row:
                tick = money(mid)
            else:
                tick = ""
            lines.append(
                f"[#9a7f4a]{tick:>{gutter}}[/] [#33465b]│[/]"
                f"[{dir_col}]{escape(bar)}[/]")
        # X axis (time): a baseline under the plot, then the oldest→latest span,
        # then a one-line legend naming both axes so the plot is self-defining.
        pad = " " * (gutter + 2)
        lines.append(f"[#33465b]{' ' * gutter} └{'─' * chart_w}[/]")
        left_lbl = f"{len(history)} bars ago"
        right_lbl = f"as of {as_of}"
        gap = chart_w - len(left_lbl) - len(right_lbl)
        span = (left_lbl + " " * gap + right_lbl if gap >= 1
                else f"{len(history)} bars → {as_of}"[:chart_w])
        lines.append(f"[#9a7f4a]{pad}{escape(span)}[/]")
        lines.append(
            f"[#586777]{pad}X · time (daily bars, oldest → latest)"
            "   Y · price (USD)[/]")
        lines.append("")

        stats = [
            ("20-day change", pct(row.get("change_20d"))),
            ("63-day vol", pct(row.get("realized_vol"))),
            ("portfolio weight", pct(current)),
            ("target weight", pct(target)),
            ("regime", str(market.get("regime", {}).get("regime", "—")).upper()),
            ("source", str(market.get("source", "—")).upper()),
            ("as of", str(market.get("as_of", "—"))),
            ("bar age", f"{market.get('bar_age_days', '—')} days"),
        ]
        per_row = 2 if avail_w >= 78 else 1
        for i in range(0, len(stats), per_row):
            cells = [
                f"[#9a7f4a]{escape(label):<18}[/][#f6fafe]{escape(str(value)):<16}[/]"
                for label, value in stats[i:i + per_row]
            ]
            lines.append("  ".join(cells))
        lines.append(
            "[#9a7f4a]Daily adjusted-close context; this is not a streaming quote.[/]")
        self.query_one("#market-content", Static).update("\n".join(lines))

    def _render_flow(self) -> None:
        """Paint the five-node agent flowchart from the current flow state."""
        for phase, agent, short in _FLOW:
            try:
                node = self.query_one(f"#flow-{phase}", FlowNode)
            except Exception:
                continue
            state = self._flow_states.get(phase, "idle")
            glyph, color = _STATE_STYLE.get(state, ("◌", "#586777"))
            if state == "working":
                glyph = _PULSE_FRAMES[self._pulse % len(_PULSE_FRAMES)]
            node.update(
                f"[bold]{escape(short)}[/]\n[{color}]{glyph} {escape(state)}[/]")
            for token in ("working", "queued", "done", "failed", "blocked"):
                node.set_class(token == state, f"-{token}")
            node.tooltip = self._flow_details.get(phase) or (
                f"{agent}\n\nnot yet started")

    def _flow_detail(self, agent: str, phase: str, step: dict) -> str:
        """A phase's hover card: agent, state, elapsed, summary, artifacts."""
        state = str(step.get("status", "queued"))
        elapsed = phase_elapsed(step.get("started_at"), step.get("completed_at"))
        head = f"{agent} · {phase}\nstate {state}" + (
            f" · {elapsed}" if elapsed else "")
        summary = str(step.get("summary") or "").strip()
        if summary:
            head += f"\n\n{summary[:400]}"
        artifacts = step.get("artifacts") or {}
        if artifacts:
            packed = "  ".join(f"{key}={artifacts[key]}"
                               for key in list(artifacts)[:4])
            head += f"\n\nartifacts: {packed[:200]}"
        return head

    def _select_workflow(self, workflows: list[dict]) -> dict | None:
        """The run the view is bound to — never a stale one.

        A launched run owns the view from the moment it starts, but its durable
        row only appears once the coordinator calls workflow.start. In that gap
        the previous run must not be shown: its finished nodes would read as
        this run's progress. So a pending launch adopts the first workflow it
        did not already know about, and shows nothing until then.
        """
        by_id = {str(row.get("workflow_id", "")): row for row in workflows}
        if self._active_workflow_id:
            return by_id.get(self._active_workflow_id)
        if self._pending_workflow:
            for row in workflows:  # newest first
                workflow_id = str(row.get("workflow_id", ""))
                if workflow_id and workflow_id not in self._seen_workflow_ids:
                    self._active_workflow_id = workflow_id
                    self._pending_workflow = False
                    return row
            return None
        return workflows[0] if workflows else None

    def _render_workforce(self) -> None:
        workflows = self.snapshot.get("workflows", []) if self.snapshot else []
        workflow = self._select_workflow(workflows)
        if workflow is None:
            if self._pending_workflow:
                empty = ("[#9a7f4a]STARTING RUN[/]   "
                         "[#8797a8]the coordinator is opening a durable workflow — "
                         "phases appear here as they register.[/]")
            elif self._active_workflow_id:
                empty = ("[#9a7f4a]RESUMING[/]   "
                         f"[#8797a8]{escape(self._active_workflow_id)} is outside "
                         "the recent-run window; its phases appear as they "
                         "advance.[/]")
            else:
                empty = ("[#9a7f4a]NO DURABLE RUN[/]   "
                         "[#8797a8]type a goal below — Claude runs analyst → "
                         "challenger → optimizer → referee → reporter autonomously "
                         "and makes its own best-estimate calls. Hover a node above "
                         "for its live update.[/]")
            self.query_one("#workforce-content", Static).update(empty)
            # A pending launch already reset every node to 'queued'; only an
            # idle desk falls back to 'idle'.
            if not self._pending_workflow and not self.claude.running \
                    and not self._action_running:
                self._flow_states = {phase: "idle" for phase, _, _ in _FLOW}
                self._flow_details = {}
            self._render_flow()
            return

        request = workflow.get("request") or {}
        status = str(workflow.get("status", "unknown"))
        steps = workflow.get("steps", [])
        step_by_phase = {str(step.get("phase")): step for step in steps}

        # Rebuild flow state/detail from durable steps; where a phase has no
        # step yet, keep a live 'working' the tool stream set, else queue it.
        for phase, agent, _short in _FLOW:
            step = step_by_phase.get(phase)
            if step is not None:
                durable = str(step.get("status", "queued"))
                # The agent stream sees a worker start before that worker gets
                # its `working` update persisted; don't drop back to queued.
                live_working = (durable == "queued"
                                and self._flow_states.get(phase) == "working")
                self._flow_states[phase] = "working" if live_working else durable
                self._flow_details[phase] = self._flow_detail(agent, phase, step)
            else:
                live = self._flow_states.get(phase)
                self._flow_states[phase] = (
                    "working" if live == "working"
                    else "queued" if status == "running" else "idle")
                self._flow_details.setdefault(
                    phase, f"{agent} · {phase}\n\nnot yet started")
        self._render_flow()

        lines = [
            f"[bold #f6fafe]{escape(str(workflow.get('workflow_id', '—')))}[/]   "
            f"{escape(status.upper())}",
            f"[#9a7f4a]{escape(str(workflow.get('kind', 'portfolio_review')))} · "
            f"as of {escape(str(request.get('as_of', '—')))} · "
            f"{escape(str(request.get('universe', 'core')))}[/]",
            f"[#8797a8]{escape(str(request.get('goal', 'Governed portfolio review'))[:160])}[/]",
        ]

        # The regime the analyst selected, as soon as it persists — the operator
        # reads what drove the window/shrinkage call without opening a hover.
        regime_line = _regime_line(step_by_phase)
        if regime_line:
            lines.append(regime_line)

        result = workflow.get("result") or {}
        if status == "complete" and result.get("final_summary"):
            referee = step_by_phase.get("referee")
            verdict = str(((referee or {}).get("artifacts") or {})
                          .get("verdict") or "")
            chip = f"  ·  referee {verdict}" if verdict else ""
            lines.extend([
                "",
                f"[#1fe07b]▮ RESULT{chip}[/]",
                f"[#cdd9e6]{escape(str(result['final_summary'])[:320])}[/]",
            ])
            targets = _extract_targets(step_by_phase)
            if targets:
                lines.append(
                    f"[#9a7f4a]target weights[/]  "
                    f"[#f6fafe]{escape(_format_targets(targets))}[/]")
            plan_id = str((step_by_phase.get("reporter", {}).get("artifacts")
                           or {}).get("plan_id") or "")
            if plan_id:
                lines.append(
                    f"[#9a7f4a]checked plan[/]  [#f6fafe]{escape(plan_id)}[/]  "
                    "[#8797a8]→ : rebalance paper to confirm[/]")
        elif status in ("failed", "blocked"):
            tone = "#ff5257" if status == "failed" else "#ffb020"
            broken = next(
                (s for s in steps if s.get("status") in ("failed", "blocked")),
                None)
            why = str((broken or {}).get("summary") or "").strip()
            lines.extend([
                "",
                f"[{tone}]▮ {status.upper()} at "
                f"{escape(str((broken or {}).get('phase', '—')))}[/]"
                + (f"\n[#8797a8]{escape(why[:200])}[/]" if why else ""),
                "[#9a7f4a]Resume with : workforce resume "
                f"{escape(str(workflow.get('workflow_id', '')))}[/]",
            ])

        earlier = [row for row in workflows
                   if str(row.get("workflow_id", "")) != self._active_workflow_id
                   and row is not workflow][:4]
        if earlier:
            packed = "   ".join(
                f"{escape(str(row.get('workflow_id', '—')))} "
                f"[#8797a8]{escape(str(row.get('status', '')))}[/]"
                for row in earlier)
            lines.append(
                f"[#9a7f4a]earlier (: workforce resume ID)[/]  {packed}")
        self.query_one("#workforce-content", Static).update("\n".join(lines))

    def _maybe_offer_workforce(self) -> None:
        if self._claude_offer_handled:
            return
        if self.claude_start == "off":
            self._claude_offer_handled = True
            return
        # Not yet available (e.g. first snapshot raced readiness): leave the
        # sentinel unset so a later snapshot can still make the one offer.
        if not bool(self.snapshot.get("system", {}).get("workforce_available")):
            return
        self._claude_offer_handled = True
        if self.claude_start == "auto":
            self._start_workforce("")
        elif self.claude_start == "offer":
            self.push_screen(ClaudeWorkforceScreen(), self._workforce_start_choice)

    def _workforce_start_choice(self, start: bool | None) -> None:
        if start:
            self._start_workforce("")
        else:
            self._set_selected_work(
                "CLAUDE WORKFORCE READY\n\nStart later with : workforce GOAL"
            )

    def _render_research(self) -> None:
        runs = self.snapshot.get("runs", [])
        algorithms = self.snapshot.get("algorithms", [])
        summary = [
            "Experiments and solver evidence share the same registry as the paper book.",
            "",
        ]
        if algorithms:
            counts = {
                stage: sum(row.get("stage") == stage for row in algorithms)
                for stage in ("operational", "research", "offline")
            }
            summary.append(
                f"Algorithms  [bold]{counts['operational']} operational[/]"
                f"   ·   {counts['research']} research   ·   {counts['offline']} offline"
            )
        summary.append("Run [bold]: batch[/] for the staged comparison suite.")
        self.query_one("#research-summary", Static).update("\n".join(summary))

        signature = tuple((r.get("run_id"), r.get("created_at")) for r in runs)
        if signature == self._runs_signature:
            return
        self._runs_signature = signature
        table = self.query_one("#runs-table", DataTable)
        table.clear()
        for run in runs:
            table.add_row(
                str(run.get("run_id", ""))[:12],
                str(run.get("kind", "")),
                str(run.get("created_at", ""))[5:19].replace("T", " "),
                key=str(run.get("run_id", "")),
            )

    def _render_audit(self) -> None:
        decisions = self.snapshot.get("decisions", [])
        plans = self.snapshot.get("plans", [])
        orders = self.snapshot.get("orders", [])
        self.query_one("#audit-summary", Static).update(
            "Every judgment, structured proposal, and paper fill remains inspectable.\n\n"
            f"{len(decisions)} decisions   ·   {len(plans)} proposals   ·   {len(orders)} orders"
        )
        rows = []
        self._audit_decisions = {}
        for decision in decisions:
            choice = decision.get("choice", {})
            detail = choice.get("regime") or choice.get("arm") or decision.get("rationale", "")
            key = str(decision.get("decision_id", ""))
            self._audit_decisions[key] = decision
            rows.append((
                decision.get("created_at", ""), "decision", decision.get("kind", ""),
                _verdict_cell(decision.get("verdict")),
                _reflection_cell(decision.get("reflection")),
                str(detail)[:48], key,
            ))
        for plan in plans:
            rows.append((
                plan.get("created_at", ""), "proposal", plan.get("state", ""),
                "—", "—",
                f"decision {str(plan.get('decision_id', ''))[:10]}", plan.get("plan_id", ""),
            ))
        for order in orders:
            rows.append((
                order.get("created_at", ""), "paper order", order.get("state", ""),
                "—", "—",
                f"{order.get('side', '')} {order.get('ticker', '')} ${order.get('notional', 0):,.2f}",
                order.get("client_order_id", ""),
            ))
        rows.sort(key=lambda row: row[0], reverse=True)
        signature = tuple((row[6], row[0], row[2], row[3], row[4]) for row in rows)
        if signature == self._audit_signature:
            return
        self._audit_signature = signature
        table = self.query_one("#audit-table", DataTable)
        table.clear()
        for created, obj, state, verdict_cell, reflection_cell, detail, key in rows[:80]:
            table.add_row(
                str(created)[5:19].replace("T", " "), obj, str(state),
                verdict_cell, reflection_cell, str(detail),
                key=str(key),
            )

    def _render_audit_detail(self, key: str) -> None:
        """Full decision detail in the work rail; a summary on the strip.

        The audit table stays compact; selecting a row expands the judgment —
        rationale, challenger case, verdict with reasons, reflection — where
        there is room to actually read it.
        """
        decision = self._audit_decisions.get(str(key))
        if not decision:
            return
        challenger = str(decision.get("challenger_view") or "").strip() or "no challenge recorded"
        verdict = decision.get("verdict") or {}
        label = str(verdict.get("verdict", "—")).upper() if verdict else "—"
        reasons = verdict.get("reasons") or []
        reason_text = "; ".join(str(reason) for reason in reasons) if reasons else "—"
        rationale = str(decision.get("rationale") or "").strip() or "—"
        reflection = str(decision.get("reflection") or "").strip() or "pending"
        choice = decision.get("choice") or {}
        choice_text = "  ".join(
            f"{k}={choice[k]}" for k in list(choice)[:4]) or "—"
        card = "\n".join([
            f"DECISION {str(key)[:16]}",
            f"{decision.get('kind', '—')} · {decision.get('as_of', '—')}",
            "",
            "CHOICE",
            choice_text[:160],
            "",
            "RATIONALE",
            rationale[:300],
            "",
            "CHALLENGER",
            challenger[:300],
            "",
            f"VERDICT  {label}",
            reason_text[:300],
            "",
            "REFLECTION",
            reflection[:300],
        ])
        self._set_selected_work(card)
        line = f"{str(key)[:10]}  verdict {label} · challenger + rationale in the work rail →"
        self.query_one("#event-strip", Static).update(escape(line))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "audit-table" or event.row_key is None:
            return
        key = event.row_key.value
        if key:
            self._render_audit_detail(str(key))

    def _render_agents(self) -> None:
        agents = self.snapshot.get("agents", []) if self.snapshot else []
        if not agents:
            agents = [
                {"name": name, "authority": "—", "state": "idle"}
                for name in _AGENT_NAMES
            ]
        lines = []
        for agent in agents:
            name = agent["name"]
            state = self._agent_states.get(name, agent.get("state", "idle"))
            glyph, color = {
                "working": ("●", "#38ccff"),
                "queued": ("◐", "#eb9a2e"),
                "waiting": ("◐", "#eb9a2e"),
                "done": ("✓", "#1fe07b"),
                "failed": ("×", "#ff5257"),
                "blocked": ("!", "#ffb020"),
            }.get(state, ("◌", "#586777"))
            if state == "working":
                glyph = _PULSE_FRAMES[self._pulse % len(_PULSE_FRAMES)]
            lines.append(
                f"[{color}]{glyph}[/] [bold]{escape(name)}[/]\n"
                f"   [#586777]{escape(state)} · {escape(str(agent.get('authority', '—')))}[/]"
            )
        self.query_one("#agent-list", Static).update("\n".join(lines))

    def _render_status(self) -> None:
        system = self.snapshot.get("system", {})
        market = self.snapshot.get("market", {})
        source = str(market.get("source", "data")).upper()
        if system.get("mcp_proxy_available"):
            mcp = "MCP WORKFORCE"
        elif system.get("mcp_configured"):
            mcp = "MCP LOCKED"
        else:
            mcp = "MCP —"
        claude = "CLAUDE READY" if system.get("claude_available") else "CLAUDE —"
        if self.claude.running:
            claude = "CLAUDE WORKING"
        data_source = str(system.get("data_source", "none"))
        age = system.get("data_age_days")
        if data_source in ("none", "") or age is None:
            data_token = "DATA none"
        else:
            data_token = f"DATA {data_source}·{age}d"
        self.query_one("#system-status", Static).update(
            f"PAPER · {source}/DAILY · {mcp} · {claude} · {data_token}")
        self.query_one("#chat-exit", Button).label = (
            "■ stop" if self.claude.running else "exit")
        self._sync_chat_input()

    # -- events -----------------------------------------------------------
    def _ingest_events(self, events_: list[dict]) -> None:
        for event in events_:
            event_id = str(event.get("event_id", ""))
            if event_id and event_id in self._event_ids:
                continue
            if event_id:
                self._event_ids.add(event_id)
            self._append_event(event)
            if str(event.get("kind", "")) == "workflow_phase":
                self._note_workflow_phase(event.get("payload") or {})

    def _append_event(self, event: dict) -> None:
        ts = str(event.get("ts", ""))
        clock = ts[11:19] if len(ts) >= 19 else datetime.now().strftime("%H:%M:%S")
        kind = str(event.get("kind", "event"))
        payload = event.get("payload", {})
        detail = json.dumps(payload, sort_keys=True, default=str)
        if len(detail) > 180:
            detail = detail[:177] + "…"
        line = f"{clock}  {kind}  {detail if detail != '{}' else ''}".rstrip()
        self.query_one("#timeline", RichLog).write(line)
        self.query_one("#event-strip", Static).update(line)

    def _write_local_event(self, kind: str, payload: dict) -> None:
        self._append_event({"ts": datetime.now().isoformat(), "kind": kind, "payload": payload})

    # -- navigation -------------------------------------------------------
    def action_view(self, view: str) -> None:
        if view not in _VIEWS:
            return
        self._agent_focus = False
        self.screen.remove_class("agent-focus")
        self.active_view = view
        self.query_one("#canvas", ContentSwitcher).current = view
        self._render_nav()
        if view == "workforce":
            field = self.query_one("#chat-input", Input)
            if not field.disabled:  # a running turn owns the box; don't grab it
                field.focus()

    def action_next_symbol(self) -> None:
        if isinstance(self.focused, Input):
            return
        view = self.query_one("#universe", ListView)
        index = 0 if view.index is None else min(
            len(self.universe_tickers) - 1, view.index + 1)
        view.index = index
        self.active_ticker = self.universe_tickers[index]
        self._render_market()

    def action_previous_symbol(self) -> None:
        if isinstance(self.focused, Input):
            return
        view = self.query_one("#universe", ListView)
        index = 0 if view.index is None else max(0, view.index - 1)
        view.index = index
        self.active_ticker = self.universe_tickers[index]
        self._render_market()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = event.list_view.index
        if event.list_view.id != "universe" or index is None:
            return
        if 0 <= index < len(self.universe_tickers):
            self.active_ticker = self.universe_tickers[index]
        self.action_view("market")
        self._render_market()

    def action_command(self) -> None:
        command = self.query_one("#command", Input)
        command.focus()

    def action_escape(self) -> None:
        if isinstance(self.focused, Input):
            self.set_focus(None)

    def action_timeline(self) -> None:
        timeline = self.query_one("#timeline", RichLog)
        timeline.styles.display = "none" if timeline.styles.display != "none" else "block"

    def action_agent_focus(self) -> None:
        if self.size.width >= 105:
            return
        self._agent_focus = not self._agent_focus
        self.screen.set_class(self._agent_focus, "agent-focus")

    # -- command surface --------------------------------------------------
    def on_input_submitted(self, event: Input.Submitted) -> None:
        raw = event.value.strip()
        event.input.value = ""
        if event.input.id == "chat-input":
            if raw:
                self._chat_send(raw)
            return
        if raw.startswith(":"):
            raw = raw[1:].strip()
        if not raw:
            return
        self._handle_command(raw)

    # -- coordinator chat --------------------------------------------------
    def _chat_send(self, message: str) -> None:
        """One chat turn with the workforce coordinator.

        The first message starts a governed session; later messages resume
        the same Claude CLI session so the coordinator keeps its context.
        """
        if self.claude.running:
            self._console_write(
                "[#eb9a2e]a session is working — wait for the turn to "
                "finish or press stop[/]")
            return
        self._console_write(f"[bold #ffb020]you ▸[/] [#f6fafe]{escape(message)}[/]")
        if self._chat_mode == "chat":
            resume = self._chat_sessions["chat"]
            if not resume:
                self._console_write(
                    "[#9a7f4a]▌ chat — read-only desk assistant[/]")
            self._start_claude(message, governed=False, chat=True,
                               resume_session=resume)
        elif self._chat_sessions["workforce"]:
            self._start_claude(
                message, governed=True,
                resume_session=self._chat_sessions["workforce"])
        else:
            self._start_workforce(message)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "chat-exit":
            return
        if self.claude.running:
            self.claude.stop()
            self._console_write(
                "[#eb9a2e]■ stopped — durable phase state is kept; send a "
                "message to continue or use : workforce resume ID[/]")
            self._write_local_event("claude.workforce_stopped", {})
        else:
            self.action_view("desk")

    def _handle_command(self, raw: str) -> None:
        command, _, rest = raw.partition(" ")
        command = command.lower()
        rest = rest.strip()

        if command == "view" and rest.lower() in _VIEWS:
            self.action_view(rest.lower())
        elif (command == "view" and rest.lower() == "agents") or command == "agents":
            self.action_agent_focus()
        elif command == "symbol" and rest.upper() in self.universe_tickers:
            self.active_ticker = rest.upper()
            self.query_one("#universe", ListView).index = (
                self.universe_tickers.index(self.active_ticker))
            self.action_view("market")
            self._render_market()
        elif command == "timeline":
            self.action_timeline()
        elif command == "help":
            self._set_selected_work(
                "COMMANDS\n\n"
                "view desk|market|workforce|research|audit\n"
                "view agents\n"
                "symbol TICKER\n"
                "chat MESSAGE      (read-only desk assistant)\n"
                "workforce GOAL    (governed five-role pipeline)\n"
                "workforce status\n"
                "workforce resume ID\n"
                "workforce stop\n"
                "rebalance dry\n"
                "rebalance paper\n"
                "daily\n"
                "batch\n"
                "ask PROMPT  (isolated, no tools)\n"
                "timeline\n\n"
                "1–5 switch views · j/k select instrument · Ctrl-Q quits"
            )
        elif command == "ask" and rest:
            self._start_claude(rest, governed=False)
        elif command == "chat":
            self._chat_mode = "chat"
            self.action_view("workforce")
            self._render_chat_mode()
            if rest:
                self._chat_send(rest)
        elif command in {"workforce", "governed"}:
            self._chat_mode = "workforce"
            self._render_chat_mode()
            system = self.snapshot.get("system", {})
            if rest.lower() == "status":
                self.action_view("workforce")
                self._render_workforce()
            elif rest.lower() == "stop":
                self.claude.stop()
                self._set_selected_work(
                    "CLAUDE WORKFORCE STOPPED\n\nThe owner kept its durable phase state. "
                    "Use : workforce resume ID to continue."
                )
                self._write_local_event("claude.workforce_stopped", {})
            elif rest.lower().startswith("resume "):
                workflow_id = rest.split(None, 1)[1].strip()
                self._start_workforce("", workflow_id=workflow_id)
            elif not system.get("workforce_available", system.get("governed_available")):
                reason = system.get(
                    "governed_lock_reason", "Claude workforce runtime is not ready")
                self._set_selected_work(f"CLAUDE WORKFORCE LOCKED\n\n{reason}")
                self._write_local_event("claude.workforce_locked", {"reason": reason})
            else:
                self._start_workforce(rest)
        elif command == "rebalance" and rest.lower() == "dry":
            self._run_api_action(
                "rebalance dry", "/api/run_once",
                {"offline": self.offline, "execute": False},
                active_agent="moments-analyst",
            )
        elif command == "rebalance" and rest.lower() == "paper":
            plan = next(
                (row for row in self.snapshot.get("plans", [])
                 if row.get("state") in {"checked", "submitted"}),
                None,
            )
            if plan is None:
                self._set_selected_work(
                    "NO CHECKED PLAN\n\nRun : rebalance dry or complete a workforce "
                    "review before requesting paper execution."
                )
            else:
                self._pending_plan_id = str(plan["plan_id"])
                self.push_screen(
                    PaperConfirmScreen(self._pending_plan_id), self._paper_confirmed
                )
        elif command == "daily":
            self._run_api_action(
                "daily ops", "/api/daily_ops", {"offline": self.offline},
                active_agent="reporter",
            )
        elif command == "batch":
            self.action_view("research")
            self._run_api_action(
                "batch ablation", "/api/batch",
                {"offline": self.offline},
                active_agent="optimization-runner",
            )
        else:
            self._write_local_event("command.unknown", {"command": raw})
            self._set_selected_work("Unknown command. Use : help for the command surface.")

    def _paper_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            self._write_local_event("paper.cancelled", {})
            self._pending_plan_id = ""
            return
        plan_id = self._pending_plan_id
        self._pending_plan_id = ""
        self._run_api_action(
            "paper plan execution", "/api/plans/execute",
            {"offline": self.offline, "plan_id": plan_id, "human_confirmed": True},
            active_agent="reporter",
        )

    def _run_api_action(self, label: str, path: str, body: dict, *, active_agent: str) -> None:
        if self._action_running:
            self._write_local_event("action.rejected", {"reason": "another action is running"})
            return
        self._action_running = True
        self._agent_states = {name: "queued" for name in _AGENT_NAMES}
        self._agent_states[active_agent] = "working"
        self._render_agents()
        self._set_selected_work(f"{label.upper()}\n\nRunning through the owner API…")
        self._write_local_event("action.started", {"action": label})

        def run() -> None:
            try:
                result = self.client.post(path, body)
                self.call_from_thread(self._finish_api_action, label, result, None)
            except Exception as exc:
                self.call_from_thread(self._finish_api_action, label, None, exc)

        threading.Thread(target=run, daemon=True).start()

    def _finish_api_action(self, label: str, result: dict | None, error: Exception | None) -> None:
        self._action_running = False
        if error is not None:
            self._agent_states = {name: "failed" if state == "working" else "idle"
                                  for name, state in self._agent_states.items()}
            self._set_selected_work(f"{label.upper()} FAILED\n\n{error!r}")
            self._write_local_event("action.failed", {"action": label, "error": repr(error)})
        else:
            self._agent_states = {name: "done" for name in self._agent_states}
            assert result is not None
            lines = [f"{label.upper()} COMPLETE", ""]
            if "decision_id" in result:
                lines.append(f"decision  {result['decision_id']}")
            if "regime" in result:
                regime = result.get("regime") or {}
                lines.append(f"regime    {regime.get('regime', '—')}")
            trade = result.get("trade") or {}
            if trade:
                lines.append(f"paper execution  {bool(trade.get('executed'))}")
                if trade.get("mandate_violation"):
                    lines.append(f"mandate violation  {trade['mandate_violation']}")
            if "run_id" in result:
                lines.append(f"run       {result['run_id']}")
            if "executed" in result:
                lines.append(f"paper execution  {bool(result.get('executed'))}")
            if result.get("plan_id"):
                lines.append(f"plan      {result['plan_id']}")
            if "triggers" in result:
                lines.append(f"triggers  {', '.join(result.get('triggers') or []) or 'none'}")
            self._set_selected_work("\n".join(lines))
            self._write_local_event("action.completed", {"action": label})
        self._render_agents()
        self._start_refresh()

    # -- Claude stream ----------------------------------------------------
    def _start_workforce(self, goal: str, *, workflow_id: str = "") -> None:
        goal = goal.strip() or (
            "Review the current portfolio and market regime, challenge the "
            "estimation choices, compare the operational allocation policy with "
            "honest benchmarks, apply the referee gate, and prepare a dry human "
            "recommendation. Preserve MVSK as research evidence; do not assume it wins."
        )
        if workflow_id:
            if not workflow_id.replace("-", "").isalnum():
                self._set_selected_work("Invalid workflow id.")
                return
            prompt = (
                f"RESUME_WORKFLOW_ID: {workflow_id}\n"
                "Inspect workflow.status first. Continue at the first non-done phase; "
                "do not create a new workflow.\n"
                f"GOAL: {goal}"
            )
        else:
            prompt = f"GOAL: {goal}"
        self.action_view("workforce")
        self._bind_run(workflow_id)
        if not self._start_claude(prompt, governed=True):
            # Nothing launched, so the view must go back to showing history
            # rather than an empty chart waiting on a run that never starts.
            self._active_workflow_id = ""
            self._pending_workflow = False
            self._render_workforce()

    def _bind_run(self, workflow_id: str = "") -> None:
        """Point every workforce surface at the run about to start.

        Called before the session launches so the flowchart, the console notes,
        and the result block can never be attributed to the previous run — the
        durable row for a new run does not exist yet, and until it does the
        view must show this run's empty state, not the last one's outcome.
        """
        workflows = self.snapshot.get("workflows", []) if self.snapshot else []
        self._seen_workflow_ids = {
            str(row.get("workflow_id", "")) for row in workflows}
        self._active_workflow_id = workflow_id
        self._pending_workflow = not workflow_id
        self._phase_reported = {}
        self._results_printed = False
        self._flow_states = {phase: "queued" for phase, _, _ in _FLOW}
        self._flow_details = {
            phase: f"{agent} · {phase}\n\nqueued — waiting to start"
            for phase, agent, _ in _FLOW}
        self._render_flow()
        self._render_workforce()

    def _start_claude(self, prompt: str, *, governed: bool,
                      resume_session: str = "", chat: bool = False) -> bool:
        if self.claude.running:
            self._write_local_event("claude.rejected", {"reason": "session already running"})
            return False
        self._claude_buffer = ""
        self._claude_saw_delta = False
        mode = "WORKFORCE" if governed else ("CHAT" if chat else "READ-ONLY")
        self._set_selected_work(f"CLAUDE · {mode}\n\nStarting streaming session…")
        if not self.claude.start(prompt, governed=governed, chat=chat,
                                 resume_session=resume_session or None):
            reason = self.claude.last_error or (
                "Claude Code is not available or a session is already running."
            )
            self._set_selected_work(reason)
            return False
        if governed:
            self._console_partial = ""
            if not resume_session:
                first_line = prompt.splitlines()[0]
                self._console_write(
                    f"[bold #ffb020]▌ workforce run[/]  "
                    f"[#8797a8]{escape(first_line[:110])}[/]")
                self._console_write(
                    "[#9a7f4a]running autonomously — one note per agent below; "
                    "hover a node for its live detail[/]")
        self._write_local_event(
            "claude.started",
            {"mode": "workforce" if governed else "read-only", "prompt": prompt[:120]},
        )
        self._render_status()
        return True

    def _receive_claude_event(self, event: ClaudeEvent) -> None:
        try:
            self.call_from_thread(self._apply_claude_event, event)
        except Exception:
            pass  # app closed while the reader thread was finishing

    def _apply_claude_event(self, event: ClaudeEvent) -> None:
        workforce = self.claude.mode == "workforce"
        chat = self.claude.mode == "chat"
        if event.kind == "session":
            session_id = str(event.raw.get("session_id") or "")
            if session_id and self.claude.mode in self._chat_sessions:
                self._chat_sessions[self.claude.mode] = session_id
        elif event.kind in ("text_delta", "text"):
            # The workforce never dumps coordinator/worker prose — its progress
            # lives on the flowchart. Only the read-only chat and ask modes show
            # assistant text (a short conversation, not a governed-run block).
            if event.kind == "text_delta":
                self._claude_saw_delta = True
            elif self._claude_saw_delta:
                return
            if chat:
                self._console_stream_text(
                    event.text if event.kind == "text_delta" else event.text + "\n")
            elif not workforce:  # read-only 'ask'
                self._claude_buffer += event.text
                self._set_selected_work(
                    "CLAUDE · READ-ONLY\n\n" + self._claude_buffer[-6000:])
        elif event.kind == "tool_start":
            agent = self._set_agent_from_tool(event.tool, event.agent)
            payload = {"tool": event.tool}
            if event.agent:
                payload["agent"] = event.agent
            self._write_local_event("claude.tool", payload)
            if workforce:
                base = event.tool.rsplit("__", 1)[-1] or event.tool
                phase = _AGENT_TO_PHASE.get(agent, "")
                # Tool traffic is timeline material, not console material: only
                # the first tool of a phase writes a line, and only when the
                # owner's own `working` event has not already announced it.
                if phase:
                    started = self._flow_states.get(phase) == "working"
                    self._flow_states[phase] = "working"
                    self._flow_details[phase] = (
                        f"{agent} · {phase}\n\nworking — {base}")
                    self._render_flow()
                    if not started and self._phase_reported.get(phase) is None:
                        self._phase_reported[phase] = "working"
                        self._console_write(
                            f"[#38ccff]▶ {escape(_PHASE_SHORT.get(phase, phase))}[/]"
                            f" [#9a7f4a]working[/]")
            elif chat:
                self._console_flush()
                self._console_write(f"[#38ccff]→ {escape(event.tool)}[/]")
        elif event.kind == "error":
            self._write_local_event("claude.failed", {"error": event.text[-400:]})
            if workforce:
                self._console_write(f"[#ff5257]✗ {escape(event.text[-240:])}[/]")
                # A terminal error still owes the operator the run's state: the
                # durable phases reached before it stopped.
                if not self.claude.running:
                    self._print_workforce_results("")
            elif chat:
                self._console_flush()
                self._console_write(f"[#ff5257]✗ {escape(event.text[-300:])}[/]")
            else:
                self._set_selected_work("CLAUDE FAILED\n\n" + event.text[-4000:])
            self._start_refresh()
        elif event.kind == "result":
            self._write_local_event("claude.completed", {})
            if workforce:
                # Reporter is done and the run is complete: the one block that
                # lands in the chat is the run's synthesized results.
                self._print_workforce_results(event.text)
            elif chat:
                self._console_flush()
                self._console_write("[#1fe07b]▮ done[/]")
            elif event.text:
                self._set_selected_work(
                    "CLAUDE · READ-ONLY\n\n" + (self._claude_buffer or event.text)[-6000:])
            self._start_refresh()
        self._render_status()

    def _print_workforce_results(self, text: str) -> None:
        """Print one friendly completion summary when the coordinator's turn ends.

        The intermediate narrative is never streamed; this is the single block
        that reaches the console, and it is built from the durable record alone —
        never a raw model text dump. It reads top to bottom the way a person
        asks: what was achieved, the regime and why, what each agent did in a
        line, the recommendation, and what it means. Only when no durable
        workflow was recorded at all does it fall back to the coordinator's own
        closing text, cleaned of markdown, ids, and mojibake.
        """
        if self._results_printed:
            return
        self._results_printed = True
        workflow = self._select_workflow(
            self.snapshot.get("workflows", []) if self.snapshot else [])
        status = str((workflow or {}).get("status", "")) or "unknown"
        tone, label = _result_banner(status)

        self._console_write("")
        self._console_write(f"[bold {tone}]▮ {label}[/]")

        if workflow is None:
            self._print_results_fallback(text)
            return

        steps = {str(step.get("phase")): step for step in workflow.get("steps", [])}
        verdict = str((steps.get("referee", {}).get("artifacts") or {})
                      .get("verdict") or "")

        # 1 · what was achieved — the conclusion, first.
        self._console_write(f"[#cdd9e6]{escape(_result_headline(status, verdict))}[/]")

        # 2 · the regime the run chose, and why.
        regime_line = _regime_line(steps)
        if regime_line:
            self._console_write("")
            self._console_write(regime_line)

        # 3 · what each agent did, one plain-language line each.
        self._console_write("")
        self._console_write("[#9a7f4a]WHAT EACH AGENT DID[/]")
        for phase, _agent, _short in _FLOW:
            glyph, colour, name, action = _agent_brief(
                phase, steps.get(phase), status)
            self._console_write(
                f"  [{colour}]{glyph}[/] [bold #f6fafe]{escape(name):<11}[/] "
                f"[#cdd9e6]{escape(action)}[/]")

        # 4 · the final output — the weights, and the one action left to a human.
        targets = _extract_targets(steps)
        has_plan = bool((steps.get("reporter", {}).get("artifacts") or {})
                        .get("plan_id"))
        if targets or has_plan:
            self._console_write("")
            self._console_write("[#9a7f4a]RECOMMENDATION[/]")
            if targets:
                self._console_write(
                    f"  [#8797a8]target weights[/]  "
                    f"[#f6fafe]{escape(_format_targets(targets))}[/]")
            if has_plan:
                self._console_write(
                    "  [#8797a8]paper plan[/]     [#1fe07b]ready[/] "
                    "[#8797a8]— type[/] [#f6fafe]: rebalance paper[/] "
                    "[#8797a8]to confirm it yourself[/]")

        # 5 · what the output signifies, in plain terms.
        self._console_write("")
        self._console_write("[#9a7f4a]WHAT THIS MEANS[/]")
        self._console_write(
            "  [#cdd9e6]"
            f"{escape(_result_meaning(status, verdict, bool(targets), has_plan))}[/]")

    def _print_results_fallback(self, text: str) -> None:
        """No durable workflow to summarize: show the coordinator's closing text,
        cleaned of markdown, ids, and mojibake, or a short pointer if it is empty."""
        text = (text or "").strip()
        if not text or text == "Claude completed":
            self._console_write(
                "[#8797a8]The run ended before it recorded any results. Start "
                "again with a goal, or resume it from the workforce view.[/]")
            return
        for raw in text.splitlines():
            is_heading, line = clean_report_line(raw)
            if not line:
                self._console_write("")
            elif is_heading:
                self._console_write(f"[#9a7f4a]{escape(line)}[/]")
            else:
                self._console_write(f"[#f6fafe]{escape(line)}[/]")

    def _set_agent_from_tool(self, tool: str, explicit_agent: str = "") -> str:
        if explicit_agent in _AGENT_NAMES:
            agent = explicit_agent
        elif "workflow_analyst" in tool or "objective_build" in tool:
            agent = "moments-analyst"
        elif "workflow_challenger" in tool:
            agent = "challenger"
        elif "workflow_optimizer" in tool or "algorithms_solve" in tool:
            agent = "optimization-runner"
        elif "workflow_referee" in tool or "log_verdict" in tool or "backtest_run" in tool:
            agent = "referee"
        elif ("workflow_reporter" in tool or "rebalance_preview" in tool
              or "daily_ops" in tool or "portfolio_state" in tool
              or "report_recommendation" in tool):
            agent = "reporter"
        else:
            return ""
        self._agent_states[agent] = "working"
        self._render_agents()
        return agent

    def _set_selected_work(self, text: str) -> None:
        self.query_one("#selected-work", Static).update(escape(text))
