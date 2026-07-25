"""The qlab quiet-workstation operator console."""

from __future__ import annotations

import json
import math
import subprocess
import threading
import time
from datetime import datetime
from typing import Any

from rich.markup import escape
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical
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

from qlab.core.atlas import arm_display_name
from qlab.paths import workspace_root
from qlab.research.prediction import (
    IC_ADMISSION_THRESHOLD,
    IC_STABILITY_THRESHOLD,
)
from qlab.tui.atlas_view import AtlasView
from qlab.tui.claude import ClaudeEvent, ClaudeSession
from qlab.tui.client import gather_snapshot
from qlab.tui.formatting import (
    braille_chart, bulletin, connection_chip, fence_state_after,
    is_numbered_item, key_number_lines, money, pct, phase_elapsed,
    report_lines, sparkline, verdict_chip, weight_bar,
)
from qlab.tui.theme import (
    ALLOCATION_TRACK,
    AMBER,
    AMBER_HI,
    APP_CSS,
    BG,
    BG_PANEL,
    BG_RAISED,
    BORDER,
    BORDER_HI,
    CHART_AXIS,
    CYAN,
    DIM,
    DOWN,
    GOLD,
    LABEL_GOLD,
    MUTED,
    PALETTE_NAME,
    PAPER_MODAL_CSS,
    SEL_BG,
    STATE_STYLE,
    TEXT,
    TEXT_HI,
    UP,
)


_WORKSPACE_ROOT = workspace_root()
_DEFAULT_TICKERS = ["ACWI", "BNDW", "GSG", "IGF", "GLD", "VNQ", "EMB"]
_VIEWS = (
    "dashboard", "market", "workforce", "research", "book", "audit", "atlas",
    "settings",
)
_DASHBOARD_TILE_KEYS = (
    "equity", "allocation", "regime", "market-pulse", "verdict", "run", "alerts",
    "stress",
)
_AGENT_NAMES = (
    "moments-analyst", "challenger", "optimization-runner", "referee", "reporter",
)
# Standard-run fallback before an owner workflow has registered. Once a durable
# row exists, the board is rebuilt from that workflow's ordered steps.
_FLOW = (
    ("analyst", "moments-analyst", "analyst"),
    ("challenger", "challenger", "challenger"),
    ("optimizer", "optimization-runner", "optimizer"),
    ("referee", "referee", "referee"),
    ("reporter", "reporter", "reporter"),
)
_DEFAULT_AGENT_BY_PHASE = {
    phase: agent for phase, agent, _short in _FLOW
}
_DEFAULT_AGENT_BY_PHASE["judge"] = "referee"


def _phase_parts(phase: str) -> tuple[str, int | None]:
    """Return the registry phase type and optional panel branch number."""
    base, dash, suffix = phase.rpartition("-")
    if dash and suffix.isdigit():
        return base, int(suffix)
    return phase, None


def _phase_short(phase: str) -> str:
    base, branch = _phase_parts(phase)
    if branch is not None and base in {"analyst", "optimizer"}:
        return f"v{branch} {base}"
    return base


def _flow_from_steps(
    steps: list[dict],
    *,
    standard_fallback: bool = False,
) -> tuple[tuple[str, str, str], ...]:
    """Build the visible phase board from one workflow's persisted steps.

    Panel rows are stored as all analysts then all optimizers for dependency
    bookkeeping. The operator board groups them by variant instead, followed
    by the join phases in their persisted order.
    """
    specs: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for step in steps:
        if not isinstance(step, dict):
            continue
        phase = str(step.get("phase") or "")
        if not phase or phase in seen:
            continue
        seen.add(phase)
        base, _branch = _phase_parts(phase)
        agent = str(
            step.get("agent")
            or _DEFAULT_AGENT_BY_PHASE.get(base, base)
        )
        specs.append((phase, agent, _phase_short(phase)))

    branch_numbers = sorted({
        branch for phase, _agent, _short in specs
        for base, branch in [_phase_parts(phase)]
        if branch is not None and base in {"analyst", "optimizer"}
    })
    if branch_numbers:
        by_phase = {phase: (phase, agent, short)
                    for phase, agent, short in specs}
        grouped = []
        grouped_phases: set[str] = set()
        for branch in branch_numbers:
            for base in ("analyst", "optimizer"):
                phase = f"{base}-{branch}"
                if phase in by_phase:
                    grouped.append(by_phase[phase])
                    grouped_phases.add(phase)
        grouped.extend(
            spec for spec in specs if spec[0] not in grouped_phases
        )
        return tuple(grouped)

    if standard_fallback:
        by_phase = {phase: (phase, agent, short)
                    for phase, agent, short in specs}
        ordered = [
            by_phase.get(phase, (phase, agent, short))
            for phase, agent, short in _FLOW
        ]
        ordered.extend(spec for spec in specs if spec[0] not in {
            phase for phase, _agent, _short in _FLOW
        })
        return tuple(ordered)
    return tuple(specs) or _FLOW


# What each phase contributes, in one clause — the "what just happened" half of
# the per-phase console note. Mirrors qlab.state.registry's dependency DAG.
_PHASE_DID = {
    "analyst": "chose the estimation window, shrinkage, and regime call, and "
               "logged that judgment",
    "challenger": "argued the opposing case and attached it to the decision",
    "optimizer": "ran the cataloged operational algorithm and produced target "
                 "weights",
    "judge": "compared the branch evidence and selected one persisted result",
    "referee": "independently checked constraints, benchmarks, and the "
               "target binding",
    "reporter": "compiled the human-facing recommendation",
}
_STATE_STYLE = STATE_STYLE
_COLOR_BY_TOKEN = {
    "UP": UP,
    "DOWN": DOWN,
    "MUTED": MUTED,
}
_BOOK_STATE_ALIASES = {
    "proposed": "queued",
    "checked": "done",
    "submitted": "working",
    "filled": "done",
    "cancelled": "blocked",
    "canceled": "blocked",
}
_PULSE_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
# Bus events that mean durable state changed and a full refresh is worth it.
_REFRESH_EVENT_KINDS = {
    "workflow_started", "workflow_phase", "referee_verdict",
    "plan_built", "order_filled", "decision_logged", "ablation_complete",
    "cost_gate_refusal", "autopilot_trigger", "daily_ops",
}
_QUOTE_REPAINT_INTERVAL = 1.0
COMMAND_TABLE = {
    ("view", "dashboard"): "action_view",
    ("view", "desk"): "action_view",
    ("view", "market"): "action_view",
    ("view", "workforce"): "action_view",
    ("view", "research"): "action_view",
    ("view", "book"): "action_view",
    ("view", "audit"): "action_view",
    ("view", "atlas"): "action_view",
    ("view", "settings"): "action_view",
    ("view", "agents"): "action_agent_focus",
    ("agents", None): "action_agent_focus",
    ("symbol", None): "action_symbol",
    ("timeline", None): "action_timeline",
    ("help", None): "action_help",
    ("ask", None): "action_ask",
    ("chat", None): "action_chat_mode",
    ("workforce", None): "action_workforce_new",
    ("workforce", "status"): "action_workforce_status",
    ("workforce", "resume"): "action_workforce_resume",
    ("workforce", "stop"): "action_workforce_stop",
    ("governed", None): "action_workforce_new",
    ("governed", "status"): "action_workforce_status",
    ("governed", "resume"): "action_workforce_resume",
    ("governed", "stop"): "action_workforce_stop",
    ("rebalance", "dry"): "action_rebalance_dry",
    ("rebalance", "paper"): "action_rebalance_paper",
    ("daily", None): "action_daily_ops",
    ("batch", None): "action_batch",
}


def _bulletin_markup(
    lines: list[str],
    *,
    tone: str = TEXT,
    max_len: int = 200,
    strip_ids: bool = True,
) -> list[str]:
    """Rich-markup bullet rows built from the shared plain-text normalizer."""
    return [
        f"[{tone}]• {escape(line)}[/]"
        for line in bulletin(
            lines, max_len=max_len, strip_ids=strip_ids
        )
    ]


# Theme skin for the kinds report_lines() emits. The renderer stays tone-free;
# only this table knows the palette.
_REPORT_TONES = {
    "h1": f"[bold {AMBER}]▍{{}}[/]",
    "h2": f"[bold {TEXT_HI}]{{}}[/]",
    "bullet": f"[{MUTED}]  • [/][{TEXT}]{{}}[/]",
    "code": f"[{DIM}]{{}}[/]",
    "table": f"[{DIM}]{{}}[/]",
    "text": f"[{TEXT}]{{}}[/]",
    "blank": "{}",
}


def _key_number_markup(
    pairs: list[tuple[str, object]],
    *,
    value_tones: list[str] | None = None,
    bold_values: set[int] | None = None,
    values_are_markup: bool = False,
) -> list[str]:
    """Theme the shared aligned key/value rows without embedding colors in them."""
    normalized = [(str(label), str(value)) for label, value in pairs]
    if not normalized:
        return []
    label_width = max(len(label) for label, _value in normalized)
    tones = value_tones or []
    bold = bold_values or set()
    rendered = []
    for index, line in enumerate(key_number_lines(normalized)):
        label = line[:label_width]
        value = line[label_width + 2:]
        tone = tones[index] if index < len(tones) else TEXT_HI
        style = f"bold {tone}" if index in bold else tone
        safe_value = value if values_are_markup else escape(value)
        rendered.append(
            f"[{MUTED}]• {escape(label)}[/]  [{style}]{safe_value}[/]"
        )
    return rendered


def _verdict_style(verdict: dict | None) -> tuple[str, str]:
    """Resolve the formatter's semantic token name through the TUI theme."""
    token_name, text = verdict_chip(verdict)
    return _COLOR_BY_TOKEN.get(token_name, MUTED), text


def workforce_note(phase: str, status: str, summary: str,
                   done_phases: set[str],
                   all_phases: set[str] | None = None) -> tuple[str, str]:
    """The two-line note printed when one agent finishes: done, then next.

    Pure so the wording is testable without a running app. ``done_phases``
    is the set of phases already complete *including* this one; the follow-up
    line is derived from the same dependency graph the registry enforces, so
    the operator is never told a stage is next that the gate would refuse.
    """
    base, branch = _phase_parts(phase)
    short = _phase_short(phase)
    detail = " · ".join(
        bulletin(str(summary or "").splitlines(), max_len=220)
    )[:220]
    did = _PHASE_DID.get(base, "completed its phase")

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
    phase_set = all_phases or done_phases
    if base == "analyst" and branch is not None:
        nxt = (
            "Next: once every analyst stance is in, the branch optimizers run "
            "in parallel on their matching objectives."
        )
    elif base == "optimizer" and branch is not None:
        optimizers = {
            candidate for candidate in phase_set
            if _phase_parts(candidate)[0] == "optimizer"
            and _phase_parts(candidate)[1] is not None
        }
        nxt = (
            "Next: the judge compares every branch on walk-forward evidence."
            if optimizers and optimizers <= done_phases else
            "Next: waiting on the other branch optimizers, then the evidence judge."
        )
    elif base == "judge":
        nxt = (
            "Next: the referee independently gates the judge's exact winning "
            "targets."
        )
    elif base == "analyst":
        nxt = ("Next: the challenger opens the bounded debate on this "
               "estimation call; the optimizer runs after the debate settles "
               "on the final decision.")
    elif base in ("challenger", "optimizer"):
        other = "optimizer" if base == "challenger" else "challenger"
        nxt = (
            "Next: the referee gate, now that both parallel phases are in."
            if other in done_phases else
            f"Next: waiting on the {other} (running in parallel), then the "
            "referee gate."
        )
    elif base == "referee":
        nxt = ("Next: the reporter compiles the recommendation. A PASS is bound "
               "to these exact weights; execution still needs you.")
    else:
        nxt = ("Run complete — the results print below. Any paper trade remains "
               "a separate, explicitly confirmed action.")
    return head, nxt


# The regime a run selected is a first-class read for the operator, so it gets
# its own colored line rather than living only in a hover card.
_REGIME_TONE = {
    "calm": UP,
    "normal": CYAN,
    "stress": DOWN,
    "uncertain": AMBER,
}


def _regime_readout(steps_by_phase: dict) -> tuple[str, str] | None:
    """The analyst's selected regime and its one-line reasoning, or None.

    Sourced from the durable analyst-phase artifacts the agent persists, so the
    operator sees the exact call the run made rather than a re-derived one.
    """
    analyst_phase = "analyst"
    if analyst_phase not in steps_by_phase:
        judge_artifacts = (
            steps_by_phase.get("judge", {}) or {}
        ).get("artifacts") or {}
        winner_phase = str(judge_artifacts.get("winner_phase") or "")
        base, branch = _phase_parts(winner_phase)
        if base != "optimizer" or branch is None:
            return None
        analyst_phase = f"analyst-{branch}"
    artifacts = (
        steps_by_phase.get(analyst_phase, {}) or {}
    ).get("artifacts") or {}
    regime = str(artifacts.get("regime") or "").strip()
    if not regime:
        return None
    reasoning = " · ".join(
        bulletin(
            str(artifacts.get("regime_reasoning") or "").splitlines(),
            max_len=220,
        )
    )
    return regime, reasoning


def _regime_line(steps_by_phase: dict, *, indent: str = "") -> str | None:
    """A ready-to-print rich-markup regime line, coloured calm/stress, or None."""
    readout = _regime_readout(steps_by_phase)
    if readout is None:
        return None
    regime, reasoning = readout
    tone = _REGIME_TONE.get(regime.lower(), AMBER)
    line = f"{indent}[{CYAN}]◆ REGIME[/]  [bold {tone}]{escape(regime.upper())}[/]"
    if reasoning:
        line += f"  [{MUTED}]{escape(reasoning[:220])}[/]"
    return line


def _extract_targets(steps_by_phase: dict) -> dict:
    """The reviewed target weights: the referee's if it re-published them (they
    are then gate-bound), otherwise the optimizer's. Empty when neither ran."""
    for phase, artifact in (
        ("referee", "targets"),
        ("judge", "winning_targets"),
        ("optimizer", "targets"),
    ):
        targets = (
            steps_by_phase.get(phase, {}).get("artifacts") or {}
        ).get(artifact)
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
        "complete": (UP, "WORKFORCE COMPLETE"),
        "blocked": (AMBER, "STOPPED AT A SAFETY GATE"),
        "failed": (DOWN, "STOPPED ON AN ERROR"),
    }.get(status, (GOLD, "ENDED EARLY"))


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
    "judge": ("Judge", "compared the variant results on persisted evidence"),
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
    base, branch = _phase_parts(phase)
    name, action = _PHASE_PERSON.get(base, (phase.title(), "ran its phase"))
    if branch is not None:
        name = f"Variant {branch} {name.lower()}"
    state = str((step or {}).get("status", "idle"))
    glyph, colour = _STATE_STYLE.get(state, ("◌", DIM))
    artifacts = (step or {}).get("artifacts") or {}

    if not step or state in ("idle", "queued", "waiting"):
        return "◌", DIM, name, "did not run"
    if state == "working":
        return glyph, colour, name, "was still running when the run stopped"
    if state in ("failed", "blocked"):
        reason = " · ".join(
            bulletin(
                str(step.get("summary") or "").splitlines(),
                max_len=90,
            )
        )[:90]
        base = ("hit an error and stopped the run" if state == "failed"
                else "was refused by a safety gate")
        return glyph, colour, name, base + (f" — {reason}" if reason else "")

    if base == "optimizer":
        algo = str(artifacts.get("algorithm_id")
                   or artifacts.get("algorithm") or "").strip()
        if algo:
            action = f"computed the target weights using {algo}"
    elif base == "judge":
        winner = str(artifacts.get("winner_phase") or "").strip()
        if winner:
            action = f"compared the variants and selected {winner}"
    elif base == "referee":
        result = str(artifacts.get("verdict") or "").upper()
        if result == "PASS":
            action = "re-checked the result against the mandate and approved it"
        elif result:
            action = ("re-checked the result against the mandate and did not "
                      "approve a trade")
    elif base == "reporter" and artifacts.get("plan_id"):
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
    _token_name, label = verdict_chip(verdict)
    source = str((verdict or {}).get("source", "")).strip()
    return f"{label}·{source}" if verdict and source else label


def _reflection_cell(reflection: str | None) -> str:
    """First ~50 chars of a decision's reflection, 'pending' when unresolved."""
    text = str(reflection or "").strip()
    return text[:50] if text else "pending"


def _book_state_style(state: str) -> tuple[str, str]:
    """Map persisted plan/order states onto the shared workstation state tones."""
    token = _BOOK_STATE_ALIASES.get(state.lower(), state.lower())
    return _STATE_STYLE.get(token, ("◌", DIM))


def _cell(value: Any, fmt: str = "{:.2f}") -> str:
    """Format a leaderboard metric, or an em dash when the metric is absent."""
    return "—" if value is None else fmt.format(value)


def _marks_label(performance: dict[str, Any]) -> str:
    """Mark count, stated as a fraction whenever the owner capped the read."""
    shown = int(performance.get("marks") or 0)
    total = int(performance.get("marks_total") or shown)
    if performance.get("marks_capped") and total > shown:
        return f"{shown:,} of {total:,} marks"
    return f"{shown:,} marks"


def _record(value: Any) -> dict[str, Any]:
    """Return one owner record, or an empty record for a sparse payload."""
    return value if isinstance(value, dict) else {}


def _records(value: Any) -> list[dict[str, Any]]:
    """Drop null or malformed rows from an owner snapshot list."""
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _finite_number(value: Any) -> float | None:
    """JSON numbers are displayable; missing, boolean, and non-finite values are not."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


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

    def on_mount(self) -> None:
        paint = getattr(self.app, "_paint_flow_node", None)
        if paint is not None:
            paint(self)

    def on_enter(self, event: events.Enter) -> None:
        app = self.app
        detail = getattr(app, "_flow_details", {}).get(self.phase) or (
            f"{self.agent}\n\nnot yet started")
        app._set_selected_work(f"{self.short.upper()} · {self.agent}\n\n{detail}")


class FlowBoard(Horizontal):
    """A recomposable workflow-step board; panel branches are real nodes."""

    def __init__(self, flow: tuple[tuple[str, str, str], ...]):
        super().__init__(id="flow-row")
        self.flow = flow

    def compose(self) -> ComposeResult:
        for index, (phase, agent, short) in enumerate(self.flow):
            if index:
                yield Static("→", classes="flow-arrow")
            yield FlowNode(phase, agent, short)

    def set_flow(self, flow: tuple[tuple[str, str, str], ...]) -> None:
        if flow == self.flow:
            return
        self.flow = flow
        if self.is_attached:
            self.run_worker(
                self.recompose(),
                name="recompose-workflow-flow",
                group="workflow-flow",
                exclusive=True,
                exit_on_error=False,
            )


class NavMenu(Static):
    """The eight-view switcher in the spine.

    It renders one text line per view (in ``_VIEWS`` order), so a click selects
    the view on the clicked row — the click's y within the widget *is* the row
    index. Digit and function keys still work; this adds the mouse path a Static
    lacks.
    """

    def on_click(self, event: events.Click) -> None:
        index = int(event.y)
        if 0 <= index < len(_VIEWS):
            self.app.action_view(_VIEWS[index])


class Tile(Vertical):
    """One titled dashboard cell with a stable content target."""

    def __init__(self, title: str, tile_key: str):
        super().__init__(id=f"tile-{tile_key}", classes="dashboard-tile")
        self.tile_title = title
        self.tile_key = tile_key

    def compose(self) -> ComposeResult:
        yield Static(
            self.tile_title.upper(), classes="tile-title", markup=False)
        yield Static(
            id=f"tile-{self.tile_key}-content",
            classes="tile-content",
            markup=True,
        )


class PaperConfirmScreen(ModalScreen[bool]):
    """Explicit confirmation for the only mutating action exposed by v1."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    CSS = PAPER_MODAL_CSS

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


class QlabTui(App[None]):
    """Border-light terminal workspace for portfolio and agent operations."""

    TITLE = "qlab operator"

    CSS = APP_CSS

    BINDINGS = [
        Binding("1", "view('dashboard')", "Dashboard", show=False),
        Binding("2", "view('market')", "Market", show=False),
        Binding("3", "view('workforce')", "Workforce", show=False),
        Binding("4", "view('research')", "Research", show=False),
        Binding("5", "view('book')", "Book", show=False),
        Binding("6", "view('audit')", "Audit", show=False),
        Binding("7", "view('atlas')", "Atlas", show=False),
        Binding("8", "view('settings')", "Settings", show=False),
        Binding("f1", "view('dashboard')", "Dashboard", show=False),
        Binding("f2", "view('market')", "Market", show=False),
        Binding("f3", "view('workforce')", "Workforce", show=False),
        Binding("f4", "view('research')", "Research", show=False),
        Binding("f5", "view('book')", "Book", show=False),
        Binding("f6", "view('audit')", "Audit", show=False),
        Binding("f7", "view('atlas')", "Atlas", show=False),
        Binding("f8", "view('settings')", "Settings", show=False),
        Binding("a", "agent_focus", "Agents", show=False),
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
        self.active_view = "dashboard"
        # Placeholder until the first snapshot; the owner's mandate universe
        # (market.assets) replaces it so a config change never desyncs the TUI.
        self.universe_tickers: list[str] = list(_DEFAULT_TICKERS)
        self.active_ticker = self.universe_tickers[0]
        self.snapshot: dict[str, Any] = {}
        self._refreshing = False
        self._action_running = False
        self._last_snapshot_at: float | None = None
        self._refresh_failures = 0
        self._event_ids: set[str] = set()
        self._runs_signature: tuple = ()
        self._audit_signature: tuple = ()
        self._audit_decisions: dict[str, dict] = {}
        self._book_plan_ids: dict[str, str] = {}
        self.bootstrap: dict[str, Any] | None = None
        self._bootstrap_started = False
        self._bootstrap_error = ""
        self._atlas_started = False
        self._claude_buffer = ""
        self._claude_saw_delta = False
        self._claude_offer_handled = False
        self._pending_plan_id = ""
        self._agent_focus = False
        self._agent_states: dict[str, str] = {}
        # Flowchart state: phase -> state token, and phase -> hover detail.
        self._flow_spec = _FLOW
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
        self._last_quote_repaint_at = 0.0
        self._quote_repaint_timer = None
        self._console_partial = ""
        # Streamed text arrives token by token, so the ``` opener and the code it
        # introduces land in different calls; the console remembers the block.
        self._console_fenced = False
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

            with ContentSwitcher(initial="dashboard", id="canvas"):
                with Vertical(id="dashboard", classes="canvas-view"):
                    yield Static(
                        f"[{AMBER}]\u258d[/] DASHBOARD",
                        classes="canvas-title",
                        markup=True,
                    )
                    with Grid(id="dashboard-grid"):
                        yield Tile("equity", "equity")
                        yield Tile("regime", "regime")
                        yield Tile("allocation", "allocation")
                        yield Tile("market pulse", "market-pulse")
                        yield Tile("verdict", "verdict")
                        yield Tile("run", "run")
                        yield Tile("guardrail alerts", "alerts")
                        yield Tile("scenario replay", "stress")
                    with Horizontal(id="dashboard-actions"):
                        yield Button(
                            "DRY REBALANCE",
                            id="btn-rebalance-dry",
                            classes="view-action-button",
                            compact=True,
                        )
                        yield Button(
                            "DAILY OPS",
                            id="btn-daily-ops",
                            classes="view-action-button",
                            compact=True,
                        )
                        yield Button(
                            "BATCH",
                            id="btn-batch",
                            classes="view-action-button",
                            compact=True,
                        )
                with Vertical(id="market", classes="canvas-view"):
                    yield Static(f"[{AMBER}]\u258d[/] MARKET", classes="canvas-title", markup=True)
                    yield Static(id="market-content", markup=True)
                with Vertical(id="workforce", classes="canvas-view"):
                    yield Static(f"[{AMBER}]\u258d[/] WORKFORCE", classes="canvas-title", markup=True)
                    yield Static(id="workforce-content", markup=True)
                    yield FlowBoard(self._flow_spec)
                    yield RichLog(id="workforce-console", wrap=True,
                                  markup=True, max_lines=400)
                    with Horizontal(id="chat-row"):
                        yield Static(id="chat-mode", markup=True)
                        yield Input(
                            placeholder="message the coordinator — Enter sends",
                            id="chat-input")
                        yield Button(
                            "NEW REVIEW",
                            id="btn-workforce-new",
                            classes="view-action-button",
                            compact=True,
                        )
                        yield Button(
                            "RESUME LAST",
                            id="btn-workforce-resume",
                            classes="view-action-button",
                            disabled=True,
                            compact=True,
                        )
                        yield Button("exit", id="chat-exit")
                with Vertical(id="research", classes="canvas-view"):
                    yield Static(f"[{AMBER}]\u258d[/] RESEARCH", classes="canvas-title", markup=True)
                    yield Static(id="research-summary", markup=True)
                    yield Static("ABLATION LEADERBOARD", classes="book-section-title")
                    yield Static(id="leaderboard", classes="book-section", markup=True)
                    yield DataTable(id="runs-table", cursor_type="row")
                with Vertical(id="book", classes="canvas-view"):
                    yield Static(f"[{AMBER}]\u258d[/] BOOK", classes="canvas-title", markup=True)
                    yield Static("EQUITY", classes="book-section-title")
                    yield Static(
                        id="book-equity",
                        classes="book-section",
                        markup=True,
                    )
                    yield Static("POSITIONS", classes="book-section-title")
                    yield Static(
                        id="book-positions",
                        classes="book-section",
                        markup=True,
                    )
                    yield Static("PLANS · NEWEST 5", classes="book-section-title")
                    with Vertical(id="book-plans"):
                        for slot in range(5):
                            with Horizontal(
                                id=f"book-plan-{slot}",
                                classes="book-plan-card",
                            ):
                                yield Static(
                                    id=f"book-plan-copy-{slot}",
                                    classes="book-plan-copy",
                                    markup=True,
                                )
                                yield Button(
                                    "execute",
                                    id=f"execute-plan-{slot}",
                                    classes="view-action-button book-execute-button",
                                    disabled=True,
                                    compact=True,
                                )
                    yield Static("ORDERS · NEWEST 10", classes="book-section-title")
                    yield Static(
                        id="book-orders",
                        classes="book-section",
                        markup=True,
                    )
                with Vertical(id="audit", classes="canvas-view"):
                    yield Static(f"[{AMBER}]\u258d[/] AUDIT", classes="canvas-title", markup=True)
                    yield Static(id="audit-summary", markup=True)
                    yield DataTable(id="audit-table", cursor_type="row")
                yield AtlasView(id="atlas", classes="canvas-view")
                with Vertical(id="settings", classes="canvas-view"):
                    yield Static(f"[{AMBER}]\u258d[/] SETTINGS", classes="canvas-title", markup=True)
                    yield Static(
                        id="settings-mandate",
                        classes="settings-card",
                        markup=True,
                    )
                    yield Static(
                        id="settings-data",
                        classes="settings-card",
                        markup=True,
                    )
                    yield Static(
                        id="settings-agents",
                        classes="settings-card",
                        markup=True,
                    )
                    yield Static(
                        id="settings-theme",
                        classes="settings-card",
                        markup=True,
                    )

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
            yield Static("CONNECTING", id="conn-chip", markup=True)
            yield Static("PAPER · CONNECTING", id="system-status")

    def on_mount(self) -> None:
        self.query_one("#runs-table", DataTable).add_columns("run", "kind", "created")
        self.query_one("#audit-table", DataTable).add_columns(
            "time", "object", "state", "verdict", "reflection", "detail")
        universe = self.query_one("#universe", ListView)
        universe.index = 0
        self._console_write(
            f"[{LABEL_GOLD}]workforce — type a goal below and the coordinator runs the "
            "five governed roles autonomously. Watch the flowchart above; hover a "
            "node for its live update. [bold]■ stop[/] interrupts; durable state "
            "survives.[/]")
        self._render_chat_mode()
        self._render_flow()
        self.query_one("#audit-table", DataTable).zebra_stripes = True
        self.query_one("#runs-table", DataTable).zebra_stripes = True
        self._render_nav()
        self._render_book()
        self._render_settings()
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
        if self.snapshot and self.active_view in ("market", "dashboard"):
            if self.active_view == "market":
                self._render_market()
            else:
                self._render_dashboard()

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
                self.call_from_thread(self._note_refresh_failure)
            finally:
                self.call_from_thread(self._finish_refresh)

        threading.Thread(target=run, daemon=True).start()

    def _finish_refresh(self) -> None:
        self._refreshing = False

    def _note_refresh_failure(self) -> None:
        self._refresh_failures += 1
        self._render_conn_chip()

    def _render_conn_chip(self) -> None:
        age = (None if self._last_snapshot_at is None
               else time.monotonic() - self._last_snapshot_at)
        text, level = connection_chip(age, self._refresh_failures)
        tone = {"ok": UP, "warn": AMBER, "down": DOWN}[level]
        self.query_one("#conn-chip", Static).update(f"[{tone}]{text}[/]")

    def _start_bootstrap(self) -> None:
        """Fetch immutable owner configuration once, when Settings is first shown."""
        if self._bootstrap_started:
            return
        self._bootstrap_started = True
        self._render_settings()

        def run() -> None:
            try:
                bootstrap = self.client.get("/api/bootstrap")
                self.call_from_thread(self._finish_bootstrap, bootstrap, "")
            except Exception as exc:
                self.call_from_thread(
                    self._finish_bootstrap, None, repr(exc))

        threading.Thread(target=run, daemon=True).start()

    def _finish_bootstrap(
        self,
        bootstrap: dict[str, Any] | None,
        error: str,
    ) -> None:
        self.bootstrap = bootstrap
        self._bootstrap_error = error
        self._render_settings()

    def _start_atlas_fetch(self) -> None:
        """Fetch the curated catalog once, when Atlas is first shown."""
        if self._atlas_started:
            return
        self._atlas_started = True

        def run() -> None:
            try:
                payload = self.client.get("/api/atlas")
                self.call_from_thread(self._finish_atlas, payload, "")
            except Exception as exc:
                self.call_from_thread(self._finish_atlas, None, repr(exc))

        threading.Thread(target=run, daemon=True).start()

    def _finish_atlas(self, payload: dict[str, Any] | None, error: str) -> None:
        view = self.query_one("#atlas", AtlasView)
        if payload is None:
            self.query_one("#atlas-detail", Static).update(
                f"[{DOWN}]atlas unavailable: {escape(error)}[/]")
            self._atlas_started = False  # allow retry on the next visit
            return
        view.set_entries(payload.get("entries") or [])

    def _start_live_stream(self) -> None:
        """Subscribe to the owner's SSE bus so state and quotes land instantly.

        Polling stays on as the fallback; this only makes the desk react the
        moment a phase flips or a verdict lands instead of at the next tick.
        """
        if not hasattr(self.client, "stream"):
            return

        def run() -> None:
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
        kind = str(event.get("kind", ""))
        if kind == "quote":
            self._apply_quote_event(event)
            return
        # Console notes are raised by _ingest_events, which both the SSE stream
        # and the snapshot poll feed — whichever delivers a phase event first
        # writes its note, and the other is deduped by id.
        self._ingest_events([event])
        if kind in _REFRESH_EVENT_KINDS:
            self._start_refresh()

    def _apply_quote_event(self, event: dict) -> None:
        """Merge quote rows into the local view model without a snapshot fetch."""
        payload = event.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
            raise ValueError("quote event payload.rows must be a list")
        rows = payload["rows"]
        normalized = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("quote event rows must be objects")
            ticker = str(row.get("ticker") or "")
            price = _finite_number(row.get("price"))
            change = _finite_number(row.get("change_1d"))
            if not ticker or price is None or change is None:
                raise ValueError(
                    "quote event rows require ticker, price, and change_1d"
                )
            normalized.append({
                "ticker": ticker,
                "price": price,
                "change_1d": change,
            })
        if not normalized:
            raise ValueError("quote event payload.rows must not be empty")

        if not self.snapshot:
            self.snapshot = {"market": {"assets": []}}
        market = self.snapshot.get("market")
        if not isinstance(market, dict):
            market = {}
            self.snapshot["market"] = market
        assets = _records(market.get("assets"))
        by_ticker = {
            str(asset.get("ticker") or ""): asset
            for asset in assets
            if asset.get("ticker")
        }
        for row in normalized:
            asset = by_ticker.get(row["ticker"])
            if asset is None:
                asset = {"ticker": row["ticker"], "history": []}
                assets.append(asset)
                by_ticker[row["ticker"]] = asset
            asset.update(row)
        market["assets"] = assets
        self._queue_quote_repaint()

    def _queue_quote_repaint(self) -> None:
        if self._quote_repaint_timer is not None:
            return
        now = time.monotonic()
        elapsed = now - self._last_quote_repaint_at
        if elapsed >= _QUOTE_REPAINT_INTERVAL:
            self._repaint_quote_surfaces()
            return
        self._quote_repaint_timer = self.set_timer(
            _QUOTE_REPAINT_INTERVAL - elapsed,
            self._repaint_quote_surfaces,
        )

    def _repaint_quote_surfaces(self) -> None:
        self._quote_repaint_timer = None
        self._last_quote_repaint_at = time.monotonic()
        market = _record(self.snapshot.get("market"))
        assets = _records(market.get("assets"))
        self._sync_universe([
            str(row.get("ticker"))
            for row in assets
            if row.get("ticker")
        ])
        self._render_universe(assets)
        self.query_one("#tile-market-pulse-content", Static).update(
            self._market_pulse_content(market)
        )

    # -- workforce console -------------------------------------------------
    def _console_write(self, line: str) -> None:
        self.query_one("#workforce-console", RichLog).write(line)

    def _render_chat_mode(self) -> None:
        chip = self.query_one("#chat-mode", Static)
        if self._chat_mode == "chat":
            chip.update(f"[bold {CYAN}]CHAT[/]")
        else:
            chip.update(f"[bold {AMBER}]WORK[/]")
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
        if _phase_parts(phase)[0] not in _PHASE_DID:
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
                f"[{CYAN}]▶ {escape(_phase_short(phase))}[/] "
                f"[{LABEL_GOLD}]working[/]")
            return
        if status not in ("done", "failed", "blocked"):
            return
        done = {name for name, state in self._flow_states.items() if state == "done"}
        if status == "done":
            done.add(phase)
        head, nxt = workforce_note(
            phase,
            status,
            str(payload.get("summary") or ""),
            done,
            {candidate for candidate, _agent, _short in self._flow_spec},
        )
        glyph, tone = {
            "done": ("✓", UP),
            "failed": ("×", DOWN),
            "blocked": ("!", AMBER),
        }[status]
        for line in bulletin([head], max_len=260):
            self._console_write(f"[{tone}]{glyph} {escape(line)}[/]")
        for line in bulletin([nxt], max_len=260):
            self._console_write(f"[{MUTED}]  • {escape(line)}[/]")

    def _console_report(self, lines: list[str]) -> None:
        """Write agent-report markdown as styled console rows.

        A report is a document, not a list of facts: headers become section
        titles, bullets align, tables and code pass through verbatim. An ordinal
        marker ("1.") stands in for the bullet glyph; a bullet that merely opens
        with a count keeps it. Carries `_console_fenced` across calls, because a
        code block spans as many calls as the stream has tokens.
        """
        fenced = self._console_fenced
        for kind, text in report_lines(lines, fenced=fenced):
            if kind == "bullet" and is_numbered_item(text):
                self._console_write(f"[{MUTED}]  [/][{TEXT}]{escape(text)}[/]")
            else:
                self._console_write(_REPORT_TONES[kind].format(escape(text)))
        self._console_fenced = fence_state_after(lines, fenced)

    def _console_stream_text(self, text: str) -> None:
        """Append streamed narrative, emitting only completed lines."""
        self._console_partial += text
        *complete, self._console_partial = self._console_partial.split("\n")
        self._console_report(complete)

    def _console_flush(self) -> None:
        """Emit the trailing partial line and end the block it belonged to.

        A turn that stops mid-fence must not leave the flag set: the next report
        would render entirely as code.
        """
        self._console_report([self._console_partial])
        self._console_partial = ""
        self._console_fenced = False

    def _tick_pulse(self) -> None:
        """Animate working-state glyphs only while something is running."""
        self._pulse += 1
        self._render_conn_chip()
        states = set(self._agent_states.values())
        workflows = self.snapshot.get("workflows", []) if self.snapshot else []
        running = workflows and workflows[0].get("status") == "running"
        if "working" in states or running or self.claude.running:
            self._render_agents()
            self._render_flow()
            if self.active_view == "workforce":
                self._render_workforce()

    def _apply_snapshot(self, snapshot: dict) -> None:
        self._last_snapshot_at = time.monotonic()
        self._refresh_failures = 0
        self._render_conn_chip()
        self.snapshot = snapshot
        assets = snapshot.get("market", {}).get("assets", [])
        self._sync_universe([row["ticker"] for row in assets])
        if assets and self.active_ticker not in {row["ticker"] for row in assets}:
            self.active_ticker = assets[0]["ticker"]
        self._render_nav()
        self._render_universe(assets)
        self._render_dashboard()
        self._render_market()
        self._render_workforce()
        self._render_research()
        self._render_book()
        self._render_audit()
        self._render_settings()
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
        lines = []
        for index, view in enumerate(_VIEWS, start=1):
            if view == self.active_view:
                lines.append(
                    f"[bold {TEXT_HI}]› {index}  {view.title()}[/]")
            else:
                lines.append(f"[{MUTED}]  {index}  {view.title()}[/]")
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

    def _market_pulse_content(self, market: dict) -> str:
        pulse_lines = []
        assets_by_ticker = {}
        for asset in _records(market.get("assets")):
            ticker = str(asset.get("ticker") or "")
            if ticker:
                assets_by_ticker[ticker] = asset
        for ticker in self.universe_tickers:
            asset = assets_by_ticker.get(ticker)
            safe_ticker = escape(str(ticker))
            if asset is None:
                pulse_lines.append(
                    f"[{TEXT}]{safe_ticker:<5}[/] [{MUTED}]no data[/]")
                continue
            price = _finite_number(asset.get("price"))
            price_text = "—" if price is None else f"{price:.2f}"
            change = _finite_number(asset.get("change_1d"))
            change_cell = (
                f"[{MUTED}]{'—':>6}[/]"
                if change is None
                else f"[{UP if change >= 0 else DOWN}]{change:+6.1%}[/]"
            )
            history = []
            raw_history = asset.get("history")
            if isinstance(raw_history, list):
                for value in raw_history:
                    number = _finite_number(value)
                    if number is not None:
                        history.append(number)
            pulse = sparkline(history[-12:]) or "—"
            pulse_lines.append(
                f"[{TEXT}]{safe_ticker:<5}[/] "
                f"[{TEXT_HI}]{price_text:>8}[/]  "
                f"{change_cell}  [{CYAN}]{pulse}[/]"
            )
        return "\n".join(pulse_lines) if pulse_lines else f"[{MUTED}]—[/]"

    def _update_dashboard_tiles(self, contents: dict[str, str]) -> None:
        unavailable = f"[{MUTED}]owner snapshot unavailable[/]"
        for tile_key in _DASHBOARD_TILE_KEYS:
            self.query_one(
                f"#tile-{tile_key}-content", Static
            ).update(contents.get(tile_key, unavailable))

    def _render_dashboard(self) -> None:
        if not self.snapshot:
            self._update_dashboard_tiles({})
            return
        portfolio = _record(self.snapshot.get("portfolio"))
        market = _record(self.snapshot.get("market"))
        regime = _record(market.get("regime"))
        stress = _record(self.snapshot.get("stress"))
        equilibrium = _record(self.snapshot.get("equilibrium_returns"))
        current = _record(portfolio.get("weights"))
        targets = _record(portfolio.get("target_weights"))

        equity = _finite_number(portfolio.get("equity"))
        cash = _finite_number(portfolio.get("cash"))
        drawdown = _finite_number(portfolio.get("drawdown"))
        kill_at = _finite_number(portfolio.get("kill_switch_at"))
        kill_distance = (
            kill_at - drawdown
            if kill_at is not None and drawdown is not None
            else None
        )
        drawdown_tone = (
            MUTED if drawdown is None else DOWN if drawdown > 0 else UP
        )
        distance_tone = (
            MUTED if kill_distance is None else UP if kill_distance > 0 else DOWN
        )
        equity_content = "\n".join(_key_number_markup(
            [
                ("EQUITY", money(equity)),
                ("CASH", money(cash)),
                ("DRAWDOWN", pct(drawdown)),
                ("KILL-SWITCH DISTANCE", pct(kill_distance)),
            ],
            value_tones=[TEXT_HI, TEXT_HI, drawdown_tone, distance_tone],
            bold_values={0, 3},
        ))

        def allocation_bar(value: float, target: float | None) -> str:
            raw = weight_bar(value, width=10)
            filled = raw.rstrip("░")
            track = raw[len(filled):]
            if target is not None and value > target:
                target_fill = len(weight_bar(target, width=10).rstrip("░"))
                target_fill = min(len(filled), target_fill)
                return (
                    f"[{AMBER}]{filled[:target_fill]}[/]"
                    f"[{DOWN}]{filled[target_fill:]}[/]"
                    f"[{ALLOCATION_TRACK}]{track}[/]"
                )
            return f"[{AMBER}]{filled}[/][{ALLOCATION_TRACK}]{track}[/]"

        allocation_lines = [f"[{DIM}]      CURRENT        TARGET[/]"]
        held_outside = []
        for ticker, raw_value in current.items():
            value = _finite_number(raw_value)
            if (
                ticker not in self.universe_tickers
                and value is not None
                and abs(value) > 0.0005
            ):
                held_outside.append(ticker)
        for ticker in [*self.universe_tickers, *held_outside]:
            value = _finite_number(current.get(ticker))
            target = _finite_number(targets.get(ticker))
            bar = (
                f"[{MUTED}]{'—':^10}[/]"
                if value is None
                else allocation_bar(value, target)
            )
            value_text = "—" if value is None else f"{value:.1%}"
            target_text = "—" if target is None else f"{target:.1%}"
            allocation_lines.append(
                f"[{TEXT}]{escape(str(ticker)):<5}[/] "
                f"{bar}  "
                f"[{TEXT_HI}]{value_text:>5}[/] → [{TEXT}]{target_text:>5}[/]"
            )
        if len(allocation_lines) == 1:
            allocation_lines.append(f"[{MUTED}]—[/]")
        allocation_content = "\n".join(allocation_lines)

        regime_name = str(
            regime.get("robust_state") or regime.get("regime") or "—"
        ).upper()
        source = str(market.get("source") or "—").upper()
        age = market.get("bar_age_days")
        age_text = "—" if age is None else f"{age}d"
        regime_pairs = [
            ("REGIME", regime_name),
            (
                "SIGNAL / THRESHOLD",
                f"{pct(_finite_number(regime.get('signal')))} / "
                f"{pct(_finite_number(regime.get('threshold')))}",
            ),
            ("SOURCE / BAR AGE", f"{source} / {age_text}"),
        ]
        regime_tones = [
            _REGIME_TONE.get(regime_name.lower(), TEXT_HI),
            TEXT,
            TEXT,
        ]
        equilibrium_portfolio = _record(equilibrium.get("portfolio"))
        equilibrium_lo = _finite_number(equilibrium_portfolio.get("lo"))
        equilibrium_hi = _finite_number(equilibrium_portfolio.get("hi"))
        if equilibrium_lo is not None and equilibrium_hi is not None:
            regime_pairs.append((
                "EQ RETURN (1Y)",
                f"{pct(equilibrium_lo)}–{pct(equilibrium_hi)}",
            ))
            regime_tones.append(CYAN)
        regime_content = "\n".join(_key_number_markup(
            regime_pairs,
            value_tones=regime_tones,
            bold_values={0},
        ))
        posterior = _record(regime.get("posterior"))
        if posterior:
            selected = regime_name.lower()
            posterior_parts = []
            for state in ("calm", "normal", "stress"):
                probability = _finite_number(posterior.get(state))
                if probability is None:
                    continue
                token = f"{state} {round(probability * 100):.0f}"
                if selected == state:
                    posterior_parts.append(
                        f"[bold {_REGIME_TONE[state]}]{token}[/]"
                    )
                else:
                    posterior_parts.append(f"[{MUTED}]{token}[/]")
            if posterior_parts:
                state_tone = _REGIME_TONE.get(selected, AMBER)
                posterior_parts.append(
                    f"[bold {state_tone}]{escape(regime_name)}[/]"
                )
                regime_content += "\n" + " · ".join(posterior_parts)

        market_pulse_content = self._market_pulse_content(market)

        decisions = _records(self.snapshot.get("decisions"))
        decision = decisions[0] if decisions else {}
        verdict = _record(decision.get("verdict"))
        verdict_tone, verdict_text = _verdict_style(verdict or None)
        verdict_lines = [
            f"[bold {verdict_tone}]▮ {escape(verdict_text)}[/]",
        ]
        if verdict:
            verdict_lines.extend(_bulletin_markup(
                str(decision.get("rationale") or "—").splitlines(),
                max_len=180,
            ))
        else:
            verdict_lines.extend(
                _bulletin_markup(["no verdicts yet"], tone=MUTED)
            )
        verdict_content = "\n".join(verdict_lines)

        workflows = _records(self.snapshot.get("workflows"))
        if workflows:
            workflow = workflows[0]
            workflow_id = str(workflow.get("workflow_id") or "—")
            status = str(workflow.get("status") or "—")
            phase = str(workflow.get("current_phase") or "—")
            phase_step = next((
                step for step in _records(workflow.get("steps"))
                if str(step.get("phase", "")) == phase
            ), {})
            state = str(phase_step.get("status") or {
                "running": "working",
                "complete": "done",
            }.get(status, status))
            glyph, state_tone = _STATE_STYLE.get(state, ("◌", DIM))
            run_content = "\n".join(_key_number_markup(
                [
                    ("RUN", workflow_id),
                    ("STATUS", status.upper()),
                    ("PHASE", f"{glyph} {phase.upper()}"),
                ],
                value_tones=[TEXT_HI, state_tone, state_tone],
                bold_values={0},
            ))
        else:
            run_content = f"[{MUTED}]no runs[/]"

        alerts_content = f"[{MUTED}]no alerts[/]"
        stress_replay_content = f"[{MUTED}]no replay data[/]"
        if stress:
            tier_name = str(stress.get("drawdown_tier") or "—").upper()
            tier_tone = {
                "NONE": UP,
                "WARNING": AMBER_HI,
                "CONTROL": DOWN,
                "BREAKER": DOWN,
            }.get(tier_name, MUTED)
            headroom = _finite_number(stress.get("leverage_headroom"))
            stressed_vol = _finite_number(stress.get("stressed_vol"))
            stress_limit = _finite_number(stress.get("stress_vol_limit"))
            headroom_tone = (
                MUTED if headroom is None else UP if headroom >= 0 else DOWN
            )
            vol_tone = (
                MUTED
                if stressed_vol is None or stress_limit is None
                else DOWN
                if stressed_vol > stress_limit
                else UP
            )
            stress_pairs: list[tuple[str, object]] = [
                ("DRAWDOWN TIER", tier_name),
                ("LEVERAGE HEADROOM", pct(headroom)),
                (
                    "STRESSED VOL / LIMIT",
                    f"{pct(stressed_vol)} / {pct(stress_limit)}",
                ),
            ]
            stress_tones = [tier_tone, headroom_tone, vol_tone]

            replays = _record(stress.get("replays"))
            replay_pairs: list[tuple[str, object]] = []
            replay_tones: list[str] = []
            for label in ("2008", "2020", "2022"):
                replay = _record(replays.get(label))
                replay_return = _finite_number(replay.get("return"))
                available = replay.get("available") is True
                if available and replay_return is not None:
                    replay_text = pct(replay_return)
                    replay_tone = UP if replay_return >= 0 else DOWN
                else:
                    reason = str(replay.get("reason") or "").lower()
                    replay_text = (
                        "unavailable (synthetic)"
                        if "synthetic" in reason
                        else "unavailable"
                    )
                    replay_tone = MUTED
                replay_pairs.append((f"{label} REPLAY", replay_text))
                replay_tones.append(replay_tone)

            refusals = _records(stress.get("cost_gate_refusals"))
            if refusals:
                raw_reasons = refusals[0].get("reasons")
                clean_reasons = bulletin(
                    [str(reason) for reason in raw_reasons]
                    if isinstance(raw_reasons, list)
                    else [],
                    max_len=90,
                )
                refusal_text = (
                    f"REFUSED · {clean_reasons[0]}"
                    if clean_reasons
                    else "REFUSED"
                )
                refusal_tone = DOWN
            else:
                refusal_text = "clear · no recent refusals"
                refusal_tone = UP
            stress_pairs.append(("COST GATE", refusal_text))
            stress_tones.append(refusal_tone)
            alerts_content = "\n".join(_key_number_markup(
                stress_pairs,
                value_tones=stress_tones,
                bold_values={0, 2},
            ))
            stress_replay_content = "\n".join(_key_number_markup(
                replay_pairs,
                value_tones=replay_tones,
            ))

        contents = {
            "equity": equity_content,
            "allocation": allocation_content,
            "regime": regime_content,
            "market-pulse": market_pulse_content,
            "verdict": verdict_content,
            "run": run_content,
            "alerts": alerts_content,
            "stress": stress_replay_content,
        }
        self._update_dashboard_tiles(contents)

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
            f"[bold {TEXT_HI}]{escape(self.active_ticker)}[/]  "
            f"[bold {TEXT_HI}]{money(row.get('price'))}[/]  "
            f"[{dir_col}]{'▲' if up else '▼'} {pct(row.get('change_1d'))} today[/]"
            f"    [{LABEL_GOLD}]HIGH[/] [{TEXT}]{money(hi)}[/]  "
            f"[{LABEL_GOLD}]LOW[/] [{TEXT}]{money(lo)}[/]  "
            f"[{LABEL_GOLD}]{len(history)} DAILY BARS[/]",
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
                f"[{LABEL_GOLD}]{tick:>{gutter}}[/] [{CHART_AXIS}]│[/]"
                f"[{dir_col}]{escape(bar)}[/]")
        # X axis (time): a baseline under the plot, then the oldest→latest span,
        # then a one-line legend naming both axes so the plot is self-defining.
        pad = " " * (gutter + 2)
        lines.append(f"[{CHART_AXIS}]{' ' * gutter} └{'─' * chart_w}[/]")
        left_lbl = f"{len(history)} bars ago"
        right_lbl = f"as of {as_of}"
        gap = chart_w - len(left_lbl) - len(right_lbl)
        span = (left_lbl + " " * gap + right_lbl if gap >= 1
                else f"{len(history)} bars → {as_of}"[:chart_w])
        lines.append(f"[{LABEL_GOLD}]{pad}{escape(span)}[/]")
        lines.append(
            f"[{DIM}]{pad}X · time (daily bars, oldest → latest)"
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
                f"[{LABEL_GOLD}]{escape(label):<18}[/][{TEXT_HI}]{escape(str(value)):<16}[/]"
                for label, value in stats[i:i + per_row]
            ]
            lines.append("  ".join(cells))
        lines.append(
            f"[{LABEL_GOLD}]Daily adjusted-close context; this is not a streaming quote.[/]")
        self.query_one("#market-content", Static).update("\n".join(lines))

    def _set_flow_spec(
        self,
        flow: tuple[tuple[str, str, str], ...],
    ) -> None:
        """Switch the board to the selected workflow's actual step instances."""
        self._flow_spec = flow or _FLOW
        try:
            self.query_one("#flow-row", FlowBoard).set_flow(self._flow_spec)
        except Exception:
            pass

    def _paint_flow_node(self, node: FlowNode) -> None:
        state = self._flow_states.get(node.phase, "idle")
        glyph, color = _STATE_STYLE.get(state, ("◌", DIM))
        if state == "working":
            glyph = _PULSE_FRAMES[self._pulse % len(_PULSE_FRAMES)]
        node.update(
            f"[bold]{escape(node.short)}[/]\n"
            f"[{color}]{glyph} {escape(state)}[/]"
        )
        for token in ("working", "queued", "done", "failed", "blocked"):
            node.set_class(token == state, f"-{token}")
        node.tooltip = self._flow_details.get(node.phase) or (
            f"{node.agent}\n\nnot yet started"
        )

    def _render_flow(self) -> None:
        """Paint the dynamic workflow-step board from durable phase state."""
        self._set_flow_spec(self._flow_spec)
        for node in self.query(FlowNode):
            self._paint_flow_node(node)

    def _flow_detail(self, agent: str, phase: str, step: dict) -> str:
        """A phase's hover card: agent, state, elapsed, summary, artifacts."""
        state = str(step.get("status", "queued"))
        elapsed = phase_elapsed(step.get("started_at"), step.get("completed_at"))
        head = f"{agent} · {phase}\nstate {state}" + (
            f" · {elapsed}" if elapsed else "")
        summary = bulletin(
            str(step.get("summary") or "").splitlines(),
            max_len=400,
        )
        if summary:
            head += "\n\n" + "\n".join(f"• {line}" for line in summary)
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
        resumable_id = self._latest_resumable_workflow_id()
        resume_button = self.query_one("#btn-workforce-resume", Button)
        resume_button.disabled = not bool(resumable_id)
        resume_button.tooltip = (
            f"Resume {resumable_id}"
            if resumable_id else
            "No incomplete workforce review to resume"
        )
        workflow = self._select_workflow(workflows)
        if workflow is None:
            if self._pending_workflow:
                empty = (f"[{LABEL_GOLD}]STARTING RUN[/]   "
                         f"[{MUTED}]the coordinator is opening a durable workflow — "
                         "phases appear here as they register.[/]")
            elif self._active_workflow_id:
                empty = (f"[{LABEL_GOLD}]RESUMING[/]   "
                         f"[{MUTED}]{escape(self._active_workflow_id)} is outside "
                         "the recent-run window; its phases appear as they "
                         "advance.[/]")
            else:
                empty = (f"[{LABEL_GOLD}]NO DURABLE RUN[/]   "
                         f"[{MUTED}]type a goal below — Claude runs analyst → "
                         "challenger → optimizer → referee → reporter autonomously "
                         "and makes its own best-estimate calls. Hover a node above "
                         "for its live update.[/]")
            self.query_one("#workforce-content", Static).update(empty)
            # A pending launch already reset every node to 'queued'; only an
            # idle desk falls back to 'idle'.
            if not self._pending_workflow and not self.claude.running \
                    and not self._action_running:
                self._set_flow_spec(_FLOW)
                self._flow_states = {phase: "idle" for phase, _, _ in _FLOW}
                self._flow_details = {}
            self._render_flow()
            return

        request = workflow.get("request") or {}
        status = str(workflow.get("status", "unknown"))
        steps = [
            step for step in (workflow.get("steps") or [])
            if isinstance(step, dict)
        ]
        step_by_phase = {str(step.get("phase")): step for step in steps}
        flow = _flow_from_steps(
            steps,
            standard_fallback=str(workflow.get("kind") or "") != "panel",
        )
        self._set_flow_spec(flow)

        # Rebuild flow state/detail from durable steps; where a phase has no
        # step yet, keep a live 'working' the tool stream set, else queue it.
        prior_states = self._flow_states
        flow_states: dict[str, str] = {}
        flow_details: dict[str, str] = {}
        for phase, agent, _short in flow:
            step = step_by_phase.get(phase)
            if step is not None:
                durable = str(step.get("status", "queued"))
                # The agent stream sees a worker start before that worker gets
                # its `working` update persisted; don't drop back to queued.
                live_working = (durable == "queued"
                                and prior_states.get(phase) == "working")
                flow_states[phase] = "working" if live_working else durable
                flow_details[phase] = self._flow_detail(agent, phase, step)
            else:
                live = prior_states.get(phase)
                flow_states[phase] = (
                    "working" if live == "working"
                    else "queued" if status == "running" else "idle")
                flow_details[phase] = (
                    f"{agent} · {phase}\n\nnot yet started"
                )
        self._flow_states = flow_states
        self._flow_details = flow_details
        self._render_flow()

        lines = [
            f"[bold {TEXT_HI}]{escape(str(workflow.get('workflow_id', '—')))}[/]   "
            f"{escape(status.upper())}",
            f"[{LABEL_GOLD}]{escape(str(workflow.get('kind', 'portfolio_review')))} · "
            f"as of {escape(str(request.get('as_of', '—')))} · "
            f"{escape(str(request.get('universe', 'core')))}[/]",
            f"[{MUTED}]{escape(str(request.get('goal', 'Governed portfolio review'))[:160])}[/]",
        ]

        # The regime the analyst selected, as soon as it persists — the operator
        # reads what drove the window/shrinkage call without opening a hover.
        regime_line = _regime_line(step_by_phase)
        if regime_line:
            lines.append(regime_line)

        result = workflow.get("result") or {}
        if status == "complete" and result.get("final_summary"):
            referee = step_by_phase.get("referee")
            raw_verdict = ((referee or {}).get("artifacts") or {}).get("verdict")
            verdict_record = (
                raw_verdict if isinstance(raw_verdict, dict)
                else {"verdict": raw_verdict} if raw_verdict else None
            )
            verdict_tone, verdict_text = _verdict_style(verdict_record)
            lines.extend([
                "",
                f"[{verdict_tone}]▮ RESULT  ·  referee "
                f"{escape(verdict_text)}[/]",
            ])
            lines.extend(_bulletin_markup(
                str(result["final_summary"]).splitlines(),
                max_len=320,
                strip_ids=False,
            ))
            result_pairs = []
            targets = _extract_targets(step_by_phase)
            if targets:
                result_pairs.append(("target weights", _format_targets(targets)))
            plan_id = str((step_by_phase.get("reporter", {}).get("artifacts")
                           or {}).get("plan_id") or "")
            if plan_id:
                result_pairs.append((
                    "checked plan",
                    f"{plan_id} → : rebalance paper to confirm",
                ))
            lines.extend(_key_number_markup(result_pairs))
        elif status in ("failed", "blocked"):
            tone = DOWN if status == "failed" else AMBER
            broken = next(
                (s for s in steps if s.get("status") in ("failed", "blocked")),
                None)
            why = str((broken or {}).get("summary") or "").strip()
            lines.extend([
                "",
                f"[{tone}]▮ {status.upper()} at "
                f"{escape(str((broken or {}).get('phase', '—')))}[/]",
            ])
            lines.extend(_bulletin_markup(
                why.splitlines(),
                tone=MUTED,
                max_len=200,
                strip_ids=False,
            ))
            lines.append(
                f"[{LABEL_GOLD}]Resume with : workforce resume "
                f"{escape(str(workflow.get('workflow_id', '')))}[/]"
            )

        earlier = [row for row in workflows
                   if str(row.get("workflow_id", "")) != self._active_workflow_id
                   and row is not workflow][:4]
        if earlier:
            packed = "   ".join(
                f"{escape(str(row.get('workflow_id', '—')))} "
                f"[{MUTED}]{escape(str(row.get('status', '')))}[/]"
                for row in earlier)
            lines.append(
                f"[{LABEL_GOLD}]earlier (: workforce resume ID)[/]  {packed}")
        self.query_one("#workforce-content", Static).update("\n".join(lines))

    def _latest_resumable_workflow_id(self) -> str:
        workflows = self.snapshot.get("workflows", []) if self.snapshot else []
        for workflow in workflows:
            if str(workflow.get("status", "")).lower() == "complete":
                continue
            workflow_id = str(workflow.get("workflow_id", "")).strip()
            if workflow_id:
                return workflow_id
        return ""

    def _maybe_offer_workforce(self) -> None:
        if self._claude_offer_handled:
            return
        if self.claude_start != "auto":
            self._claude_offer_handled = True
            return
        # A first snapshot can race runtime readiness. Auto mode waits for a
        # ready snapshot; offer mode is status-only and never interrupts.
        if not bool(self.snapshot.get("system", {}).get("workforce_available")):
            return
        self._claude_offer_handled = True
        self._start_workforce("")

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
        latest_prediction = next(
            (run for run in runs if run.get("kind") == "prediction"),
            None,
        )
        if latest_prediction is None:
            summary.append(f"[{MUTED}]vol forecast — no prediction run yet[/]")
        else:
            prediction_spec = latest_prediction.get("spec")
            if not isinstance(prediction_spec, dict):
                prediction_spec = {}
            mean_ic = float(prediction_spec.get("mean_ic", 0.0))
            stability = float(prediction_spec.get("ic_stability", 0.0))
            usable = bool(
                prediction_spec.get("usable", False)
                and mean_ic > IC_ADMISSION_THRESHOLD
                and stability > IC_STABILITY_THRESHOLD
            )
            stability_label = (
                "stable"
                if stability > IC_STABILITY_THRESHOLD
                else "unstable"
            )
            usability_label = "usable" if usable else "not usable"
            prediction_tone = UP if usable else DOWN
            summary.append(
                f"[{prediction_tone}]vol forecast IC {mean_ic:.3f} "
                f"({stability_label}) — {usability_label}[/]"
            )
        summary.append("Run [bold]: batch[/] for the staged comparison suite.")
        self.query_one("#research-summary", Static).update("\n".join(summary))

        board = self.snapshot.get("leaderboard") or []
        if board:
            lines = [
                f"[{DIM}]METHOD                          SHARPE     RET   "
                f"MAXDD   CVAR95    DSR[/]"
            ]
            for row in board:
                if row.get("champion"):
                    mark, mark_tone = "★", AMBER
                elif row.get("benchmark"):
                    mark, mark_tone = "BENCH", DIM
                else:
                    mark, mark_tone = "", DIM
                # Markup tags occupy no cells: pad the plain text to the column
                # width first, then wrap it, or every row type drifts.
                name_cell = escape(f"{str(row.get('name', '')):<24}")
                lines.append(
                    f"[{TEXT_HI}]{name_cell}[/] [{mark_tone}]{mark:<5}[/]  "
                    f"[{TEXT}]{_cell(row.get('sharpe')):>6}  "
                    f"{_cell(row.get('ann_return'), '{:+.1%}'):>6}  "
                    f"{_cell(row.get('max_drawdown'), '{:.1%}'):>6}  "
                    f"{_cell(row.get('cvar_95'), '{:.2%}'):>7}  "
                    f"{_cell(row.get('deflated_sharpe')):>5}[/]")
            self.query_one("#leaderboard", Static).update("\n".join(lines))
        else:
            self.query_one("#leaderboard", Static).update(
                f"[{MUTED}]No ablation recorded yet — run [bold]: batch[/] for the "
                f"staged comparison.[/]")

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

    def _render_book_equity(self, portfolio: dict) -> None:
        """Realized equity curve and metrics — or why there are none yet.

        The curve is the recorded equity marks, not a backtest: an empty series
        is a real state of the book (nothing marked yet), and metrics that the
        owner could not compute carry its note instead of blank cells.

        Every number states its own basis. The headline is the live broker equity
        and is labelled ``live``; the percentage is ``window_change``, measured
        across exactly the marks this chart draws, and dated from the first of
        them — so it can never be read from a point off the chart's left edge.
        The cadence the metrics were annualized on, a capped history, and marks
        excluded as another book's are all disclosed rather than assumed.
        """
        performance = (self.snapshot.get("performance") or {}) if self.snapshot else {}
        rows = performance.get("series") or []
        series = [float(row["equity"]) for row in rows]
        lines: list[str] = []
        if series:
            change = performance.get("window_change")
            tone = TEXT if change is None else (UP if float(change) >= 0 else DOWN)
            header = (f"[bold {TEXT_HI}]{money(portfolio.get('equity'))}[/]"
                      f" [{DIM}]live[/]")
            if change is not None:
                sign = "+" if float(change) >= 0 else ""
                header += (f"   [{tone}]{sign}{pct(float(change))} since "
                           f"{escape(str(rows[0].get('ts', '—')))}[/]")
            header += f"   [{LABEL_GOLD}]{_marks_label(performance)}[/]"
            lines.append(header)
            lines.append("")
            for bar in braille_chart(series, width=56, height=4):
                lines.append(f"[{tone}]{escape(bar)}[/]")
            span = (
                f"{str(rows[0].get('ts', '—'))} → {str(rows[-1].get('ts', '—'))}"
                if len(rows) > 1 else str(rows[-1].get("ts", "—")))
            lines.append(f"[{DIM}]{escape(span)}  ·  daily equity marks[/]")
            cadence = performance.get("cadence")
            if isinstance(cadence, dict) and cadence.get("periods_per_year"):
                lines.append(
                    f"[{DIM}]annualized at "
                    f"{cadence['periods_per_year']:.0f}/yr from the observed "
                    f"cadence[/]")
            lines.append("")
            metrics = performance.get("metrics")
            if metrics:
                lines.append(
                    f"[{LABEL_GOLD}]ret[/] [{TEXT}]{pct(metrics['ann_return'])}[/]   "
                    f"[{LABEL_GOLD}]vol[/] [{TEXT}]{pct(metrics['ann_vol'])}[/]   "
                    f"[{LABEL_GOLD}]sharpe[/] [{TEXT}]{metrics['sharpe']:.2f}[/]   "
                    f"[{LABEL_GOLD}]maxdd[/] [{TEXT}]{pct(metrics['max_drawdown'])}[/]   "
                    f"[{LABEL_GOLD}]cvar95[/] [{TEXT}]{pct(metrics['cvar_95'])}[/]   "
                    f"[{LABEL_GOLD}]obs[/] [{TEXT}]{int(metrics['n_obs'])}[/]")
            # The owner's note carries every exclusion and cap; it is a
            # disclosure, not a fallback, so it prints alongside real metrics.
            note = str(performance.get("note") or "")
            if note:
                lines.append(f"[{MUTED}]{escape(note)}[/]")
        else:
            lines.append(
                f"[{MUTED}]No equity history yet — marks are recorded by daily "
                f"ops, executions, and hourly polls.[/]")
        self.query_one("#book-equity", Static).update("\n".join(lines))

    def _render_book(self) -> None:
        portfolio = self.snapshot.get("portfolio", {}) if self.snapshot else {}
        positions = portfolio.get("positions") or {}
        weights = portfolio.get("weights") or {}
        self._render_book_equity(portfolio)
        tickers = sorted(
            set(positions) | {
                ticker for ticker, weight in weights.items()
                if abs(float(weight)) > 0.0005
            },
            key=lambda ticker: (-float(weights.get(ticker, 0.0)), str(ticker)),
        )
        position_lines = [
            f"[{DIM}]TICKER   WEIGHT        QUANTITY          VALUE          P&L[/]"
        ]
        for ticker in tickers:
            position = positions.get(ticker) or {}
            quantity = position.get("qty")
            quantity_text = (
                "—" if quantity is None else f"{float(quantity):,.4f}")
            # Marks written before unrealized P&L existed carry no key at all;
            # an em dash says "not known", which a $0.00 would misreport.
            unrealized = position.get("unrealized_pl")
            unrealized_text = "—" if unrealized is None else money(unrealized)
            unrealized_tone = (
                MUTED if unrealized is None
                else (UP if float(unrealized) >= 0 else DOWN))
            position_lines.append(
                f"[bold {TEXT_HI}]{escape(str(ticker)):<7}[/] "
                f"[{AMBER}]{pct(float(weights.get(ticker, 0.0))):>7}[/]   "
                f"[{TEXT}]{quantity_text:>12}[/]   "
                f"[{TEXT_HI}]{money(position.get('value')):>12}[/]   "
                f"[{unrealized_tone}]{unrealized_text:>10}[/]"
            )
        if not tickers:
            position_lines.append(
                f"[{MUTED}]No positions yet — the paper book is cash.[/]")
        self.query_one("#book-positions", Static).update(
            "\n".join(position_lines))

        plans = sorted(
            self.snapshot.get("plans", []) if self.snapshot else [],
            key=lambda row: str(row.get("created_at", "")),
            reverse=True,
        )[:5]
        self._book_plan_ids = {}
        for slot in range(5):
            card = self.query_one(f"#book-plan-{slot}", Horizontal)
            button = self.query_one(f"#execute-plan-{slot}", Button)
            if slot >= len(plans):
                card.styles.display = "none"
                button.disabled = True
                continue
            plan = plans[slot]
            card.styles.display = "block"
            plan_id = str(plan.get("plan_id", ""))
            state = str(plan.get("state", "unknown")).lower()
            glyph, tone = _book_state_style(state)
            pre_trade = plan.get("pre_trade") or {}
            turnover = pre_trade.get("turnover")
            turnover_text = (
                pct(float(turnover)) if turnover is not None else "—")
            created = (
                str(plan.get("created_at", "")).replace("T", " ")[:19] or "—")
            self.query_one(
                f"#book-plan-copy-{slot}", Static).update(
                    f"[{tone}]{glyph} {escape(state.upper())}[/]  "
                    f"[bold {TEXT_HI}]{escape(plan_id or '—')}[/]\n"
                    f"[{LABEL_GOLD}]turnover[/] [{TEXT}]{turnover_text}[/]   "
                    f"[{LABEL_GOLD}]created[/] [{TEXT}]{escape(created)}[/]"
                )
            button_id = str(button.id)
            self._book_plan_ids[button_id] = plan_id
            button.disabled = state not in {"checked", "submitted"}
            button.tooltip = (
                f"Confirm execution of {plan_id}"
                if not button.disabled else
                f"Plan state {state} is not executable"
            )

        orders = sorted(
            self.snapshot.get("orders", []) if self.snapshot else [],
            key=lambda row: str(row.get("created_at", "")),
            reverse=True,
        )[:10]
        order_lines = []
        for order in orders:
            side = str(order.get("side", "—")).upper()
            side_tone = UP if side == "BUY" else DOWN if side == "SELL" else MUTED
            state = str(order.get("state", "unknown"))
            glyph, state_tone = _book_state_style(state)
            order_lines.append(
                f"[bold {side_tone}]{escape(side):<4}[/]  "
                f"[{TEXT_HI}]{escape(str(order.get('ticker', '—'))):<6}[/]  "
                f"[{TEXT}]{money(order.get('notional')):>12}[/]  "
                f"[{state_tone}]{glyph} {escape(state.upper())}[/]"
            )
        if not order_lines:
            order_lines.append(f"[{MUTED}]No paper orders yet.[/]")
        self.query_one("#book-orders", Static).update("\n".join(order_lines))

    def _render_settings(self) -> None:
        if self.bootstrap is not None:
            mandate = self.bootstrap.get("mandate") or {}
            policy_id = str(mandate.get("operational_policy", "—"))
            policy_label = str(
                self.snapshot.get("policy", {}).get("label", "")).strip()
            policy_text = (
                f"{policy_id} · {policy_label}" if policy_label else policy_id)
            mandate_lines = [
                f"[{LABEL_GOLD}]MANDATE · OWNER CONFIGURATION[/]",
            ]
            mandate_lines.extend(_key_number_markup(
                [
                    ("paper capital", money(mandate.get("paper_capital"))),
                    (
                        "per-asset cap",
                        pct(mandate.get("max_weight_per_asset")),
                    ),
                    (
                        "turnover cap",
                        pct(mandate.get("max_turnover_per_rebalance")),
                    ),
                    (
                        "drawdown kill",
                        pct(mandate.get("trailing_drawdown_pct")),
                    ),
                    ("operational policy", policy_text),
                ],
                value_tones=[TEXT_HI, TEXT_HI, TEXT_HI, TEXT_HI, AMBER],
                bold_values={0, 4},
            ))
            mandate_copy = "\n".join(mandate_lines)
        elif self._bootstrap_error:
            bootstrap_error = self._bootstrap_error
            if len(bootstrap_error) > 600:
                bootstrap_error = bootstrap_error[:599].rstrip() + "…"
            mandate_lines = [f"[bold {DOWN}]OWNER UNREACHABLE[/]"]
            mandate_lines.extend(_bulletin_markup(
                [
                    "Mandate settings could not be loaded.",
                    bootstrap_error,
                ],
                tone=MUTED,
                max_len=600,
            ))
            mandate_copy = "\n".join(mandate_lines)
        elif self._bootstrap_started:
            mandate_copy = "\n".join([
                f"[{LABEL_GOLD}]MANDATE · OWNER CONFIGURATION[/]",
                *_bulletin_markup(
                    ["loading /api/bootstrap…"],
                    tone=CYAN,
                ),
            ])
        else:
            mandate_copy = "\n".join([
                f"[{LABEL_GOLD}]MANDATE · OWNER CONFIGURATION[/]",
                *_bulletin_markup(
                    ["Loaded once from the owner when Settings is opened."],
                    tone=MUTED,
                ),
            ])
        self.query_one("#settings-mandate", Static).update(mandate_copy)

        market = self.snapshot.get("market", {}) if self.snapshot else {}
        system = self.snapshot.get("system", {}) if self.snapshot else {}
        market_age = market.get("bar_age_days")
        provenance_age = system.get("data_age_days")
        data_lines = [f"[{LABEL_GOLD}]DATA · READ-ONLY PROVENANCE[/]"]
        data_lines.extend(_key_number_markup([
            ("snapshot source", str(market.get("source", "—")).upper()),
            (
                "as of / frequency",
                f"{market.get('as_of', '—')} · "
                f"{str(market.get('frequency', '—')).upper()}",
            ),
            (
                "bar age",
                "—" if market_age is None else f"{market_age} days",
            ),
            (
                "cached provenance",
                f"{system.get('data_source', 'none')} · "
                f"{'—' if provenance_age is None else f'{provenance_age} days'}",
            ),
        ]))
        data_copy = "\n".join(data_lines)
        self.query_one("#settings-data", Static).update(data_copy)

        agents = self.snapshot.get("agents", []) if self.snapshot else []
        agent_lines = [f"[{LABEL_GOLD}]AGENTS · AUTHORITY[/]"]
        agent_lines.extend(_key_number_markup(
            [
                (
                    str(agent.get("name", "—")),
                    str(agent.get("authority", "—")),
                )
                for agent in agents
            ],
            value_tones=[MUTED] * len(agents),
            bold_values=set(range(len(agents))),
        ))
        if not agents:
            agent_lines.extend(_bulletin_markup(
                ["No owner agent definitions loaded."],
                tone=MUTED,
            ))
        self.query_one("#settings-agents", Static).update(
            "\n".join(agent_lines))

        theme_lines = [f"[{LABEL_GOLD}]THEME · TERMINAL PALETTE[/]"]
        theme_lines.extend(_key_number_markup(
            [
                ("palette", f"[{TEXT_HI}]{escape(PALETTE_NAME)}[/]"),
                (
                    "accents",
                    f"[{AMBER}]████ amber[/]  [{CYAN}]████ cyan[/]  "
                    f"[{UP}]████ up[/]  [{DOWN}]████ down[/]",
                ),
            ],
            values_are_markup=True,
        ))
        self.query_one("#settings-theme", Static).update(
            "\n".join(theme_lines)
        )

    def _render_audit(self) -> None:
        decisions = self.snapshot.get("decisions", [])
        self.query_one("#audit-summary", Static).update(
            "Every judgment, challenge, verdict, and reflection remains inspectable.\n\n"
            f"{len(decisions)} decisions   ·   plans and orders are in Book"
        )
        rows = []
        self._audit_decisions = {}
        for decision in decisions:
            choice = decision.get("choice", {})
            arm = choice.get("arm")
            detail = (
                choice.get("regime")
                or (arm_display_name(arm) if arm else None)
                or decision.get("rationale", "")
            )
            key = str(decision.get("decision_id", ""))
            self._audit_decisions[key] = decision
            rows.append((
                decision.get("created_at", ""), "decision", decision.get("kind", ""),
                _verdict_cell(decision.get("verdict")),
                _reflection_cell(decision.get("reflection")),
                str(detail)[:48], key,
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
        challenger = (
            str(decision.get("challenger_view") or "").strip()
            or "no challenge recorded"
        )
        verdict = _record(decision.get("verdict"))
        verdict_tone, verdict_text = _verdict_style(verdict or None)
        reasons = verdict.get("reasons") or []
        reason_lines = (
            [str(reason) for reason in reasons]
            if isinstance(reasons, list)
            else [str(reasons)]
        )
        if not reason_lines:
            reason_lines = ["—"]
        rationale = str(decision.get("rationale") or "").strip() or "—"
        reflection = (
            str(decision.get("reflection") or "").strip() or "pending"
        )
        choice = decision.get("choice") or {}
        card_lines = [
            f"[bold {TEXT_HI}]DECISION {escape(str(key)[:16])}[/]",
            f"[{MUTED}]{escape(str(decision.get('kind', '—')))} · "
            f"{escape(str(decision.get('as_of', '—')))}[/]",
            "",
            f"[{LABEL_GOLD}]CHOICE[/]",
        ]
        if choice:
            card_lines.extend(_key_number_markup([
                (
                    str(choice_key),
                    str(
                        arm_display_name(choice[choice_key])
                        if choice_key == "arm" else choice[choice_key]
                    )[:160],
                )
                for choice_key in list(choice)[:4]
            ]))
        else:
            card_lines.extend(_bulletin_markup(["—"], tone=MUTED))
        card_lines.extend([
            "",
            f"[{LABEL_GOLD}]RATIONALE[/]",
            *_bulletin_markup(
                rationale.splitlines(),
                max_len=300,
                strip_ids=False,
            ),
            "",
            f"[{LABEL_GOLD}]CHALLENGER[/]",
            *_bulletin_markup(
                challenger.splitlines(),
                max_len=300,
                strip_ids=False,
            ),
            "",
            f"[bold {verdict_tone}]VERDICT  {escape(verdict_text)}[/]",
            *_bulletin_markup(
                reason_lines,
                max_len=300,
                strip_ids=False,
            ),
            "",
            f"[{LABEL_GOLD}]REFLECTION[/]",
            *_bulletin_markup(
                reflection.splitlines(),
                max_len=300,
                strip_ids=False,
            ),
        ])
        self._set_selected_work("\n".join(card_lines), markup=True)
        self.query_one("#event-strip", Static).update(
            f"{escape(str(key)[:10])}  "
            f"[{verdict_tone}]verdict {escape(verdict_text)}[/] · "
            "challenger + rationale in the work rail →"
        )

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
                "working": ("●", CYAN),
                "queued": ("◐", GOLD),
                "waiting": ("◐", GOLD),
                "done": ("✓", UP),
                "failed": ("×", DOWN),
                "blocked": ("!", AMBER),
            }.get(state, ("◌", DIM))
            if state == "working":
                glyph = _PULSE_FRAMES[self._pulse % len(_PULSE_FRAMES)]
            lines.append(
                f"[{color}]{glyph}[/] [bold]{escape(name)}[/]\n"
                f"   [{DIM}]{escape(state)} · {escape(str(agent.get('authority', '—')))}[/]"
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
        autopilot = system.get("autopilot", {})
        last_run = autopilot.get("last_run_at") if isinstance(autopilot, dict) else None
        trigger_count = (
            int(autopilot.get("triggers_fired", 0))
            if isinstance(autopilot, dict)
            else 0
        )
        if last_run:
            try:
                last_run_text = datetime.fromisoformat(
                    str(last_run).replace("Z", "+00:00")
                ).strftime("%m-%d %H:%M")
            except ValueError:
                last_run_text = str(last_run)
            autopilot_token = f"AUTO {last_run_text}·{trigger_count}"
        else:
            autopilot_token = "AUTO —·0"
        self.query_one("#system-status", Static).update(
            f"PAPER · {source}/DAILY · {mcp} · {claude} · "
            f"{data_token} · {autopilot_token}")
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
        if view == "desk":
            view = "dashboard"
        if view not in _VIEWS:
            return
        self.set_focus(None)
        self._agent_focus = False
        self.screen.remove_class("agent-focus")
        self.active_view = view
        self.query_one("#canvas", ContentSwitcher).current = view
        self._render_nav()
        if view == "workforce":
            # Chat-first focus claims digits as text; F1-F8 still switch views,
            # and Escape blurs the input so digit navigation works again.
            field = self.query_one("#chat-input", Input)
            if not field.disabled:  # a running turn owns the box; don't grab it
                field.focus()
        elif view == "atlas":
            # Master-detail only reads if the index is navigable on arrival; the
            # ListView claims no digit keys, so view switching keeps working.
            self.query_one("#atlas-list", ListView).focus()
            self._start_atlas_fetch()
        elif view == "settings":
            self._start_bootstrap()

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

    def action_symbol(self, ticker: str) -> None:
        self.active_ticker = ticker.upper()
        self.query_one("#universe", ListView).index = (
            self.universe_tickers.index(self.active_ticker))
        self.action_view("market")
        self._render_market()

    def action_ask(self, prompt: str) -> None:
        self._start_claude(prompt, governed=False)

    def action_chat_mode(self, message: str = "") -> None:
        self._chat_mode = "chat"
        self.action_view("workforce")
        self._render_chat_mode()
        if message:
            self._chat_send(message)

    def action_workforce_new(self, goal: str = "") -> None:
        self._chat_mode = "workforce"
        self._render_chat_mode()
        system = self.snapshot.get("system", {})
        if not system.get("workforce_available", system.get("governed_available")):
            reason = system.get(
                "governed_lock_reason", "Claude workforce runtime is not ready")
            self._set_selected_work(f"CLAUDE WORKFORCE LOCKED\n\n{reason}")
            self._write_local_event("claude.workforce_locked", {"reason": reason})
        else:
            self._start_workforce(goal)

    def action_workforce_status(self) -> None:
        self._chat_mode = "workforce"
        self._render_chat_mode()
        self.action_view("workforce")
        self._render_workforce()

    def action_workforce_resume(self, workflow_id: str) -> None:
        self._chat_mode = "workforce"
        self._render_chat_mode()
        self._start_workforce("", workflow_id=workflow_id)

    def action_workforce_stop(self) -> None:
        self._chat_mode = "workforce"
        self._render_chat_mode()
        self.claude.stop()
        self._set_selected_work(
            "CLAUDE WORKFORCE STOPPED\n\nThe owner kept its durable phase state. "
            "Use : workforce resume ID to continue."
        )
        self._write_local_event("claude.workforce_stopped", {})

    def action_rebalance_dry(self) -> None:
        self._run_api_action(
            "rebalance dry", "/api/run_once",
            {"offline": self.offline, "execute": False},
            active_agent="moments-analyst",
        )

    def action_rebalance_paper(self) -> None:
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
            self._confirm_plan_execution(str(plan["plan_id"]))

    def _confirm_plan_execution(self, plan_id: str) -> None:
        plan = next(
            (row for row in self.snapshot.get("plans", [])
             if str(row.get("plan_id", "")) == plan_id),
            None,
        )
        state = str((plan or {}).get("state", "")).lower()
        if plan is None or state not in {"checked", "submitted"}:
            self._set_selected_work(
                "PLAN NOT EXECUTABLE\n\nOnly a persisted checked or submitted "
                "plan can enter the human confirmation flow."
            )
            return
        self._pending_plan_id = plan_id
        self.push_screen(
            PaperConfirmScreen(self._pending_plan_id), self._paper_confirmed
        )

    def action_daily_ops(self) -> None:
        self._run_api_action(
            "daily ops", "/api/daily_ops", {"offline": self.offline},
            active_agent="reporter",
        )

    def action_batch(self) -> None:
        self.action_view("research")
        self._run_api_action(
            "batch ablation", "/api/batch",
            {"offline": self.offline},
            active_agent="optimization-runner",
        )

    def action_help(self) -> None:
        self._set_selected_work(
            "COMMANDS\n\n"
            "view dashboard|desk|market|workforce|research|book|audit|atlas|settings\n"
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
            "1–8 or F1–F8 switch views · j/k select instrument · "
            "A toggles agents · Ctrl-Q quits"
        )

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
                f"[{GOLD}]a session is working — wait for the turn to "
                "finish or press stop[/]")
            return
        self._console_write(f"[bold {AMBER}]you ▸[/] [{TEXT_HI}]{escape(message)}[/]")
        if self._chat_mode == "chat":
            resume = self._chat_sessions["chat"]
            if not resume:
                self._console_write(
                    f"[{LABEL_GOLD}]▌ chat — read-only desk assistant[/]")
            self._start_claude(message, governed=False, chat=True,
                               resume_session=resume)
        elif self._chat_sessions["workforce"]:
            self._start_claude(
                message, governed=True,
                resume_session=self._chat_sessions["workforce"])
        else:
            self._start_workforce(message)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id in self._book_plan_ids:
            self._confirm_plan_execution(self._book_plan_ids[button_id])
            return
        if button_id == "btn-rebalance-dry":
            self.action_rebalance_dry()
            return
        if button_id == "btn-daily-ops":
            self.action_daily_ops()
            return
        if button_id == "btn-batch":
            self.action_batch()
            return
        if button_id == "btn-workforce-new":
            self.action_workforce_new()
            return
        if button_id == "btn-workforce-resume":
            workflow_id = self._latest_resumable_workflow_id()
            if workflow_id:
                self.action_workforce_resume(workflow_id)
            else:
                self._console_write(
                    f"[{MUTED}]no incomplete workforce review to resume[/]")
                self._set_selected_work(
                    "NO REVIEW TO RESUME\n\nThe latest snapshot has no incomplete "
                    "workforce review."
                )
            return
        if button_id != "chat-exit":
            return
        if self.claude.running:
            self.claude.stop()
            self._console_write(
                f"[{GOLD}]■ stopped — durable phase state is kept; send a "
                "message to continue or use : workforce resume ID[/]")
            self._write_local_event("claude.workforce_stopped", {})
        else:
            self.action_view("dashboard")

    def _handle_command(self, raw: str) -> None:
        command, _, rest = raw.partition(" ")
        command = command.lower()
        rest = rest.strip()
        subword, _, argument = rest.partition(" ")
        subword = subword.lower()
        argument = argument.strip()

        action_name = COMMAND_TABLE.get((command, subword)) if subword else None
        action_args: tuple[Any, ...] = ()
        if action_name == "action_view":
            if argument:
                action_name = None
            else:
                action_args = (subword,)
        elif action_name == "action_workforce_resume":
            if argument:
                action_args = (argument,)
            else:
                action_name = None
        elif action_name is not None and argument:
            action_name = None

        if action_name is None:
            action_name = COMMAND_TABLE.get((command, None))
            if action_name == "action_symbol":
                ticker = rest.upper()
                if ticker in self.universe_tickers:
                    action_args = (ticker,)
                else:
                    action_name = None
            elif action_name == "action_ask":
                if rest:
                    action_args = (rest,)
                else:
                    action_name = None
            elif action_name in {"action_chat_mode", "action_workforce_new"}:
                action_args = (rest,)

        if action_name is not None:
            getattr(self, action_name)(*action_args)
            return

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
        bound = next((
            row for row in workflows
            if str(row.get("workflow_id", "")) == workflow_id
        ), None)
        flow = (
            _flow_from_steps(
                list((bound or {}).get("steps") or []),
                standard_fallback=str((bound or {}).get("kind") or "") != "panel",
            )
            if bound else _FLOW
        )
        self._set_flow_spec(flow)
        self._flow_states = {phase: "queued" for phase, _, _ in flow}
        self._flow_details = {
            phase: f"{agent} · {phase}\n\nqueued — waiting to start"
            for phase, agent, _ in flow}
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
            self._console_fenced = False
            if not resume_session:
                first_line = prompt.splitlines()[0]
                self._console_write(
                    f"[bold {AMBER}]▌ workforce run[/]  "
                    f"[{MUTED}]{escape(first_line[:110])}[/]")
                self._console_write(
                    f"[{LABEL_GOLD}]running autonomously — one note per agent below; "
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
                phase = self._phase_for_agent(
                    agent,
                    prefer_queued=event.tool == "Agent",
                )
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
                            f"[{CYAN}]▶ {escape(_phase_short(phase))}[/]"
                            f" [{LABEL_GOLD}]working[/]")
            elif chat:
                self._console_flush()
                self._console_write(f"[{CYAN}]→ {escape(event.tool)}[/]")
        elif event.kind == "error":
            self._write_local_event("claude.failed", {"error": event.text[-400:]})
            if workforce:
                self._console_write(f"[{DOWN}]✗ {escape(event.text[-240:])}[/]")
                # A terminal error still owes the operator the run's state: the
                # durable phases reached before it stopped.
                if not self.claude.running:
                    self._print_workforce_results("")
            elif chat:
                self._console_flush()
                self._console_write(f"[{DOWN}]✗ {escape(event.text[-300:])}[/]")
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
                self._console_write(f"[{UP}]▮ done[/]")
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

        workflow_steps = [
            step for step in (workflow.get("steps") or [])
            if isinstance(step, dict)
        ]
        steps = {str(step.get("phase")): step for step in workflow_steps}
        flow = _flow_from_steps(
            workflow_steps,
            standard_fallback=str(workflow.get("kind") or "") != "panel",
        )
        verdict = str((steps.get("referee", {}).get("artifacts") or {})
                      .get("verdict") or "")

        # 1 · what was achieved — the conclusion, first.
        for line in _bulletin_markup([
            _result_headline(status, verdict),
        ]):
            self._console_write(line)

        # 2 · the regime the run chose, and why.
        regime_line = _regime_line(steps)
        if regime_line:
            self._console_write("")
            self._console_write(regime_line)

        # 3 · what each agent did, one plain-language line each.
        self._console_write("")
        self._console_write(f"[{LABEL_GOLD}]WHAT EACH AGENT DID[/]")
        for phase, _agent, _short in flow:
            glyph, colour, name, action = _agent_brief(
                phase, steps.get(phase), status)
            action_lines = bulletin([action], max_len=220)
            action_text = " · ".join(action_lines) or "—"
            self._console_write(
                f"  [{colour}]{glyph}[/] [bold {TEXT_HI}]{escape(name):<11}[/] "
                f"[{TEXT}]{escape(action_text)}[/]")

        # 4 · the final output — the weights, and the one action left to a human.
        targets = _extract_targets(steps)
        has_plan = bool((steps.get("reporter", {}).get("artifacts") or {})
                        .get("plan_id"))
        if targets or has_plan:
            self._console_write("")
            self._console_write(f"[{LABEL_GOLD}]RECOMMENDATION[/]")
            recommendation_pairs = []
            recommendation_tones = []
            if targets:
                recommendation_pairs.append(
                    ("target weights", _format_targets(targets))
                )
                recommendation_tones.append(TEXT_HI)
            if has_plan:
                recommendation_pairs.append((
                    "paper plan",
                    "ready — type : rebalance paper to confirm it yourself",
                ))
                recommendation_tones.append(UP)
            for line in _key_number_markup(
                recommendation_pairs,
                value_tones=recommendation_tones,
            ):
                self._console_write(f"  {line}")

        # 5 · what the output signifies, in plain terms.
        self._console_write("")
        self._console_write(f"[{LABEL_GOLD}]WHAT THIS MEANS[/]")
        for line in _bulletin_markup([
            _result_meaning(status, verdict, bool(targets), has_plan),
        ]):
            self._console_write(f"  {line}")

    def _print_results_fallback(self, text: str) -> None:
        """No durable workflow to summarize: show the coordinator's closing text,
        cleaned of markdown, ids, and mojibake, or a short pointer if it is empty."""
        text = (text or "").strip()
        if not text or text == "Claude completed":
            self._console_write(
                f"[{MUTED}]The run ended before it recorded any results. Start "
                "again with a goal, or resume it from the workforce view.[/]")
            return
        for line in _bulletin_markup(
            text.splitlines(),
            tone=TEXT_HI,
        ):
            self._console_write(line)

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

    def _phase_for_agent(self, agent: str, *, prefer_queued: bool) -> str:
        """Choose this dispatch's concrete phase from the active step board."""
        candidates = [
            phase for phase, candidate_agent, _short in self._flow_spec
            if candidate_agent == agent
        ]
        if not candidates:
            return ""
        state_order = (
            ("queued", "idle", "working")
            if prefer_queued else
            ("working", "queued", "idle")
        )
        for state in state_order:
            for phase in candidates:
                if self._flow_states.get(phase, "idle") == state:
                    return phase
        return candidates[0]

    def _set_selected_work(self, text: str, *, markup: bool = False) -> None:
        self.query_one("#selected-work", Static).update(
            text if markup else escape(text)
        )
