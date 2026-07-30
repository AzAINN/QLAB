"""The qlab quiet-workstation operator console."""

from __future__ import annotations

import json
import math
import shlex
import shutil
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any

from rich.markup import escape
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical
from textual.reactive import reactive
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

from qlab.core.desk_mode import DeskMode
from qlab.core.reference import arm_display_name
from qlab.paths import workspace_root
from qlab.research.prediction import (
    IC_ADMISSION_THRESHOLD,
    IC_STABILITY_THRESHOLD,
)
from qlab.tui.reference_view import ReferenceView
from qlab.tui.claude import ClaudeEvent, ClaudeSession
from qlab.tui.client import gather_snapshot
from qlab.tui.desk_mode_screen import DeskModeScreen
from qlab.tui.formatting import (
    braille_chart, bulletin, connection_chip, fence_state_after,
    is_numbered_item, key_number_lines, money, pct, phase_elapsed,
    report_lines, spark, sparkline, verdict_chip, weight_bar,
)
from qlab.tui.design import primitives, tokens
# Colour names resolve against the active theme at render time. They are
# variable references, not literals, so every inline markup site below follows a
# theme switch. The Rich-rendered console is the one exception and resolves them
# itself in `_console_write`.
from qlab.tui.design.markup import (
    ALLOCATION_TRACK,
    AMBER,
    AMBER_HI,
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
    SEL_BG,
    TEXT,
    TEXT_HI,
    UP,
)
from qlab.tui.design.markup import resolve as _resolve_markup
from qlab.tui.theme import (
    APP_CSS,
    ATLAS_DRAWER_CSS,
    PALETTE_NAME,
    PAPER_MODAL_CSS,
    STATE_STYLE,
)


_WORKSPACE_ROOT = workspace_root()
_DEFAULT_TICKERS = ["ACWI", "BNDW", "GSG", "IGF", "GLD", "VNQ", "EMB"]
_VIEWS = (
    "atlas", "dashboard", "market", "workforce", "research", "book", "audit",
    "reference", "settings", "news",
)
# The key that actually selects each view. The nav used to number its rows by
# position, which silently lies the moment the order and the bindings diverge —
# a row reading "4" that does not respond to 4 is worse than an unnumbered one.
_VIEW_KEYS = {view: str((index + 1) % 10) for index, view in enumerate(_VIEWS)}
_DASHBOARD_TILE_KEYS = (
    "equity", "allocation", "regime", "market-pulse", "verdict", "run", "alerts",
    "stress",
)
_AGENT_NAMES = (
    "moments-analyst", "challenger", "optimization-runner", "referee", "reporter",
)
# What each Atlas mode may do. Stated plainly in the drawer because the mode IS
# the authority: a reader must never have to infer what Atlas is allowed to do.
_ATLAS_STATE_TONES = {
    "observing": UP,
    "blocked": DOWN,
    "degraded": AMBER,
    "paused": MUTED,
}
_ATLAS_MODE_AUTHORITY = {
    "observe": "Observe: monitors and briefs. Starts no workflows.",
    "research": "Research: may start approved research workflows. "
                "May not create a paper plan.",
    "propose": "Propose: may request a checked plan and open an approval "
               "request. Cannot approve or execute it.",
    "paused": "Paused: no new autonomous work. Monitoring and approval "
              "expiry continue.",
}
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
    "workflow_interrupted", "workflow_resumed", "workflow_abandoned",
    "plan_built", "order_filled", "decision_logged", "ablation_complete",
    "cost_gate_refusal", "autopilot_trigger", "daily_ops",
}
_QUOTE_REPAINT_INTERVAL = 1.0
_EVENT_ID_LIMIT = 2_048
COMMAND_TABLE = {
    ("view", "dashboard"): "action_view",
    ("view", "desk"): "action_view",
    ("view", "market"): "action_view",
    ("view", "workforce"): "action_view",
    ("view", "research"): "action_view",
    ("view", "book"): "action_view",
    ("view", "audit"): "action_view",
    ("view", "reference"): "action_view",
    ("view", "atlas"): "action_view",
    ("view", "settings"): "action_view",
    ("view", "agents"): "action_agent_focus",
    ("agents", None): "action_agent_focus",
    ("theme", None): "action_theme",
    # Hand the terminal to the real Claude CLI. The governed workforce prints
    # one short note per agent by design, which is deliberate but opaque if you
    # want to see what the model is actually doing — this is the way to see it.
    ("claude", None): "action_claude_cli",
    ("cli", None): "action_claude_cli",
    ("symbol", None): "action_symbol",
    ("timeline", None): "action_timeline",
    ("help", None): "action_help",
    ("ask", None): "action_ask",
    ("chat", None): "action_chat_mode",
    ("workforce", None): "action_workforce_new",
    ("workforce", "status"): "action_workforce_status",
    ("workforce", "resume"): "action_workforce_resume",
    ("workforce", "stop"): "action_workforce_stop",
    ("workforce", "abandon"): "action_workforce_abandon",
    ("workforce", "clean"): "action_workforce_abandon",
    ("governed", None): "action_workforce_new",
    ("governed", "status"): "action_workforce_status",
    ("governed", "resume"): "action_workforce_resume",
    ("governed", "stop"): "action_workforce_stop",
    ("governed", "abandon"): "action_workforce_abandon",
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


# CHANGE #5: Report tones with richer visual hierarchy.
# h1: full amber left-bar accent (Bloomberg section header style)
# h2: bold bright text, muted marker distinguishes from h1
# bullet: cyan marker for better scan-readability vs muted
# code: dim text (verbatim; unchanged semantics)
# table: dim (verbatim alignment blocks)
# text: normal body colour
_REPORT_TONES = {
    "h1": f"[bold {AMBER}]▌ {{}}[/]",
    "h2": f"[{AMBER_HI}]› [/][bold {TEXT_HI}]{{}}[/]",
    "bullet": f"[{CYAN}]  › [/][{TEXT}]{{}}[/]",
    # No indent: a fenced block is reproduced exactly as written. Padding it
    # for looks changes the text an operator copies out, and for Python that
    # is a syntax change rather than a cosmetic one.
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
    if status == "interrupted":
        return (
            f"{short} interrupted — {detail or 'the coordinator stopped'}",
            "The run is paused, not working. Resume it explicitly or abandon it "
            "to close the incomplete review.",
        )
    if status == "abandoned":
        return (
            f"{short} abandoned — {detail or 'the operator closed the run'}",
            "The incomplete review is closed and remains in the audit trail. "
            "Start a new review for fresh reasoning.",
        )

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
    elif base == "challenger":
        nxt = (
            "Next: the optimizer uses the debate's final persisted decision; "
            "it cannot start from the analyst's superseded inputs."
        )
    elif base == "optimizer":
        nxt = (
            "Next: the referee independently checks the final decision and "
            "the optimizer's exact targets."
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
# The analyst's five-level regime ladder, most to least stressed, as a red→cyan
# heat scale. Exactly the rungs in qlab.tui.claude._ANALYST_REGIMES — nothing else
# belongs here, so the two stay verifiably in sync.
_REGIME_TONE = {
    "crisis": DOWN,
    "stress": GOLD,
    "neutral": AMBER_HI,
    "calm": UP,
    "expansion": CYAN,
}
# The HMM posterior speaks a different vocabulary than the analyst's ladder
# (`normal`/`uncertain` are not rungs). The dashboard tile subscripts this map
# directly for ("calm", "normal", "stress"), so those keys must exist; ladder
# rungs are inherited so an analyst-worded regime still gets its heat colour.
_HMM_STATE_TONE = {
    **_REGIME_TONE,
    "normal": CYAN,
    "uncertain": AMBER,
}


def _regime_readout(steps_by_phase: dict) -> tuple[str, str, str] | None:
    """The analyst's regime, its one-line reasoning, and the news summary, or None.

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
    # The news backdrop is model-written text quoting untrusted headlines, so it
    # gets the same cleaning as the reasoning rather than a raw whitespace join.
    summary = " · ".join(
        bulletin(
            str(artifacts.get("regime_summary") or "").splitlines(),
            max_len=320,
        )
    )
    return regime, reasoning, summary


def _regime_line(steps_by_phase: dict, *, indent: str = "") -> str | None:
    """Rich-markup regime block: the coloured call, its reasoning, and the 1-3
    line news backdrop that informed it (from market_news), or None."""
    readout = _regime_readout(steps_by_phase)
    if readout is None:
        return None
    regime, reasoning, summary = readout
    tone = _REGIME_TONE.get(regime.lower(), AMBER)
    line = f"{indent}[{CYAN}]◆ REGIME[/]  [bold {tone}]{escape(regime.upper())}[/]"
    if reasoning:
        line += f"  [{MUTED}]{escape(reasoning[:220])}[/]"
    if summary:
        line += (f"\n{indent}  [{LABEL_GOLD}]news backdrop[/]  "
                 f"[{TEXT}]{escape(summary[:320])}[/]")
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
    """Target weights as a compact, largest-first line: 'AAPL 30.0% · GLD 20.0%'.

    Weights come from an agent-authored artifact that the registry validates
    only as a dict — the value types are never checked — so a string weight or
    a null reaches here on the failed-run path. Coercing per entry keeps one
    bad weight from raising out of a render and, through `call_from_thread`,
    into the Claude reader thread.
    """
    numeric: list[tuple[str, float]] = []
    unreadable: list[str] = []
    for ticker, weight in targets.items():
        try:
            value = float(weight)
        except (TypeError, ValueError):
            unreadable.append(str(ticker))
            continue
        # NaN and inf survive float() and rendered as "SPY nan%" — a number-
        # shaped non-number on a trading surface, which reads as a real weight.
        # Python's json emits and parses NaN by default, so an agent artifact
        # carries one all the way here. Report it as unreadable, like a string.
        if not math.isfinite(value):
            unreadable.append(str(ticker))
            continue
        numeric.append((str(ticker), value))
    ordered = sorted(numeric, key=lambda kv: -kv[1])[:limit]
    line = " · ".join(f"{ticker} {weight:.1%}" for ticker, weight in ordered)
    if unreadable:
        # Naming them beats dropping them: a weight the desk could not read is
        # a fact about the run, not noise to hide.
        line += (" · " if line else "") + (
            f"[unreadable: {', '.join(sorted(unreadable)[:4])}]")
    return line


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
        "interrupted": (GOLD, "WORKFORCE INTERRUPTED"),
        "abandoned": (MUTED, "WORKFORCE ABANDONED"),
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
    if status == "interrupted":
        return "The coordinator stopped. Completed steps are saved and resumable."
    if status == "abandoned":
        return "The incomplete run was closed by the operator. Nothing was traded."
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
    if state == "interrupted":
        return glyph, colour, name, "paused safely before completing"
    if state == "abandoned":
        return glyph, colour, name, "did not complete before the run was abandoned"
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
    if status == "interrupted":
        return "Nothing was traded. Resume explicitly when you are ready to continue."
    if status == "abandoned":
        return "Nothing was traded. Start a new review if you want fresh reasoning."
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

    state: reactive[str] = reactive("idle")
    detail: reactive[str] = reactive("", repaint=False)
    pulse: reactive[int] = reactive(0)
    # Pushed down by the app on a theme change. The widget never reaches up to
    # read the active theme, so it renders identically inside and outside a
    # running app -- which is what keeps these renderers unit-testable.
    theme_name: reactive[str] = reactive(tokens.DEFAULT_THEME)

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
        detail = self.detail or f"{self.agent}\n\nnot yet started"
        self.app._set_selected_work(
            f"{self.short.upper()} · {self.agent}\n\n{detail}")

    def render(self) -> Text:
        # State is encoded twice -- glyph colour and the state word -- so an
        # unmapped owner status still reads correctly with the glyph degraded.
        # Change 1: three-line card — phase short label / glyph + state / agent role
        pulse = (_PULSE_FRAMES[self.pulse % len(_PULSE_FRAMES)]
                 if self.state == "working" else None)
        faint = tokens.role(self.theme_name, "faint")
        # Line 1: phase label (bold)
        rendered = Text(self.short.upper(), style="bold")
        rendered.append("\n")
        # Line 2: state glyph + state word
        rendered.append_text(primitives.state_badge(
            self.state, glyph=pulse, fallback="idle", theme=self.theme_name))
        rendered.append(f" {self.state}")
        rendered.append("\n")
        # Line 3: agent role name, dim/faint — truncated to fit the cell width
        role_label = self.agent.replace("-", " ")
        if len(role_label) > 11:
            role_label = role_label[:10] + "…"
        rendered.append(role_label, style=faint)
        return rendered

    def watch_state(self, state: str) -> None:
        for token in (
            "working", "queued", "done", "failed", "blocked",
            "interrupted", "abandoned",
        ):
            self.set_class(token == state, f"-{token}")

    def watch_detail(self, detail: str) -> None:
        self.tooltip = detail or f"{self.agent}\n\nnot yet started"


class FlowRow(Horizontal):
    """The scrollable node row inside the flow section."""

    def __init__(self, flow: tuple[tuple[str, str, str], ...]):
        super().__init__(id="flow-row")
        self.flow = flow

    def compose(self) -> ComposeResult:
        for index, (phase, agent, short) in enumerate(self.flow):
            if index:
                # data-flow style connector
                yield Static("──►", classes="flow-arrow")
            yield FlowNode(phase, agent, short)


class FlowBoard(Vertical):
    """A recomposable workflow-step board: header + node row + legend.

    Change 2: wraps the scrollable node row in a named section with a
    PIPELINE header and a compact state-legend row beneath the nodes so
    the operator always has a colour key visible.
    """

    # The legend text is static — state colours are already on the nodes.
    _LEGEND = (
        f"[{_STATE_STYLE['working'][1]}]● working[/]  "
        f"[{_STATE_STYLE['done'][1]}]✓ done[/]  "
        f"[{_STATE_STYLE['failed'][1]}]× failed[/]  "
        f"[{_STATE_STYLE['blocked'][1]}]! blocked[/]  "
        f"[{_STATE_STYLE['queued'][1]}]· queued[/]  "
        f"[{DIM}]hover a node for detail[/]"
    )

    def __init__(self, flow: tuple[tuple[str, str, str], ...]):
        super().__init__(id="flow-section")
        self.flow = flow
        self._flow_row: FlowRow | None = None

    def compose(self) -> ComposeResult:
        # Change 2: header row above the nodes
        yield Static(
            f"[bold {AMBER}]PIPELINE[/]  [{DIM}]{len(self.flow)} phases[/]",
            id="flow-header",
            markup=True,
        )
        self._flow_row = FlowRow(self.flow)
        yield self._flow_row
        # Change 2: legend row below the nodes
        yield Static(self._LEGEND, id="flow-legend", markup=True)

    def set_flow(self, flow: tuple[tuple[str, str, str], ...]) -> None:
        if flow == self.flow:
            return
        self.flow = flow
        # Update the phase count in the header
        try:
            self.query_one("#flow-header", Static).update(
                f"[bold {AMBER}]PIPELINE[/]  [{DIM}]{len(flow)} phases[/]"
            )
        except Exception:
            pass
        row = self._flow_row
        if row is None:
            return
        if row.flow == flow:
            return
        row.flow = flow
        if row.is_attached:
            row.run_worker(
                row.recompose(),
                name="recompose-workflow-flow",
                group="workflow-flow",
                exclusive=True,
                exit_on_error=False,
            )


class NavMenu(Static):
    """The nine-view switcher in the spine.

    It renders one text line per view (in ``_VIEWS`` order), so a click selects
    the view on the clicked row — the click's y within the widget *is* the row
    index. Digit and function keys still work; this adds the mouse path a Static
    lacks.
    """

    active_view: reactive[str] = reactive("atlas")

    def render(self) -> str:
        # CHANGE #2: amber accent bar on active item, muted dim on inactive,
        # uppercase labels for Bloomberg-style scan-readability.
        return "\n".join(
            (
                f"[bold {AMBER}]▐[/][bold {TEXT_HI}] "
                f"{_VIEW_KEYS[view]}  {view.upper()}[/]"
                if view == self.active_view
                else f"[{MUTED}]   {_VIEW_KEYS[view]}  {view.title()}[/]"
            )
            for index, view in enumerate(_VIEWS, start=1)
        )

    def on_click(self, event: events.Click) -> None:
        index = int(event.y)
        if 0 <= index < len(_VIEWS):
            self.app.action_view(_VIEWS[index])


class AgentRail(Static):
    """Declarative rendering of the owner-reported workforce roster."""

    rows: reactive[tuple[tuple[str, str, str], ...]] = reactive(tuple)
    pulse: reactive[int] = reactive(0)
    theme_name: reactive[str] = reactive(tokens.DEFAULT_THEME)

    def render(self) -> Text:
        rendered = Text()
        faint = tokens.role(self.theme_name, "faint")
        for index, (name, authority, state) in enumerate(self.rows):
            if index:
                rendered.append("\n")
            pulse = (_PULSE_FRAMES[self.pulse % len(_PULSE_FRAMES)]
                     if state == "working" else None)
            rendered.append_text(primitives.state_badge(
                state, glyph=pulse, fallback="idle", theme=self.theme_name))
            rendered.append(" ")
            rendered.append(name, style="bold")
            rendered.append(f"\n   {state} · {authority}", style=faint)
        return rendered


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


class AtlasDrawerScreen(ModalScreen[None]):
    """Atlas's full detail: state, triggers, task history, pending approvals.

    Read-only by construction. Approving a plan is a deliberate act through the
    owner's approvals API, never a keystroke inside a status drawer.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("ctrl+b", "cancel", "Close", show=False),
    ]
    CSS = ATLAS_DRAWER_CSS

    def __init__(self, body: str = ""):
        super().__init__()
        self.body = body

    def compose(self) -> ComposeResult:
        # Change 5: title bar + scrollable body + pinned hint footer
        with Vertical(id="atlas-drawer"):
            yield Static(
                f"[bold]ATLAS[/]  [{AMBER}]◈[/]  DESK MANAGER",
                id="atlas-drawer-title",
                markup=True,
            )
            yield Static(self.body, id="atlas-drawer-body", markup=True)
            yield Static(
                f"[{DIM}]esc[/] or [bold {AMBER}]ctrl+b[/] [{DIM}]to close[/]",
                id="atlas-drawer-hint",
                markup=True,
            )

    def action_cancel(self) -> None:
        self.dismiss(None)


class QlabTui(App[None]):
    """Border-light terminal workspace for portfolio and agent operations."""

    TITLE = "qlab operator"

    CSS = APP_CSS

    BINDINGS = [
        Binding("1", "view('atlas')", "Atlas", show=False),
        Binding("2", "view('dashboard')", "Dashboard", show=False),
        Binding("3", "view('market')", "Market", show=False),
        Binding("4", "view('workforce')", "Workforce", show=False),
        Binding("5", "view('research')", "Research", show=False),
        Binding("6", "view('book')", "Book", show=False),
        Binding("7", "view('audit')", "Audit", show=False),
        Binding("8", "view('reference')", "Reference", show=False),
        Binding("9", "view('settings')", "Settings", show=False),
        Binding("0", "view('news')", "News", show=False),
        Binding("f10", "view('news')", "News", show=False),
        Binding("f1", "view('atlas')", "Atlas", show=False),
        Binding("f2", "view('dashboard')", "Dashboard", show=False),
        Binding("f3", "view('market')", "Market", show=False),
        Binding("f4", "view('workforce')", "Workforce", show=False),
        Binding("f5", "view('research')", "Research", show=False),
        Binding("f6", "view('book')", "Book", show=False),
        Binding("f7", "view('audit')", "Audit", show=False),
        Binding("f8", "view('reference')", "Reference", show=False),
        Binding("f9", "view('settings')", "Settings", show=False),
        Binding("a", "agent_focus", "Agents", show=False),
        Binding("j", "next_symbol", "Next symbol", show=False),
        Binding("k", "previous_symbol", "Previous symbol", show=False),
        Binding("colon", "command", "Command", show=False),
        Binding("ctrl+p", "command", "Command", show=False),
        Binding("tilde", "timeline", "Timeline", show=False),
        Binding("ctrl+b", "atlas_drawer", "Atlas", show=False),
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
        desk_mode: DeskMode | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        # Before anything else: the stylesheet is parsed during startup and
        # references these variables, so registering in on_mount is too late and
        # raises UnresolvedVariableError on `$bg`.
        self._register_design_themes()
        self.client = client
        # None means "nobody has said yet" — the operator is asked on mount. A
        # mode from a flag is authoritative and skips the question entirely; it
        # also owns the data lane, so ``offline`` can never contradict the mode
        # the chip is about to report.
        self.desk_mode = desk_mode
        self.offline = offline if desk_mode is None else desk_mode.offline
        self._desk_mode_prompted = False
        # Set when the owner would not accept a chosen mode: the two are then
        # trading different desks and the chip has to say so.
        self._desk_mode_error: str | None = None
        self.refresh_interval = refresh_interval
        self.owned_server = owned_server
        self.claude_start = claude_start
        # The desk opens on Atlas: the manager is the front door, not a tab.
        self.active_view = "atlas"
        # Placeholder until the first snapshot; the owner's mandate universe
        # (market.assets) replaces it so a config change never desyncs the TUI.
        self.universe_tickers: list[str] = list(_DEFAULT_TICKERS)
        self.active_ticker = self.universe_tickers[0]
        self.snapshot: dict[str, Any] = {}
        self._snapshot_initialized = False
        self._closing = False
        self._refreshing = False
        self._action_running = False
        self._last_snapshot_at: float | None = None
        self._refresh_failures = 0
        self._event_ids: set[str] = set()
        self._event_id_order: deque[str] = deque()
        self._runs_signature: tuple = ()
        self._audit_signature: tuple = ()
        self._audit_decisions: dict[str, dict] = {}
        self._book_plan_ids: dict[str, str] = {}
        self.bootstrap: dict[str, Any] | None = None
        self._bootstrap_started = False
        self._bootstrap_error = ""
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
        # The workflow this TUI launched or explicitly resumed. Unlike
        # _active_workflow_id, this never points at passive history and is safe
        # to interrupt automatically when the owned coordinator exits.
        self._launched_workflow_id = ""
        # A stop can race workflow.start: the coordinator process is already
        # ours, but the durable id has not reached the event stream yet. Keep
        # the requested transition and apply it as soon as the id is observed.
        self._pending_workflow_control: tuple[str, str] | None = None
        self._pending_workflow = False
        self._seen_workflow_ids: set[str] = set()
        self._phase_reported: dict[str, str] = {}
        # Unresolved console markup, bounded to the RichLog's own capacity so
        # the two retain the same history. Kept for theme re-rendering.
        self._console_lines: deque[str] = deque(maxlen=400)
        self._results_printed = False
        self._pulse = 0
        self._live_stream_stop = threading.Event()
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
            offline=self.offline,
        )

    def compose(self) -> ComposeResult:
        with Horizontal(id="workspace"):
            with Vertical(id="spine"):
                # CHANGE #2: Bloomberg-style wordmark — amber ticker name + muted descriptor
                yield Static(
                    f"[bold {AMBER_HI}]QLAB[/]  [bold {AMBER}]◈[/]\n"
                    f"[{MUTED}]OPERATOR CONSOLE[/]",
                    id="wordmark", markup=True,
                )
                yield NavMenu(id="nav", markup=True)
                yield Static("UNIVERSE", id="universe-label")
                yield ListView(
                    *(ListItem(Label(ticker)) for ticker in _DEFAULT_TICKERS),
                    id="universe",
                )

            with ContentSwitcher(initial="atlas", id="canvas"):
                with Vertical(id="atlas", classes="canvas-view"):
                    # Change 4: canvas-title stays as the tab label
                    yield Static(
                        f"[{AMBER}]▍[/] ATLAS · DESK MANAGER",
                        classes="canvas-title",
                        markup=True,
                    )
                    # Change 4: pinned status strip — mode/state/conviction chips
                    yield Static(id="atlas-status-strip", markup=True)
                    # Buttons before the scroll region so they are never cut off
                    with Horizontal(id="atlas-actions"):
                        yield Button(
                            "REFRESH READ",
                            id="btn-atlas-refresh",
                            classes="view-action-button",
                            compact=True,
                        )
                        yield Button(
                            "ESCALATE DEBATE",
                            id="btn-atlas-escalate",
                            classes="view-action-button",
                            compact=True,
                        )
                        yield Button(
                            "OBSERVE NOW",
                            id="btn-atlas-observe",
                            classes="view-action-button",
                            compact=True,
                        )
                        yield Button(
                            "MODE",
                            id="btn-atlas-mode",
                            classes="view-action-button",
                            compact=True,
                        )
                        yield Button(
                            "AUTONOMY",
                            id="btn-atlas-autonomy",
                            classes="view-action-button",
                            compact=True,
                        )
                    # Scrollable read content below the always-visible buttons
                    with Vertical(id="atlas-read-scroll"):
                        yield Static(id="atlas-read", markup=True)

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
                    # Change 1: split layout — braille chart column + stat sidebar
                    with Horizontal(id="market-split"):
                        with Vertical(id="market-chart-col"):
                            yield Static(id="market-chart", markup=True)
                        with Vertical(id="market-stats-col"):
                            yield Static(id="market-stats-header", markup=True)
                            yield Static(id="market-stats-body", markup=True)
                    # kept for any code path that resolves it; display:none in CSS
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
                        yield Button(
                            "ABANDON",
                            id="btn-workforce-abandon",
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
                    # Every plan card is hidden when there are none, which left
                    # this heading standing over nothing while ORDERS below it
                    # said so plainly. An empty section reads as a broken one.
                    yield Static(
                        id="book-plans-empty",
                        classes="book-section",
                        markup=True,
                    )
                    yield Static("ORDERS · NEWEST 10", classes="book-section-title")
                    yield Static(
                        id="book-orders",
                        classes="book-section",
                        markup=True,
                    )
                with Vertical(id="news", classes="canvas-view"):
                    yield Static(f"[{AMBER}]\u258d[/] NEWS", classes="canvas-title",
                                 markup=True)
                    yield Static(id="news-summary", markup=True)
                    with Vertical(id="news-scroll"):
                        yield Static(id="news-stories", markup=True)
                with Vertical(id="audit", classes="canvas-view"):
                    yield Static(f"[{AMBER}]\u258d[/] AUDIT", classes="canvas-title", markup=True)
                    yield Static(id="audit-summary", markup=True)
                    yield DataTable(id="audit-table", cursor_type="row")
                yield ReferenceView(id="reference", classes="canvas-view")
                with Vertical(id="settings", classes="canvas-view"):
                    yield Static(f"[{AMBER}]\u258d[/] SETTINGS", classes="canvas-title", markup=True)
                    # The desk mode is the only setting an operator changes at
                    # runtime, and it was reachable solely from a modal shown
                    # once at startup \u2014 so a session that answered it, or was
                    # started with a flag, had no way back to it. This card is
                    # that way back, and it is first because "whose book is
                    # this" outranks everything else on the page.
                    with Vertical(id="settings-desk", classes="settings-card"):
                        yield Static(id="settings-desk-copy", markup=True)
                        with Horizontal(id="settings-desk-actions"):
                            yield Button(
                                "change desk mode",
                                id="settings-change-desk",
                                classes="view-action-button",
                                compact=True,
                            )
                            yield Button(
                                "re-check alpaca",
                                id="settings-recheck-alpaca",
                                classes="view-action-button",
                                compact=True,
                            )
                    # How the workforce runs, next to whose book it runs on.
                    # Autonomy has a control in the Atlas panel; fast mode and
                    # owner-driven coordination had none at all, so the only way
                    # to reach either was an env var set before launch.
                    with Vertical(id="settings-workforce",
                                  classes="settings-card"):
                        yield Static(id="settings-workforce-copy", markup=True)
                        with Horizontal(id="settings-workforce-actions"):
                            yield Button(
                                "toggle fast mode",
                                id="settings-toggle-fast",
                                classes="view-action-button",
                                compact=True,
                            )
                    yield Static(
                        id="settings-mandate",
                        classes="settings-card",
                        markup=True,
                    )
                    yield Static(
                        id="settings-system",
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
                # Atlas sits at the top of the rail because the desk manager is
                # always present: its mode is the standing authority statement,
                # visible in every view rather than only where work happens.
                yield Static("ATLAS · DESK MANAGER", id="atlas-label")
                yield Static(
                    "waiting for runtime snapshot",
                    id="atlas-rail",
                    markup=True,
                )
                yield Static("AGENTS", id="agent-label")
                yield AgentRail(id="agent-list", markup=True)
                yield Static("SELECTED WORK", id="work-label")
                yield Static(
                    "No active workforce.\n\nUse [bold]: workforce GOAL[/] to let "
                    "Claude coordinate the five governed qlab roles.",
                    id="selected-work",
                    markup=True,
                )

        yield RichLog(id="timeline", wrap=True, markup=False, max_lines=500)
        # CHANGE #1: Bloomberg-style status bar — command prompt + chip strip
        with Horizontal(id="command-row"):
            yield Input(placeholder="  : command  ·  ctrl+p  ·  0-9 views  ·  j/k symbol", id="command")
            yield Static("CONNECTING", id="conn-chip", markup=True)
            yield Static("CONNECTING", id="mode-chip")

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
        # A launcher flag is authoritative and skips the chooser, so mount is
        # the only chance to paint the chip before the first snapshot — and if
        # the owner never answers, it is the only chance at all.
        self._render_mode_chip()
        self._start_refresh()
        if self.refresh_interval > 0:
            self.set_interval(self.refresh_interval, self._start_refresh)
            self.set_interval(0.25, self._tick_pulse)
        self._start_live_stream()
        if self.desk_mode is None and not self._desk_mode_prompted:
            self._desk_mode_prompted = True
            self._start_desk_mode_prompt()

    def _register_design_themes(self) -> None:
        """Make the design themes selectable and adopt the default.

        Registering here rather than in `on_mount` is required: the stylesheet
        is parsed during startup and references these variables, so a later
        registration raises UnresolvedVariableError on `$bg`.

        A switch repaints chrome as well as content — `qlab.tui.theme`
        substitutes token *references* rather than frozen literals, so the CSS
        reaches Textual as `$bg` and follows the active theme.
        """
        for theme in tokens.THEMES.values():
            self.register_theme(theme)
        self.theme = tokens.DEFAULT_THEME

    def action_claude_cli(self, args: str = "") -> None:
        """Drop out of the TUI and run the real Claude CLI, then come back.

        The governed workforce is deliberately terse — one short note per agent,
        no raw model output — which keeps the console readable but means there is
        no way to watch Claude think. This is that way: Textual releases the
        terminal, `claude` owns it exactly as it would in a shell, and the desk
        resumes when you exit.

        The owner keeps running throughout, so a session started here reaches
        the same desk over HTTP and the same governance applies to it. It is not
        a back door: it is the ordinary CLI, with no qlab authority of its own
        beyond what the MCP config already grants.
        """
        binary = shutil.which("claude")
        if binary is None:
            self._write_local_event("claude.cli_missing", {})
            self._set_selected_work(
                "CLAUDE CLI\n\nNot found on PATH. Install Claude Code, or use "
                ": workforce GOAL for the governed pipeline.")
            return
        argv = [binary, *shlex.split(args)] if args.strip() else [binary]
        self._write_local_event("claude.cli_opened", {"args": args.strip()})
        try:
            with self.suspend():
                subprocess.call(argv, cwd=str(_WORKSPACE_ROOT))
        except Exception as exc:
            self._write_local_event("claude.cli_failed", {"error": repr(exc)})
            self._set_selected_work(f"CLAUDE CLI FAILED\n\n{exc!r}")
            return
        # The desk may have moved while the terminal was elsewhere.
        self._set_selected_work(
            "CLAUDE CLI\n\nSession ended; the desk is back. Anything that "
            "session persisted is in the next snapshot.")
        self._start_refresh()

    def action_theme(self, name: str = "") -> None:
        """Switch the active design theme, or list the choices when unnamed."""
        wanted = name.strip()
        if wanted not in tokens.THEMES:
            self._write_local_event(
                "theme.rejected", {"requested": wanted})
            self._set_selected_work(
                "THEME\n\n"
                + ("Name a theme.\n\n" if not wanted
                   else f"Unknown theme {wanted!r}.\n\n")
                + "\n".join(f"  : theme {known}" for known in tokens.THEMES)
            )
            return
        self.theme = wanted
        # State down: the renderers are told the theme, they never read it.
        for node in self.query(FlowNode):
            node.theme_name = wanted
        for rail in self.query(AgentRail):
            rail.theme_name = wanted
        self._repaint_console()
        self._write_local_event("theme.changed", {"theme": wanted})
        self._set_selected_work(f"THEME\n\n{wanted} is active.")

    def on_unmount(self) -> None:
        self._closing = True
        self._live_stream_stop.set()
        owned_workflow = (
            self._launched_workflow_id
            if self.claude.mode == "workforce" and self.claude.running
            else ""
        )
        self.claude.stop("TUI closed before the coordinator completed")
        if owned_workflow:
            try:
                post_control = getattr(
                    self.client, "post_control", self.client.post)
                post_control(
                    f"/api/workflows/{owned_workflow}/interrupt",
                    {"reason": "TUI closed before the coordinator completed"},
                )
            except Exception:
                # The owned server may already be exiting. Its next startup
                # performs the same orphan recovery before serving snapshots.
                pass
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
    def _call_from_worker(self, callback, *args) -> None:
        """Marshal a worker result only while Textual owns its message loop."""
        if self._closing:
            return
        try:
            self.call_from_thread(callback, *args)
        except RuntimeError:
            # A request may finish after unmount begins. Its result belongs in
            # the next owner snapshot, not in this retired widget tree.
            return

    def _start_refresh(self) -> None:
        # A running owner action holds the owner's dispatch lock; polling
        # /api/tui behind it would only pile up timeouts in the timeline.
        if self._refreshing or self._action_running:
            return
        self._refreshing = True

        def run() -> None:
            # The fetch and the repaint are separated because they fail for
            # different reasons and only one of them is the owner's fault.
            # `call_from_thread` re-raises renderer exceptions here, so a
            # repaint bug used to count toward `_refresh_failures` and, after
            # three ticks, tell the operator OWNER DOWN about a healthy owner.
            try:
                snapshot = gather_snapshot(self.client, offline=self.offline)
            except Exception as exc:
                self._call_from_worker(
                    self._write_local_event, "api.error", {"error": repr(exc)})
                self._call_from_worker(self._note_refresh_failure)
                self._call_from_worker(self._finish_refresh)
                return
            try:
                self._call_from_worker(self._apply_snapshot, snapshot)
            except Exception as exc:
                self._call_from_worker(
                    self._write_local_event, "render.error", {"error": repr(exc)})
            finally:
                self._call_from_worker(self._finish_refresh)

        threading.Thread(
            target=run,
            daemon=True,
            name="qlab-tui-refresh",
        ).start()

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

    # -- desk mode --------------------------------------------------------
    def _start_desk_mode_prompt(self) -> None:
        """Fetch credential status off-thread, then ask.

        The probe reaches the owner, which resolves a credential; that must never
        block the UI, so it follows the same worker shape as the atlas fetch. A
        probe that cannot answer is reported as "no usable credential", never as
        a working one.
        """

        def run() -> None:
            try:
                payload = self.client.get("/api/desk_mode")
            except Exception as exc:
                payload = {"credentials": f"owner unreachable: {exc!r}",
                           "credentials_ok": False}
            self.call_from_thread(self._ask_desk_mode, payload)

        threading.Thread(target=run, daemon=True).start()

    def _ask_desk_mode(self, payload: dict) -> None:
        def chosen(mode: DeskMode | None) -> None:
            if mode is None:
                return
            self.desk_mode = mode
            self.offline = mode.offline
            # The workforce reads its own flag when a coordinator spawns, and it
            # is stamped onto every owner call the MCP proxy makes. Leaving it
            # behind would run governed reviews on synthetic data under a LIVE
            # chip — the one contradiction this screen exists to prevent.
            self.claude.offline = mode.offline
            # The snapshot in hand predates this choice and the chip prefers it,
            # so a real book would read as the muted demo until the next poll.
            if self.snapshot:
                self.snapshot.pop("desk_mode", None)
            self._post_desk_mode(mode)
            self._render_mode_chip()
            self._start_refresh()

        self.push_screen(
            DeskModeScreen(
                credentials=str(payload.get("credentials", "")),
                credentials_ok=bool(payload.get("credentials_ok")),
            ),
            chosen,
        )

    def _post_desk_mode(self, mode: DeskMode) -> None:
        """Tell the owner which desk it is serving; it holds the book lane."""

        def run() -> None:
            try:
                self.client.post("/api/desk_mode",
                                 {"data": mode.data, "book": mode.book})
            except Exception as exc:
                self.call_from_thread(self._note_desk_mode_failure, repr(exc))
            else:
                self.call_from_thread(self._clear_desk_mode_failure)

        threading.Thread(target=run, daemon=True).start()

    def _note_desk_mode_failure(self, error: str) -> None:
        """Make a rejected mode visible, without taking the desk down.

        ``chosen()`` has already committed the mode here, so the TUI is now
        sending ``offline`` and running the workforce against a desk the owner
        never accepted. The CLI's attached-owner path exits loudly on the same
        failure; this path cannot, so the always-visible chip carries the error
        instead of a timeline line that scrolls away.
        """
        self._desk_mode_error = error
        self._write_local_event("desk_mode.error", {"error": error})
        self._render_mode_chip()

    def _clear_desk_mode_failure(self) -> None:
        if self._desk_mode_error is None:
            return
        self._desk_mode_error = None
        self._render_mode_chip()

    def _reconcile_desk_mode(self, snapshot: dict) -> None:
        """Retire a rejected-mode error once the owner is demonstrably in sync.

        A refused POST may still have applied, or the operator may have retuned
        the owner another way. The snapshot is the owner's own answer about
        which desk it is serving, so a match ends the disagreement — leaving the
        error up after that would be its own misread.
        """
        if self._desk_mode_error is None or self.desk_mode is None:
            return
        mode = snapshot.get("desk_mode") or {}
        agrees = (
            str(mode.get("data", "")).strip().lower() == self.desk_mode.data
            and str(mode.get("book", "")).strip().lower() == self.desk_mode.book)
        if agrees:
            self._desk_mode_error = None

    def _start_bootstrap(self) -> None:
        """Fetch immutable owner configuration once, when Settings is first shown."""
        if self._bootstrap_started:
            return
        self._bootstrap_started = True
        self._render_settings()

        def run() -> None:
            try:
                bootstrap = self.client.get("/api/bootstrap")
                self._call_from_worker(self._finish_bootstrap, bootstrap, "")
            except Exception as exc:
                self._call_from_worker(
                    self._finish_bootstrap, None, repr(exc))

        threading.Thread(
            target=run,
            daemon=True,
            name="qlab-tui-bootstrap",
        ).start()

    def _finish_bootstrap(
        self,
        bootstrap: dict[str, Any] | None,
        error: str,
    ) -> None:
        self.bootstrap = bootstrap
        self._bootstrap_error = error
        self._render_settings()

    def _start_reference_fetch(self) -> None:
        """Fetch the curated catalog on every visit to Reference.

        The payload carries live ablation evidence, and ``: batch`` writes new
        ablation numbers mid-session. A once-per-session fetch would keep
        asserting "latest ablation" with superseded numbers while the leaderboard
        on the same evidence refreshed on the next tick. ``ReferenceView.set_entries``
        ignores an unchanged payload, so re-entry costs one request and no
        rebuild flicker.
        """

        def run() -> None:
            try:
                payload = self.client.get("/api/reference")
                self._call_from_worker(self._finish_reference, payload, "")
            except Exception as exc:
                self._call_from_worker(
                    self._finish_reference, None, repr(exc))

        threading.Thread(
            target=run,
            daemon=True,
            name="qlab-tui-reference",
        ).start()

    def _finish_reference(self, payload: dict[str, Any] | None, error: str) -> None:
        view = self.query_one("#reference", ReferenceView)
        if payload is None:
            self.query_one("#reference-detail", Static).update(
                f"[{DOWN}]reference unavailable: {escape(error)}[/]")
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
            while not self._live_stream_stop.is_set():
                try:
                    for event in self.client.stream(
                        "/api/stream",
                        stop_event=self._live_stream_stop,
                    ):
                        if self._live_stream_stop.is_set():
                            return
                        self._call_from_worker(self._apply_live_event, event)
                except Exception:
                    pass  # owner restarting or unreachable; retry quietly
                self._live_stream_stop.wait(2.0)

        threading.Thread(
            target=run,
            daemon=True,
            name="qlab-tui-events",
        ).start()

    def _apply_live_event(self, event: dict) -> None:
        # One bad event must not kill the subscription: an exception raised
        # here propagates back through call_from_thread into the reader
        # thread, and before this guard a single malformed frame dropped the
        # desk into a silent reconnect loop forever. Loud-but-alive: the strip
        # names the bad frame and the stream keeps delivering.
        try:
            self._dispatch_live_event(event)
        except Exception as exc:
            self._write_local_event(
                "event.malformed",
                {"kind": str(event.get("kind", "")),
                 "error": repr(exc)[:120]})

    def _dispatch_live_event(self, event: dict) -> None:
        kind = str(event.get("kind", ""))
        if kind == "stream.malformed":
            # The client already decoded what it could; surface, don't parse.
            self._write_local_event("event.malformed", event.get("payload") or {})
            return
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
    def _active_theme(self) -> str:
        """The design theme to render against.

        Textual's own themes remain selectable, so `self.theme` is not
        guaranteed to name one of ours; falling back keeps a renderer from
        raising on a theme the design system does not publish.
        """
        return self.theme if self.theme in tokens.THEMES else tokens.DEFAULT_THEME

    def _console_write(self, line: str) -> None:
        # RichLog renders through rich.text.Text.from_markup, which knows
        # nothing about theme variables, so they are resolved here against the
        # active theme rather than reaching Rich as a literal `$name`.
        # The unresolved line is kept so a theme switch can re-render it: a
        # colour resolved at write time is frozen, and the run narrative would
        # otherwise stay in the old palette — dark-theme hex on a light canvas
        # is about 1:1 contrast, i.e. the whole run output goes invisible.
        self._console_lines.append(line)
        self.query_one("#workforce-console", RichLog).write(
            _resolve_markup(line, theme=self._active_theme()))

    def _repaint_console(self) -> None:
        """Re-render the kept console history against the active theme."""
        try:
            console = self.query_one("#workforce-console", RichLog)
        except Exception:
            return
        console.clear()
        theme = self._active_theme()
        for line in self._console_lines:
            console.write(_resolve_markup(line, theme=theme))

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

    def _note_workflow_started(self, payload: dict) -> None:
        """Bind a pending launch before its first phase update can race a stop."""
        workflow_id = str(payload.get("workflow_id") or "")
        if (
            self._pending_workflow
            and workflow_id
            and workflow_id not in self._seen_workflow_ids
        ):
            self._active_workflow_id = workflow_id
            self._launched_workflow_id = workflow_id
            self._pending_workflow = False
            self._apply_pending_workflow_control(workflow_id)

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
        else:
            self._active_workflow_id = workflow_id
            self._launched_workflow_id = workflow_id
            self._pending_workflow = False
            self._apply_pending_workflow_control(workflow_id)
        if self._phase_reported.get(phase) == status:
            return
        self._phase_reported[phase] = status
        if status == "working":
            self._console_write(
                f"[{CYAN}]▶ {escape(_phase_short(phase))}[/] "
                f"[{LABEL_GOLD}]working[/]")
            return
        if status not in (
            "done", "failed", "blocked", "interrupted", "abandoned",
        ):
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
            "interrupted": ("Ⅱ", GOLD),
            "abandoned": ("×", MUTED),
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
        if self._snapshot_initialized and snapshot:
            self._render_active_snapshot_view()
        else:
            # Prime every canvas once, and clear every canvas if the owner
            # returns no state so a hidden view cannot retain stale figures.
            # Ordinary later polls repaint only the visible canvas.
            self._render_dashboard()
            self._render_market()
            self._render_workforce()
            self._render_research()
            self._render_book()
            self._render_audit()
            self._render_settings()
            self._render_atlas()
            self._snapshot_initialized = True
        if not self._action_running:
            self._agent_states = {
                str(agent.get("name")): str(agent.get("state", "idle"))
                for agent in snapshot.get("agents", [])
            }
        self._render_agents()
        self._render_atlas_rail()
        self._reconcile_desk_mode(snapshot)
        self._render_mode_chip()
        self._ingest_events(snapshot.get("events", []))
        self._maybe_offer_workforce()

    def _render_active_snapshot_view(self) -> None:
        """Render the visible canvas from the latest owner snapshot."""
        renderers = {
            "atlas": self._render_atlas,
            "dashboard": self._render_dashboard,
            "market": self._render_market,
            "workforce": self._render_workforce,
            "research": self._render_research,
            "book": self._render_book,
            "news": self._render_news,
            "audit": self._render_audit,
            "settings": self._render_settings,
        }
        renderer = renderers.get(self.active_view)
        if renderer is not None:
            renderer()

    # -- renderers --------------------------------------------------------
    def _render_nav(self) -> None:
        self.query_one("#nav", NavMenu).active_view = self.active_view

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
            # Two samples per cell, so twice the history in the same width.
            pulse = spark(history[-24:], width=12) or "—"
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
        # A flat book renders as a full tile of em-dashes: maximum space for
        # one fact, and it reads as missing data. Say it instead — but only
        # when the weights are actually KNOWN to be zero. An absent weight is
        # not a zero one, and a sparse payload must keep reading as unknown.
        weights_known = [_finite_number(current.get(t))
                         for t in self.universe_tickers]
        if (weights_known and all(w == 0.0 for w in weights_known)
                and not held_outside):
            allocation_content = (
                f"[{MUTED}]No positions held.[/]\n\n"
                f"[{DIM}]The book is flat across all "
                f"{len(self.universe_tickers)} mandate assets. A dry rebalance "
                f"proposes targets; nothing is booked without your "
                f"confirmation.[/]"
            )
        else:
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
            _HMM_STATE_TONE.get(regime_name.lower(), TEXT_HI),
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
                        f"[bold {_HMM_STATE_TONE[state]}]{token}[/]"
                    )
                else:
                    posterior_parts.append(f"[{MUTED}]{token}[/]")
            if posterior_parts:
                state_tone = _HMM_STATE_TONE.get(selected, AMBER)
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

    def _render_atlas(self) -> None:
        """The desk-manager view: Atlas's read, in the order a human asks it.

        Conclusion first (what does this add up to), then the tensions that
        make it interesting, then the evidence under them.
        """
        read = _record(self.snapshot.get("atlas_read"))
        atlas = _record(self.snapshot.get("atlas"))
        beat = _record(self.snapshot.get("atlas_heartbeat"))
        target = self.query_one("#atlas-read", Static)
        strip = self.query_one("#atlas-status-strip", Static)

        # Change 4: always populate the status strip from atlas + beat, even if
        # there is no read yet — the operator always knows Atlas's live state.
        if atlas:
            mode = str(atlas.get("mode", "—")).upper()
            state = str(atlas.get("state", "—")).upper()
            state_tone = _ATLAS_STATE_TONES.get(state.lower(), TEXT_HI)
            mode_tone = AMBER if atlas.get("mode") == "propose" else TEXT_HI
            # Incorporate the merge's beat_errors so the strip reflects health
            _beat_errors = int(beat.get("errors", 0) or 0)
            if not beat.get("running"):
                beat_icon = f"[{MUTED}]◎ STOPPED[/]"
            elif _beat_errors:
                beat_icon = f"[{DOWN}]⚠ FAILING[/]"
            else:
                beat_icon = f"[{UP}]◉ LIVE[/]"
            auto_icon = (f"[{AMBER}]AUTO ON[/]" if beat.get("autonomous")
                         else f"[{MUTED}]AUTO OFF[/]")
            conviction = _finite_number(read.get("conviction")) if read else None
            conv_tone = (UP if (conviction or 0) >= 0.6
                         else AMBER if (conviction or 0) >= 0.35 else MUTED)
            conv_text = pct(conviction) if conviction is not None else "—"
            strip.update(
                # No brackets around the value: Rich reads `[OBSERVE]` as a
                # style tag and drops it, so the strip said "MODE" and then the
                # state — the authority statement, silently missing.
                f"[bold {mode_tone}]MODE {mode}[/]"
                f"  [{DIM}]│[/]  "
                f"[bold {state_tone}]{state}[/]"
                f"  [{DIM}]│[/]  "
                f"[{LABEL_GOLD}]conviction [/][bold {conv_tone}]{conv_text}[/]"
                f"  [{DIM}]│[/]  "
                f"{beat_icon}  {auto_icon}"
            )
        else:
            strip.update(f"[{MUTED}]waiting for atlas snapshot…[/]")

        if not read:
            # No read is the case that most needs the explanation, not least:
            # a fresh desk showed one muted sentence and nothing about what
            # Atlas may do or why it is quiet.
            target.update("\n".join([
                f"[{MUTED}]Atlas has not composed a read yet. "
                "The heartbeat writes one on its first tick.[/]",
                "",
                *self._atlas_why_lines(beat),
            ]))
            return

        agreement = str(read.get("agreement", "—"))
        conviction = _finite_number(read.get("conviction"))
        tone = str((read.get("news") or {}).get("tone", "—"))
        quant = str(read.get("quantitative_state", "—"))
        agreement_tone = {
            "divergent": AMBER, "aligned": UP, "quiet": MUTED,
        }.get(agreement, TEXT_HI)

        # Change 4: "THE READ" section header uses the richer h1-style format
        rule = f"[{BORDER_HI}]{'─' * 48}[/]"
        lines = [
            f"[bold {AMBER}]▌ THE READ[/]  [{DIM}]as of {read.get('as_of','—')}[/]",
            rule,
        ]
        lines.extend(_key_number_markup(
            [
                ("SIGNALS", quant.upper()),
                ("NEWS TONE", tone.replace("_", " ").upper()),
                ("AGREEMENT", agreement.upper()),
                ("CONVICTION", pct(conviction) if conviction is not None else "—"),
            ],
            value_tones=[TEXT_HI, TEXT_HI, agreement_tone,
                         UP if (conviction or 0) >= 0.6
                         else AMBER if (conviction or 0) >= 0.35 else MUTED],
            bold_values={2, 3},
        ))

        if read.get("news_error"):
            lines.append("")
            lines.extend(_bulletin_markup(
                [f"NEWS FEED UNAVAILABLE — {read['news_error']}",
                 "The qualitative side of this read is missing, not quiet."],
                tone=DOWN, max_len=200))

        qual = _record(read.get("qualitative_signals"))
        if qual.get("signals"):
            lines.append("")
            lines.append(f"[bold {AMBER}]\u258c WHAT THE RECORD COVERS[/]")
            lines.append(f"[{BORDER_HI}]{'\u2500' * 48}[/]")
            if not qual.get("sufficient"):
                # Absence stated, never rendered as a calm reading.
                lines.append(
                    f"[{AMBER}]window too thin to interpret[/]  [{DIM}]"
                    f"{int(qual.get('item_count') or 0)} record(s); "
                    f"{int(qual.get('min_items') or 0)} needed before a ratio "
                    f"is a measurement rather than one story.[/]")
            named = _record(qual.get("by_name"))
            for key, label in (("coverage_breadth", "HOLDINGS NAMED"),
                               ("asset_class_reach", "CLASSES COVERED"),
                               ("corroboration_ratio", "CORROBORATED")):
                sig = _record(named.get(key))
                value = sig.get("value")
                lines.extend(_key_number_markup(
                    [(label, "\u2014" if value is None else pct(value))],
                    value_tones=[MUTED if value is None
                                 else UP if value >= 0.6
                                 else AMBER if value >= 0.3 else DOWN]))
                if sig.get("reason"):
                    lines.append(f"    [{DIM}]{escape(str(sig['reason']))}[/]")

        tensions = [str(t) for t in (read.get("tensions") or [])]
        if tensions:
            lines.append("")
            lines.append(f"[bold {AMBER}]▌ TENSIONS[/]  [{DIM}]where the evidence disagrees[/]")
            lines.append(rule)
            lines.extend(_bulletin_markup(tensions, tone=AMBER, max_len=220))

        observations = [str(o) for o in (read.get("observations") or [])]
        if observations:
            lines.append("")
            lines.append(f"[bold {AMBER}]▌ OBSERVATIONS[/]")
            lines.append(rule)
            lines.extend(_bulletin_markup(observations, tone=TEXT, max_len=200))

        changers = [str(c) for c in (read.get("would_change_my_mind") or [])]
        if changers:
            lines.append("")
            lines.append(f"[bold {AMBER}]▌ WOULD CHANGE THIS[/]")
            lines.append(rule)
            lines.extend(_bulletin_markup(changers, tone=MUTED, max_len=200))

        supported = read.get("supported_claims") or []
        if supported:
            lines.append("")
            lines.append(f"[bold {AMBER}]▌ WELL-SUPPORTED CLAIMS[/]  "
                         f"[{DIM}]primary documents or multi-publisher[/]")
            lines.append(rule)
            for claim in supported[:5]:
                tier_tone = UP if claim.get("tier") == "primary" else CYAN
                lines.append(
                    f"  [{tier_tone}]›[/] {str(claim.get('headline',''))[:88]} "
                    f"[{DIM}]({claim.get('support','')})[/]")

        grounding = read.get("grounding") or {}
        for flag in (grounding.get("quality_flags") or [])[:3]:
            lines.extend(_bulletin_markup([str(flag)], tone=AMBER, max_len=140))

        headlines = (read.get("news") or {}).get("headlines") or []
        if headlines:
            lines.append("")
            lines.append(f"[bold {AMBER}]▌ QUALITATIVE RECORD[/]  "
                         f"[{DIM}]everything in the window[/]")
            lines.append(rule)
            for item in headlines[:6]:
                item_tone = {
                    "risk_off": DOWN, "risk_on": UP, "mixed": AMBER,
                }.get(str(item.get("tone")), DIM)
                tickers = ",".join(item.get("tickers") or [])[:20]
                lines.append(
                    f"  [{item_tone}]›[/] {str(item.get('headline',''))[:96]} "
                    f"[{DIM}]{item.get('source','')} {tickers}[/]")

        lines.append("")
        # Supervisor error alert: shown in the scrollable body (not just the
        # strip) so the operator can read the full message without truncation.
        beat_errors = int(beat.get("errors", 0) or 0)
        if beat_errors and beat.get("last_error"):
            lines.append(
                f"[{DOWN}]⚠ supervisor error: "
                f"{escape(str(beat.get('last_error'))[:160])}[/]")
        # The tick count alone cannot answer "is the supervisor working" — a
        # thread that is alive but failing every tick, and one that is doing its
        # job, both just count. The health word is the answer, so it stays here
        # in the body next to the read it produced, not only in the rail.
        lines.append("")
        lines.extend(self._atlas_why_lines(beat))
        lines.append("")

        if not beat.get("running"):
            beat_health = "stopped"
        elif beat_errors:
            beat_health = f"FAILING ({beat_errors} errors)"
        else:
            beat_health = "live"
        lines.append(
            f"[{DIM}]news {read.get('news_source','—')} · "
            f"heartbeat {beat_health} ({int(beat.get('ticks', 0))} ticks) · "
            f"advisory, never an instruction — Atlas cannot trade[/]")
        target.update("\n".join(lines))

    def _atlas_why_lines(self, beat: dict) -> list[str]:
        """Why the supervisor is not doing anything, stated rather than implied.

        A desk that is deliberately idle and one that is broken look identical
        from outside, and "OBSERVING" answers neither — it is a state, not a
        reason. Atlas is deterministic code, so this IS its reasoning: the
        authority it holds, and what has fired under it.
        """
        atlas = _record(self.snapshot.get("atlas")) if self.snapshot else {}
        mode_now = str(atlas.get("mode") or "—").lower()
        autonomous = bool(beat.get("autonomous"))
        open_tasks = int(atlas.get("open_tasks") or 0)

        why: list[str] = []
        if mode_now == "observe":
            why.append("Mode is OBSERVE: Atlas may start no workflow at all. "
                       "Raise it with the MODE control to let it act.")
        elif mode_now == "paused":
            why.append("Mode is PAUSED: monitoring continues, no new work is "
                       "created.")
        elif not autonomous:
            why.append(f"Mode is {mode_now.upper()}, which permits work, but "
                       "autonomy is OFF — Atlas queues tasks and waits for you "
                       "to start them.")
        else:
            why.append(f"Mode is {mode_now.upper()} and autonomy is ON: Atlas "
                       "starts the work its mode permits on each heartbeat.")
        if open_tasks:
            why.append(f"{open_tasks} task(s) queued — ctrl+b opens the drawer "
                       "with each one and whether it is startable.")
        else:
            why.append("No trigger has fired: no drawdown tier, no drift "
                       "breach, no regime flip, no data outage. Nothing to act "
                       "on is not the same as idle by accident.")
        # A dispatched workflow only advances while a coordinator walks its
        # phases. Reporting that separately is what distinguishes "Claude is
        # reasoning right now" from "a workflow row exists and is parked".
        coordinator = _record(beat.get("coordinator"))
        if coordinator.get("driving"):
            why.append(
                f"A Claude coordinator is driving workflow "
                f"{coordinator.get('workflow_id', '—')} right now — its "
                "reasoning streams into the console as it runs.")
        elif not coordinator.get("can_drive") and coordinator.get("reason"):
            why.append(f"Atlas cannot drive a run itself: "
                       f"{coordinator['reason']}. Dispatched work waits for you "
                       "to resume it with `: workforce`.")
        why.append("Atlas itself is deterministic code, not a model — its own "
                   "decisions have no prose to stream. The judgment runs in the "
                   "coordinator it dispatches; `: workforce GOAL` starts one by "
                   "hand, and `: claude` opens the real Claude CLI here.")
        return [
            f"[bold {AMBER}]▌ WHY NOTHING IS RUNNING[/]",
            f"[{BORDER_HI}]{'─' * 48}[/]",
            *_bulletin_markup(why, tone=TEXT, max_len=240),
        ]

    def action_atlas_drawer(self) -> None:
        """Ctrl+B: open Atlas's detail drawer over the current view."""
        if isinstance(self.screen, AtlasDrawerScreen):
            self.pop_screen()
            return
        self.push_screen(AtlasDrawerScreen(self._atlas_drawer_content()))

    def _render_atlas_rail(self) -> None:
        """The always-present rail summary: authority first, then state."""
        atlas = _record(self.snapshot.get("atlas"))
        rail = self.query_one("#atlas-rail", Static)
        if not atlas:
            rail.update(f"[{MUTED}]desk manager unavailable[/]")
            return
        mode = str(atlas.get("mode", "—"))
        state = str(atlas.get("state", "—"))
        state_tone = _ATLAS_STATE_TONES.get(state, TEXT_HI)
        approvals = _records(self.snapshot.get("approvals"))
        tasks = _records(self.snapshot.get("atlas_tasks"))
        active = [t for t in tasks if t.get("status") in ("queued", "running")]
        lines = _key_number_markup(
            [
                ("MODE", mode.upper()),
                ("STATE", state.upper()),
                ("APPROVALS", str(len(approvals))),
                ("OPEN TASKS", str(len(active))),
            ],
            value_tones=[
                AMBER if mode == "propose" else TEXT_HI,
                state_tone,
                AMBER if approvals else MUTED,
                TEXT_HI if active else MUTED,
            ],
            bold_values={0, 1},
        )
        reason = str(atlas.get("blocked_reason") or "").strip()
        if reason:
            lines.extend(_bulletin_markup([reason], tone=DOWN, max_len=60))
        elif not atlas.get("coordinator_available"):
            lines.extend(_bulletin_markup(
                ["coordinator unavailable"], tone=MUTED, max_len=60))
        lines.append(f"[{DIM}]ctrl+b for detail[/]")
        rail.update("\n".join(lines))

    def _atlas_drawer_content(self) -> str:
        """Full Atlas detail: authority, state, approvals, and task history."""
        # Change 5: section divider rule shared across all sections in the drawer
        atlas = _record(self.snapshot.get("atlas"))
        if not atlas:
            return f"[{MUTED}]desk manager unavailable[/]"
        mode = str(atlas.get("mode", "—"))
        rule = f"[{BORDER_HI}]{'─' * 52}[/]"

        lines = [self._atlas_panel_content(), rule, ""]

        # Authority section — what this mode is allowed to do
        lines.append(f"[bold {AMBER}]▌ AUTHORITY[/]")
        lines.append(rule)
        lines.extend(_bulletin_markup(
            [_ATLAS_MODE_AUTHORITY.get(mode, "unknown mode"),
             "Atlas never executes: paper execution consumes a persisted human "
             "approval bound to the exact plan."],
            tone=MUTED, max_len=110))

        # Task history section — structured columns with status icon prefix
        tasks = _records(self.snapshot.get("atlas_tasks"))
        lines.append("")
        lines.append(f"[bold {AMBER}]▌ RECENT TASKS[/]")
        lines.append(rule)
        if not tasks:
            lines.extend(_bulletin_markup(
                ["no autonomous tasks recorded"], tone=MUTED, max_len=100))
        for task in tasks[:8]:
            status = str(task.get("status", "—"))
            task_tone, task_glyph = {
                "completed": (UP, "✓"),
                "failed":    (DOWN, "×"),
                "blocked":   (DOWN, "!"),
                "running":   (AMBER, "●"),
            }.get(status, (MUTED, "◌"))
            created = str(task.get("created_at", ""))[5:16].replace("T", " ")
            trigger = str(task.get("trigger_kind", "—"))
            lines.append(
                f"  [{task_tone}]{task_glyph} {status:<10}[/]"
                f"  [{TEXT_HI}]{trigger:<20}[/]"
                f"  [{DIM}]{created}[/]")
            error = str(task.get("error") or "").strip()
            if error:
                lines.extend(_bulletin_markup([error], tone=DOWN, max_len=100))
        return "\n".join(lines)

    def _atlas_panel_content(self) -> str:
        """Atlas's mode, lifecycle state, pending approvals, and recent tasks.

        Mode is the authority statement (observe never launches work; only
        propose can put a plan up for approval), so it is shown first and
        never abbreviated away.
        """
        atlas = _record(self.snapshot.get("atlas"))
        if not atlas:
            return f"[{MUTED}]desk manager unavailable[/]"
        mode = str(atlas.get("mode", "—"))
        state = str(atlas.get("state", "—"))
        state_tone = _ATLAS_STATE_TONES.get(state, TEXT_HI)
        approvals = _records(self.snapshot.get("approvals"))
        tasks = _records(self.snapshot.get("atlas_tasks"))
        active = [t for t in tasks if t.get("status") in ("queued", "running")]
        pairs = [
            ("MODE", mode.upper()),
            ("STATE", state.upper()),
            ("PENDING APPROVALS", str(len(approvals))),
            ("OPEN TASKS", str(len(active))),
        ]
        tones = [
            AMBER if mode == "propose" else TEXT_HI,
            state_tone,
            AMBER if approvals else MUTED,
            TEXT_HI if active else MUTED,
        ]
        rule = f"[{BORDER_HI}]{'─' * 52}[/]"
        lines = [f"[bold {AMBER}]▌ ATLAS · DESK MANAGER[/]", rule]
        lines.extend(_key_number_markup(
            pairs, value_tones=tones, bold_values={0, 1}))
        reason = str(atlas.get("blocked_reason") or "").strip()
        if reason:
            lines.extend(_bulletin_markup([reason], tone=DOWN, max_len=90))
        elif not atlas.get("coordinator_available"):
            lines.extend(_bulletin_markup(
                ["coordinator unavailable — monitoring continues"],
                tone=MUTED, max_len=90))

        # Pending approvals are the human's decision queue: each one names the
        # exact plan it binds and when it expires. Approving is a deliberate
        # act through the owner API, never a side effect of viewing.
        if approvals:
            lines.append("")
            # Change 5: approval cards with amber ID badges and expiry chips
            lines.append(f"[bold {AMBER}]▌ PENDING APPROVALS[/]")
            lines.append(rule)
            for approval in approvals[:5]:
                plan_id = str(approval.get("plan_id", ""))[:14]
                expires = str(approval.get("expires_at", ""))[11:19]
                approval_id = str(approval.get('approval_id', ''))[:10]
                lines.append(
                    f"  [{AMBER}]❯ {approval_id}[/]"
                    f"  [{TEXT_HI}]plan {plan_id}[/]"
                    f"  [{DIM}]expires {expires}[/]")
            lines.extend(_bulletin_markup(
                ["approve or reject through the owner approvals API; "
                 "execution consumes the approval"],
                tone=MUTED, max_len=90))
        return "\n".join(lines)

    def _plot_region(self, widget_id: str) -> tuple[int, int]:
        """Cells available to a chart in ``widget_id``, or ``(0, 0)`` if unknown.

        A view inside the canvas switcher has no size until it is shown, so the
        first paint after switching to it used to fall back to a width guessed
        from the terminal. The guess does not match the real column, so the
        chart was drawn at the wrong dimensions and then snapped to the right
        ones on the next repaint — which is exactly the "distorted graph, then
        the actual chart" an operator sees on first open.

        Returning zero lets the caller wait for layout instead of inventing a
        size. A chart drawn to the wrong scale is worse than one drawn a frame
        later.
        """
        try:
            region = self.query_one(widget_id).size
        except Exception:
            return 0, 0
        return max(0, int(region.width)), max(0, int(region.height))

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

        # ── Change 1: chart column gets its own size probe ─────────────────
        # The stats sidebar is ~28 cols wide; subtract that + the gutter from
        # what the chart column can use. The height is now the full canvas
        # height minus the header row only (no stats rows below the chart).
        avail_w, avail_h = self._plot_region("#market-chart")
        if avail_w <= 0 or avail_h <= 0:
            # Layout has not sized the chart column yet. Repaint once it has,
            # rather than drawing to a guessed scale and correcting visibly.
            self.call_after_refresh(self._render_market)
            return
        hi = max(history) if history else 0.0
        lo = min(history) if history else 0.0
        mid = (hi + lo) / 2.0

        # Y-axis gutter: right-aligned price labels.
        gutter = max(len(money(hi)), len(money(mid)), len(money(lo))) if history else 0
        chart_w = max(24, avail_w - gutter - 2)
        # Height is bounded by the chart's own aspect, not just by the space
        # available. A braille cell is 2 dots wide and 4 tall, so a plot given
        # the full canvas height stretches each move into a near-vertical
        # stroke and the line reads as scattered dots — the same series at a
        # third of that height reads as a price curve. Roughly 3:1 width to
        # height is the shape a trend is legible at.
        # The chart takes the height it is given: this is the view's subject,
        # and a plot occupying a third of its own pane looks like a rendering
        # fault. Six lines go to the header, the x-axis baseline, the span and
        # the legend.
        chart_h = max(8, avail_h - 6)
        # Filled, because a one-dot line spread over that much height reads as
        # scatter even though it is continuous — the shape is what carries a
        # price series.
        rows = braille_chart(history, chart_w, chart_h, fill=True)
        last_row = len(rows) - 1
        mid_row = last_row // 2
        as_of = str(market.get("as_of", "—"))

        # ── Assemble chart lines ────────────────────────────────────────────
        # Header: ticker + price + change + hi/lo range
        lines = [
            f"[bold {TEXT_HI}]{escape(self.active_ticker)}[/]  "
            f"[bold {TEXT_HI}]{money(row.get('price'))}[/]  "
            f"[{dir_col}]{'▲' if up else '▼'} {pct(row.get('change_1d'))} today[/]"
            f"  [{LABEL_GOLD}]H[/] [{TEXT}]{money(hi)}[/]"
            f"  [{LABEL_GOLD}]L[/] [{TEXT}]{money(lo)}[/]"
            f"  [{DIM}]{len(history)}d[/]",
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
                f"[{dir_col}]{escape(bar)}[/]"
            )
        # X axis
        pad = " " * (gutter + 2)
        lines.append(f"[{CHART_AXIS}]{' ' * gutter} └{'─' * chart_w}[/]")
        left_lbl = f"{len(history)}d ago"
        right_lbl = f"as of {as_of}"
        gap = chart_w - len(left_lbl) - len(right_lbl)
        span = (left_lbl + " " * gap + right_lbl if gap >= 1
                else f"{len(history)}d → {as_of}"[:chart_w])
        lines.append(f"[{LABEL_GOLD}]{pad}{escape(span)}[/]")
        lines.append(
            f"[{DIM}]{pad}daily adjusted-close · not a streaming quote[/]")

        self.query_one("#market-chart", Static).update("\n".join(lines))

        # ── Change 3: sidebar — sparkline header + grouped stat cards ──────
        # Header card: large sparkline of full history for the selected ticker
        spark_w = 20  # cells available in the 28-wide sidebar
        spark_lines = braille_chart(history, spark_w, 3) if len(history) >= 2 else []
        header_lines: list[str] = []
        for spark_row in spark_lines:
            header_lines.append(f"[{dir_col}]{escape(spark_row)}[/]")
        if not header_lines:
            header_lines.append(f"[{MUTED}]no history[/]")
        self.query_one("#market-stats-header", Static).update(
            "\n".join(header_lines)
        )

        # Stat cards: three groups — PRICE, POSITION, CONTEXT
        change_20d = _finite_number(row.get("change_20d"))
        change_20d_tone = (MUTED if change_20d is None
                           else UP if change_20d >= 0 else DOWN)
        realized_vol = _finite_number(row.get("realized_vol"))
        regime_name = str(
            market.get("regime", {}).get("regime", "—")
        ).upper()
        regime_tone = _HMM_STATE_TONE.get(regime_name.lower(), TEXT_HI)

        def _stat(label: str, value: str, tone: str = TEXT_HI) -> str:
            lbl = escape(label[:12])
            val = escape(str(value)[:10])
            return f"[{MUTED}]{lbl:<12}[/] [{tone}]{val}[/]"

        rule = f"[{BORDER_HI}]{'─' * 22}[/]"
        body_lines = [
            f"[bold {AMBER}]▌ PRICE[/]",
            rule,
            _stat("20d change", pct(change_20d), change_20d_tone),
            _stat("63d vol", pct(realized_vol)),
            "",
            f"[bold {AMBER}]▌ POSITION[/]",
            rule,
            _stat("weight", pct(float(current))),
            _stat("target", pct(target) if target is not None else "—"),
            "",
            f"[bold {AMBER}]▌ CONTEXT[/]",
            rule,
            _stat("regime", regime_name, regime_tone),
            _stat("source", str(market.get("source", "—")).upper()),
            _stat("as of", str(market.get("as_of", "—"))[:10]),
            _stat("bar age", f"{market.get('bar_age_days', '—')}d"),
        ]
        self.query_one("#market-stats-body", Static).update(
            "\n".join(body_lines)
        )

    def _set_flow_spec(
        self,
        flow: tuple[tuple[str, str, str], ...],
    ) -> None:
        """Switch the board to the selected workflow's actual step instances."""
        self._flow_spec = flow or _FLOW
        try:
            self.query_one("#flow-section", FlowBoard).set_flow(self._flow_spec)
        except Exception:
            pass

    def _paint_flow_node(self, node: FlowNode) -> None:
        state = self._flow_states.get(node.phase, "idle")
        node.state = state
        node.pulse = self._pulse
        # A workflow respec recomposes the board, and a node built after a theme
        # switch would otherwise mount on the default palette -- `action_theme`
        # only reaches the nodes that existed when it ran. Pushing it here makes
        # mount-time correct, because on_mount paints through this method.
        node.theme_name = self._active_theme()
        node.detail = self._flow_details.get(node.phase) or (
            f"{node.agent}\n\nnot yet started")

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
                    self._launched_workflow_id = workflow_id
                    self._pending_workflow = False
                    return row
            return None
        return workflows[0] if workflows else None

    def _render_workforce(self) -> None:
        workflows = self.snapshot.get("workflows", []) if self.snapshot else []
        resumable_id = self._latest_resumable_workflow_id()
        abandonable_id = self._latest_abandonable_workflow_id()
        resume_button = self.query_one("#btn-workforce-resume", Button)
        abandon_button = self.query_one("#btn-workforce-abandon", Button)
        resume_button.disabled = not bool(resumable_id)
        abandon_button.disabled = not bool(abandonable_id)
        resume_button.tooltip = (
            f"Resume {resumable_id}"
            if resumable_id else
            "No incomplete workforce review to resume"
        )
        abandon_button.tooltip = (
            f"Permanently close {abandonable_id}; its audit record is retained"
            if abandonable_id else
            "No incomplete workforce review to abandon"
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

        # Change 3: set a CSS class on #workforce-content that reflects the run
        # state so the accent border colour updates without touching text content.
        content_widget = self.query_one("#workforce-content", Static)
        for cls in ("running", "complete", "failed", "blocked", "interrupted"):
            content_widget.remove_class(f"-{cls}")
        _status_cls = {
            "running": "-running", "complete": "-complete",
            "failed": "-failed", "blocked": "-blocked",
            "interrupted": "-interrupted",
        }.get(status)
        if _status_cls:
            content_widget.add_class(_status_cls)

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

        # Change 3: structured two-line header: ID + status chip / kind · date · universe
        status_tone = {
            "running": CYAN, "complete": UP, "failed": DOWN,
            "blocked": AMBER, "interrupted": GOLD,
        }.get(status, MUTED)
        status_glyph = {
            "running": "●", "complete": "✓", "failed": "×",
            "blocked": "!", "interrupted": "‖",
        }.get(status, "◌")
        lines = [
            f"[bold {TEXT_HI}]{escape(str(workflow.get('workflow_id', '—')))}[/]"
            f"   [{status_tone}]{status_glyph} {escape(status.upper())}[/]",
            f"[{AMBER}]{escape(str(workflow.get('kind', 'portfolio_review')))}[/]"
            f"[{MUTED}] · as of {escape(str(request.get('as_of', '—')))} · "
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
        elif status in ("failed", "blocked", "interrupted", "abandoned"):
            tone = {
                "failed": DOWN,
                "blocked": AMBER,
                "interrupted": GOLD,
                "abandoned": MUTED,
            }[status]
            broken = next(
                (s for s in steps if s.get("status") == status),
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
            workflow_id = escape(str(workflow.get("workflow_id", "")))
            if status != "abandoned":
                lines.append(
                    f"[{LABEL_GOLD}]Resume with : workforce resume "
                    f"{workflow_id} · close permanently with "
                    f": workforce abandon {workflow_id}[/]"
                )
            else:
                lines.append(
                    f"[{MUTED}]This run is closed. Its completed evidence and "
                    "audit trail remain available; start a new workforce run "
                    "to continue research.[/]"
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
            if str(workflow.get("status", "")).lower() not in {
                "interrupted", "failed", "blocked",
            }:
                continue
            workflow_id = str(workflow.get("workflow_id", "")).strip()
            if workflow_id:
                return workflow_id
        return ""

    def _latest_abandonable_workflow_id(self) -> str:
        workflows = self.snapshot.get("workflows", []) if self.snapshot else []
        for workflow in workflows:
            if str(workflow.get("status", "")).lower() in {
                "complete", "abandoned",
            }:
                continue
            workflow_id = str(workflow.get("workflow_id", "")).strip()
            if workflow_id:
                return workflow_id
        return ""

    def _workflow_row(self, workflow_id: str) -> dict | None:
        workflows = self.snapshot.get("workflows", []) if self.snapshot else []
        return next((
            row for row in workflows
            if str(row.get("workflow_id", "")) == workflow_id
        ), None)

    def _merge_workflow(self, workflow: dict) -> None:
        """Merge an owner control response into the view before the next poll."""
        workflow_id = str(workflow.get("workflow_id") or "")
        if not workflow_id:
            return
        workflows = list(
            self.snapshot.get("workflows", []) if self.snapshot else [])
        for index, row in enumerate(workflows):
            if str(row.get("workflow_id", "")) == workflow_id:
                workflows[index] = workflow
                break
        else:
            workflows.insert(0, workflow)
        self.snapshot["workflows"] = workflows
        self._active_workflow_id = workflow_id
        self._pending_workflow = False

    def _control_workflow(
        self,
        workflow_id: str,
        action: str,
        reason: str,
        *,
        quiet: bool = False,
    ) -> dict | None:
        """Fence a workflow through the owner; never mutate DuckDB in the TUI."""
        if not workflow_id:
            return None
        try:
            post_control = getattr(
                self.client, "post_control", self.client.post)
            workflow = post_control(
                f"/api/workflows/{workflow_id}/{action}",
                {"reason": reason},
            )
        except Exception as exc:
            if not quiet:
                detail = str(exc) or repr(exc)
                self._console_write(
                    f"[{DOWN}]workflow {escape(action)} failed: "
                    f"{escape(detail[-240:])}[/]")
                self._set_selected_work(
                    f"WORKFLOW {action.upper()} FAILED\n\n{detail[-2000:]}"
                )
            return None
        self._merge_workflow(workflow)
        self._write_local_event(
            f"claude.workflow_{action}",
            {"workflow_id": workflow_id, "reason": reason},
        )
        self._render_workforce()
        self._start_refresh()
        return workflow

    def _apply_pending_workflow_control(self, workflow_id: str) -> None:
        pending = self._pending_workflow_control
        if pending is None or not workflow_id:
            return
        self._pending_workflow_control = None
        action, reason = pending
        self._control_workflow(workflow_id, action, reason)

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
        empty = self.query_one("#book-plans-empty", Static)
        empty.styles.display = "none" if plans else "block"
        if not plans:
            empty.update(
                f"[{MUTED}]No plans yet — a dry rebalance proposes one.[/]")
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

    def _render_desk_settings(self) -> None:
        """The desk-mode card: which desk this is, and how to change it.

        Credential state comes from the owner's own snapshot, which resolves
        env-or-profile locally and never calls Alpaca — so opening Settings
        cannot hang on a network round trip.
        """
        mode = (self.snapshot.get("desk_mode") or {}) if self.snapshot else {}
        label = str(mode.get("label")
                    or (self.desk_mode.label if self.desk_mode else "—"))
        creds_ok = bool(mode.get("credentials_ok"))
        creds = str(mode.get("credentials") or "").strip()

        # The owner's view wins, but fall back to the mode this client committed
        # to: a snapshot that predates the choice must not blank out fields the
        # app already knows.
        data = str(mode.get("data")
                   or (self.desk_mode.data if self.desk_mode else "—"))
        book = str(mode.get("book")
                   or (self.desk_mode.book if self.desk_mode else "—"))
        lines = [f"[{LABEL_GOLD}]DESK MODE · WHOSE BOOK THIS IS[/]"]
        lines.extend(_key_number_markup([
            ("current desk", label),
            ("data", data.upper()),
            ("book", book.upper()),
        ]))
        lines.append("")
        # The owner's own description already names the remedy when there is
        # none, so it is shown as-is rather than restated underneath it.
        if creds_ok:
            lines.append(f"[{UP}]✓ signed in[/]  [{DIM}]{escape(creds)}[/]")
            lines.append(
                f"[{DIM}]The Alpaca book is selectable. Paper only — this "
                f"adapter has no live path.[/]")
        else:
            # Not an error: most sessions are synthetic on purpose. It is a
            # precondition for one choice.
            lines.append(f"[{AMBER}]! not signed in[/]  [{DIM}]"
                         f"{escape(creds or 'no credential found')}[/]")
            lines.append(
                f"[{DIM}]Then press re-check — nothing here calls Alpaca.[/]")
        self.query_one("#settings-desk-copy", Static).update("\n".join(lines))
        # Reaching a real book without a credential is not a choice we offer.
        self.query_one("#settings-change-desk", Button).tooltip = (
            "Choose the data lane and which book is traded")

    def _render_workforce_settings(self) -> None:
        """How the workforce runs: autonomy, speed, and who drives.

        Each line states its trade-off rather than just its state. "FAST ON" is
        not actionable on its own — what matters is which roles it cheapens and
        which it deliberately does not.
        """
        beat = _record(self.snapshot.get("atlas_heartbeat")) if self.snapshot else {}
        coordinator = _record(beat.get("coordinator"))
        atlas = _record(self.snapshot.get("atlas")) if self.snapshot else {}
        mode_now = str(atlas.get("mode") or "—").upper()
        fast = bool(beat.get("fast"))
        autonomous = bool(beat.get("autonomous"))

        lines = [f"[{LABEL_GOLD}]WORKFORCE · HOW THE DESK RUNS[/]"]
        lines.extend(_key_number_markup(
            [
                ("atlas mode", mode_now),
                ("autonomy", "ON" if autonomous else "OFF"),
                ("model tier", "FAST" if fast else "FULL"),
                ("driving", "YES" if coordinator.get("driving") else "NO"),
            ],
            value_tones=[
                TEXT_HI,
                UP if autonomous else MUTED,
                AMBER if fast else TEXT_HI,
                UP if coordinator.get("driving") else MUTED,
            ],
        ))
        lines.append("")
        if fast:
            lines.append(
                f"[{AMBER}]Fast mode is on[/]  [{DIM}]judgment roles run on the "
                f"quick model. The referee keeps its tier — a PASS must never "
                f"mean 'passed on the fast model'.[/]")
        else:
            lines.append(
                f"[{DIM}]Every role runs on its configured tier. Fast mode "
                f"trades depth for latency on judgment roles only.[/]")
        # Whether Atlas can run work by itself is the difference between a desk
        # that researches overnight and one that queues and waits.
        if coordinator.get("driving"):
            lines.append(
                f"[{UP}]Driving workflow {escape(str(coordinator.get('workflow_id') or '—'))}[/]"
                f"  [{DIM}]its reasoning streams into the console.[/]")
        elif coordinator.get("can_drive"):
            lines.append(
                f"[{DIM}]Idle. Atlas will start a coordinator itself when a "
                f"trigger fires and its mode permits the template.[/]")
        else:
            lines.append(
                f"[{AMBER}]! cannot drive[/]  [{DIM}]"
                f"{escape(str(coordinator.get('reason') or 'unavailable'))} — "
                f"dispatched work waits for `: workforce`.[/]")
        self.query_one("#settings-workforce-copy", Static).update("\n".join(lines))
        self.query_one("#settings-toggle-fast", Button).tooltip = (
            "Turn fast mode off — every role back on its tier" if fast
            else "Run judgment roles on the quick model (the referee is exempt)")

    def _render_settings(self) -> None:
        self._render_desk_settings()
        self._render_workforce_settings()
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

        # "PAPER" leads the card because the desk is a paper desk before it is
        # anything else; the tokens below it are the ones the command row used
        # to carry, now read here instead of glanced at.
        system_lines = [f"PAPER [{LABEL_GOLD}]DESK · SYSTEM & SERVICES[/]"]
        system_lines.extend(_key_number_markup(self._system_tokens()))
        self.query_one("#settings-system", Static).update(
            "\n".join(system_lines))

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

    def _render_news(self) -> None:
        """The news window, with its coverage stated rather than implied.

        A thin window has two completely different causes — the market was
        quiet, or the wire simply is not writing about these tickers — and a
        list of headlines alone cannot tell them apart. This desk holds
        cross-asset ETFs that get almost no symbol-tagged coverage, so the
        coverage line is not a diagnostic detail; it is the main thing a reader
        needs in order to know what the absence of news means.
        """
        news = _record(self.snapshot.get("news")) if self.snapshot else {}
        counts = _record(news.get("counts"))
        provider = str(news.get("provider") or "—")
        error = str(news.get("error") or "")

        head = [f"[{LABEL_GOLD}]THE WINDOW[/]"]
        pending = bool(error) and "not fetched yet" in error.lower()
        if pending:
            # A window the desk has not asked for yet is not a broken feed.
            # Painting it red taught the operator to distrust a working feed.
            head.append("")
            head.extend(_bulletin_markup(
                ["Fetching the first window — the owner heartbeat populates it "
                 "within one tick.",
                 "Press r to fetch now."],
                tone=MUTED, max_len=200))
            self.query_one("#news-summary", Static).update("\n".join(head))
            self.query_one("#news-stories", Static).update("")
            return
        if error:
            head.extend(_bulletin_markup(
                [f"NEWS FEED UNAVAILABLE — {error}",
                 "The qualitative side of the read is missing, not quiet."],
                tone=DOWN, max_len=240))
            self.query_one("#news-summary", Static).update("\n".join(head))
            self.query_one("#news-stories", Static).update("")
            return

        total = int(counts.get("total") or 0)
        head.extend(_key_number_markup(
            [
                ("stories", str(total)),
                ("about holdings", str(int(counts.get("holding") or 0))),
                ("macro context", str(int(counts.get("macro") or 0))),
                ("provider", provider.upper()),
            ],
            value_tones=[
                TEXT_HI,
                UP if int(counts.get("holding") or 0) else MUTED,
                TEXT_HI,
                AMBER if provider == "synthetic" else UP,
            ],
        ))
        if provider == "synthetic":
            head.append("")
            head.append(
                f"[{AMBER}]synthetic (demo)[/]  [{DIM}]deterministic fixtures, "
                f"not a news record. Sign in and run --live for the real "
                f"window; see docs/news-setup.md.[/]")

        coverage = [c for c in (news.get("coverage") or []) if isinstance(c, dict)]
        if coverage:
            head.append("")
            covered = [c for c in coverage if int(c.get("stories") or 0) > 0]
            head.append(f"[{LABEL_GOLD}]COVERAGE[/]  [{DIM}]"
                        f"{len(covered)}/{len(coverage)} holdings named[/]")
            head.append("  " + "   ".join(
                f"[{UP if int(c.get('stories') or 0) else MUTED}]"
                f"{escape(str(c.get('ticker')))} {int(c.get('stories') or 0)}[/]"
                for c in coverage))
            uncovered = [str(t) for t in (news.get("uncovered") or [])]
            if uncovered:
                head.append(
                    f"[{DIM}]No story named {escape(', '.join(uncovered))} in this "
                    f"window. For broad cross-asset ETFs that is normal — the "
                    f"macro items above are the relevant record, and silence "
                    f"here is not evidence of calm.[/]")
        self.query_one("#news-summary", Static).update("\n".join(head))

        if not (news.get("items") or []):
            self.query_one("#news-stories", Static).update(
                f"[{MUTED}]No stories in the last "
                f"{int(news.get('lookback_hours') or 48)} hours.[/]")
            return

        lines = [f"[{BORDER_HI}]{'─' * 60}[/]", ""]
        for item in (news.get("items") or [])[:40]:
            row = _record(item)
            when = str(row.get("published") or "")[:16].replace("T", " ")
            tickers = [str(t) for t in (row.get("tickers") or [])]
            scope = (f"[{UP}]{escape(','.join(tickers))}[/]" if tickers
                     else f"[{MUTED}]macro[/]")
            lines.append(
                f"[{DIM}]{escape(when)}[/]  {scope}  "
                f"[{TEXT_HI}]{escape(str(row.get('headline') or ''))}[/]")
            source = str(row.get("source") or "")
            if source:
                lines.append(f"        [{DIM}]{escape(source)}[/]")
            lines.append("")
        self.query_one("#news-stories", Static).update("\n".join(lines))

    def _render_audit(self) -> None:
        decisions = self.snapshot.get("decisions", [])
        self.query_one("#audit-summary", Static).update(
            "Every judgment, challenge, verdict, and reflection remains inspectable.\n\n"
            f"{len(decisions)} decisions   ·   plans and orders are in Book\n\n"
            f"{self._atlas_panel_content()}"
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
        # The timeline keeps the trail of what was inspected and what it said,
        # so a selection made minutes ago is still recoverable from `~`.
        self.query_one("#timeline", RichLog).write(
            f"{str(key)[:10]}  verdict {verdict_text} · "
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
        rows = []
        for agent in agents:
            name = str(agent["name"])
            state = self._agent_states.get(name, agent.get("state", "idle"))
            rows.append((
                name,
                str(agent.get("authority", "—")),
                str(state),
            ))
        rail = self.query_one("#agent-list", AgentRail)
        rail.rows = tuple(rows)
        rail.pulse = self._pulse

    def _render_mode_chip(self) -> None:
        mode = (self.snapshot.get("desk_mode") or {}) if self.snapshot else {}
        fallback = self.desk_mode
        # Label and tone are read from one source. A snapshot taken before the
        # owner knew the desk mode would otherwise paint a real book in the
        # demo's tone, which is the single misread this chip exists to prevent.
        label = str(mode.get("label")
                    or (fallback.label if fallback else "")).strip()
        chip = self.query_one("#mode-chip", Static)
        if self._desk_mode_error is not None:
            # The owner rejected the mode this client already committed to, so
            # neither label is true: naming either desk would be a claim about
            # whose money is at risk that nothing currently supports.
            chip.set_class(True, "live-book")
            chip.set_class(False, "live-data")
            chip.update("MODE NOT APPLIED")
        elif not label:
            # Nobody has said which desk this is yet — the chooser is still up.
            # "SYNTHETIC" here would be a positive and possibly wrong answer to
            # "whose money is this", so the chip claims nothing instead.
            chip.set_class(False, "live-book", "live-data")
            chip.update("CONNECTING")
        else:
            # Normalised because this comparison decides whether a real book
            # reads as the demo; casing drift must not downgrade it silently.
            data = str(mode.get("data")
                       or (fallback.data if fallback else "")).strip().lower()
            book = str(mode.get("book")
                       or (fallback.book if fallback else "")).strip().lower()
            # Alert tone for a real book, warning for live prices on a
            # simulated one, muted for synthetic; the tones live in the theme.
            chip.set_class(book == "alpaca", "live-book")
            chip.set_class(book != "alpaca" and data == "live", "live-data")
            chip.update(label)
        self.query_one("#chat-exit", Button).label = (
            "■ stop" if self.claude.running else "exit")
        self._sync_chat_input()

    def _system_tokens(self) -> list[tuple[str, str]]:
        """The service facts the bottom banner used to concatenate.

        Kept as the banner's own token strings — ``DATA synthetic·0d``,
        ``AUTO 07-24 16:30·2`` — because they are what the operator learned to
        read; only their home moved from the command row into Settings.
        """
        system = (self.snapshot.get("system") or {}) if self.snapshot else {}
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
        # Feed identity is never collapsed into the word "live": IEX is not SIP
        # coverage, and the operator must always see which one is priced.
        quotes = (self.snapshot.get("quotes") or {}) if self.snapshot else {}
        if quotes.get("live_stream"):
            feed = str(quotes.get("feed", "")).replace("_", " ").upper()
            health = quotes.get("health") or {}
            feed_token = (
                f"ALPACA·{feed}" if health.get("fresh") else f"ALPACA·{feed} STALE")
        else:
            feed_token = "FEED —"
        atlas = (self.snapshot.get("atlas") or {}) if self.snapshot else {}
        atlas_token = (
            f"ATLAS {str(atlas.get('mode', '—')).upper()}/"
            f"{str(atlas.get('state', '—')).upper()}" if atlas else "ATLAS —")
        approvals = (self.snapshot.get("approvals") or []) if self.snapshot else []
        return [
            ("quote feed", feed_token),
            ("mcp proxy", mcp),
            ("coordinator", claude),
            ("provenance", data_token),
            ("autopilot", autopilot_token),
            ("desk manager", atlas_token),
            ("approvals waiting", str(len(approvals))),
        ]

    # -- events -----------------------------------------------------------
    def _ingest_events(self, events_: list[dict]) -> None:
        for event in events_:
            event_id = str(event.get("event_id", ""))
            if event_id and event_id in self._event_ids:
                continue
            if event_id:
                self._event_ids.add(event_id)
                self._event_id_order.append(event_id)
                if len(self._event_id_order) > _EVENT_ID_LIMIT:
                    self._event_ids.discard(self._event_id_order.popleft())
            self._append_event(event)
            kind = str(event.get("kind", ""))
            if kind == "workflow_started":
                self._note_workflow_started(event.get("payload") or {})
            elif kind == "workflow_phase":
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
        if self.snapshot:
            self._render_active_snapshot_view()
        if view == "workforce":
            # Chat-first focus claims digits as text; F1-F8 still switch views,
            # and Escape blurs the input so digit navigation works again.
            field = self.query_one("#chat-input", Input)
            if not field.disabled:  # a running turn owns the box; don't grab it
                field.focus()
        elif view == "reference":
            # Master-detail only reads if the index is navigable on arrival; the
            # ListView claims no digit keys, so view switching keeps working.
            self.query_one("#reference-list", ListView).focus()
            self._start_reference_fetch()
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
        reason = "operator stopped the coordinator before completion"
        workflow_id = self._launched_workflow_id
        if not workflow_id and not self._pending_workflow:
            workflows = self.snapshot.get("workflows", []) if self.snapshot else []
            workflow_id = next((
                str(row.get("workflow_id", ""))
                for row in workflows
                if str(row.get("status", "")).lower() == "running"
            ), "")
        if self.claude.mode == "workforce":
            # Stop the local process tree first. The following owner call is
            # short-bounded, but process cleanup must not depend on owner health.
            self.claude.stop(reason)
        if workflow_id:
            self._control_workflow(workflow_id, "interrupt", reason)
        elif self._pending_workflow:
            # workflow.start may already be in flight. The process tree is
            # stopped now; the first durable id is fenced when its event lands.
            self._pending_workflow_control = ("interrupt", reason)
        if workflow_id or self._pending_workflow_control:
            self._set_selected_work(
                "CLAUDE WORKFORCE INTERRUPTED\n\nThe full coordinator/agent "
                "process tree was stopped. Completed evidence is retained and "
                "the active phase is frozen; use : workforce resume ID to "
                "continue or : workforce abandon ID to close it permanently."
            )
            self._console_write(
                f"[{GOLD}]Ⅱ interrupted — child agents were stopped; durable "
                "phase state is resumable[/]")
        else:
            self._set_selected_work(
                "NO ACTIVE WORKFORCE\n\nThere is no running or incomplete "
                "coordinator owned by this desk to stop."
            )
        self._write_local_event(
            "claude.workforce_stopped", {"workflow_id": workflow_id})
        self._render_mode_chip()

    def action_workforce_abandon(self, workflow_id: str = "") -> None:
        """Close an incomplete run without deleting its evidence or audit."""
        self._chat_mode = "workforce"
        self._render_chat_mode()
        explicit_id = bool(workflow_id.strip())
        workflow_id = workflow_id.strip() or self._latest_abandonable_workflow_id()
        reason = "operator permanently closed the incomplete workflow"
        if not workflow_id and self._pending_workflow:
            self._pending_workflow_control = ("abandon", reason)
        elif not workflow_id:
            self._set_selected_work(
                "NO REVIEW TO ABANDON\n\nThere is no incomplete workflow. "
                "Completed and already-abandoned records are retained."
            )
            return
        else:
            row = self._workflow_row(workflow_id)
            if row is None and not explicit_id:
                self._set_selected_work(
                    f"UNKNOWN WORKFLOW\n\nNo recent workflow has id {workflow_id}."
                )
                return
        if self.claude.mode == "workforce" and self.claude.running:
            owned = self._launched_workflow_id
            if not owned or not workflow_id or owned == workflow_id:
                self.claude.stop(reason)
        if workflow_id and self._control_workflow(
                workflow_id, "abandon", reason) is None:
            return
        self._set_selected_work(
            "WORKFLOW ABANDONED\n\nThe run is permanently closed, and every "
            "unfinished phase is non-running. Completed results and the full "
            "audit trail were retained; no market or execution records were "
            "deleted."
        )
        self._console_write(
            f"[{MUTED}]× abandoned — audit retained; start a new run to "
            "continue[/]")
        self._render_mode_chip()

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

    def action_atlas_refresh(self) -> None:
        """Recompose Atlas's read now instead of waiting for the next heartbeat."""
        self._run_api_action(
            "atlas read",
            "/api/atlas/read",
            {"offline": self.offline, "refresh": True},
            active_agent=None,
            http_method="get",
        )

    def action_atlas_observe(self) -> None:
        """Force one supervisor tick — evaluate triggers against current facts."""
        self._run_api_action(
            "atlas observe", "/api/atlas/observe", {"offline": self.offline},
            active_agent=None)

    def action_atlas_cycle_mode(self) -> None:
        """Step Atlas through its authority modes from the desk."""
        order = ("observe", "research", "propose", "paused")
        current = str(_record(self.snapshot.get("atlas")).get("mode", "observe"))
        nxt = order[(order.index(current) + 1) % len(order)] if current in order \
            else "observe"
        self._run_api_action(
            f"atlas mode {nxt}", "/api/atlas/mode",
            {"mode": nxt, "offline": self.offline}, active_agent=None)

    def action_atlas_toggle_autonomy(self) -> None:
        """Turn autonomous work on or off. Never widens what a mode permits."""
        beat = _record(self.snapshot.get("atlas_heartbeat"))
        enabled = not bool(beat.get("autonomous"))
        self._run_api_action(
            f"atlas autonomy {'on' if enabled else 'off'}",
            "/api/atlas/autonomy",
            {"enabled": enabled, "offline": self.offline}, active_agent=None)

    def action_toggle_fast_mode(self) -> None:
        """Trade depth for latency on judgment roles. The referee is exempt."""
        beat = _record(self.snapshot.get("atlas_heartbeat"))
        enabled = not bool(beat.get("fast"))
        self._run_api_action(
            f"fast mode {'on' if enabled else 'off'}",
            "/api/workforce/fast",
            {"enabled": enabled, "offline": self.offline}, active_agent=None)

    def action_atlas_escalate(self) -> None:
        """Ask Atlas to open a bounded debate on the current disagreement."""
        self._run_api_action(
            "atlas escalate", "/api/atlas/escalate", {"offline": self.offline},
            active_agent=None)

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
            "view dashboard|desk|market|workforce|research|book|audit|reference|settings\n"
            "view agents\n"
            "symbol TICKER\n"
            "chat MESSAGE      (read-only desk assistant)\n"
            "workforce GOAL    (governed five-role pipeline)\n"
            "workforce status\n"
            "workforce resume ID\n"
            "workforce stop      (interrupt + stop every child process)\n"
            "workforce abandon [ID]  (close without deleting its audit)\n"
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
            workflow_id = self._launched_workflow_id
            row = self._workflow_row(workflow_id) if workflow_id else None
            status = str((row or {}).get("status") or "").lower()
            if status == "abandoned":
                self._set_selected_work(
                    "WORKFLOW CLOSED\n\nThis workforce run was abandoned and "
                    "cannot be reopened. Start a new run for further research."
                )
                return
            if status == "running":
                # No local process is running, so this is a live-looking orphan
                # from an earlier turn. Fence it before explicitly reopening.
                if self._control_workflow(
                    workflow_id,
                    "interrupt",
                    "previous coordinator turn ended before a follow-up",
                ) is None:
                    return
                status = "interrupted"
            if status in {"interrupted", "failed", "blocked"}:
                if self._control_workflow(
                    workflow_id, "resume",
                    "operator continued the coordinator conversation",
                ) is None:
                    return
                self._bind_run(workflow_id)
            else:
                # A completed run — or one the registry never recorded — cannot
                # be resumed, so this turn is a new run. Without rebinding, the
                # view stayed pinned to the finished one: the results block was
                # already printed, every phase already reported, and the new
                # workflow's id did not match, so a turn that ran to completion
                # printed nothing at all.
                self._bind_run("")
            started = self._start_claude(
                message, governed=True,
                resume_session=self._chat_sessions["workforce"])
            if not started and status in {
                "running", "interrupted", "failed", "blocked",
            }:
                self._control_workflow(
                    workflow_id,
                    "interrupt",
                    "Claude could not restart the resumed workflow",
                    quiet=True,
                )
        else:
            self._start_workforce(message)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id in self._book_plan_ids:
            self._confirm_plan_execution(self._book_plan_ids[button_id])
            return
        if button_id == "settings-change-desk":
            # The same chooser startup uses, so there is one place that decides
            # which desk is being traded and one place that shows the
            # credential behind it.
            self._start_desk_mode_prompt()
            return
        if button_id == "settings-recheck-alpaca":
            # Credential state rides on the owner's snapshot, so re-resolving
            # is a refresh. Nothing here calls Alpaca.
            self._write_local_event("alpaca.recheck", {})
            self._start_refresh()
            return
        if button_id == "settings-toggle-fast":
            self.action_toggle_fast_mode()
            return
        if button_id == "btn-atlas-refresh":
            self.action_atlas_refresh()
            return
        if button_id == "btn-atlas-escalate":
            self.action_atlas_escalate()
            return
        if button_id == "btn-atlas-observe":
            self.action_atlas_observe()
            return
        if button_id == "btn-atlas-mode":
            self.action_atlas_cycle_mode()
            return
        if button_id == "btn-atlas-autonomy":
            self.action_atlas_toggle_autonomy()
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
        if button_id == "btn-workforce-abandon":
            self.action_workforce_abandon(
                self._latest_abandonable_workflow_id())
            return
        if button_id != "chat-exit":
            return
        if self.claude.running:
            if self.claude.mode == "workforce":
                self.action_workforce_stop()
            else:
                self.claude.stop("operator stopped the chat session")
                self._console_write(f"[{GOLD}]■ chat stopped[/]")
                self._render_mode_chip()
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
        # A recognised subcommand that fails its own argument check is a
        # malformed subcommand, not a bare command with a long argument.
        # Falling through turned ": workforce stop now" into
        # action_workforce_new("stop now") — an attempt to halt a coordinator
        # launched a second one.
        named_subcommand = action_name is not None
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
        elif action_name == "action_workforce_abandon":
            action_args = (argument,)
        elif action_name is not None and argument:
            action_name = None

        if action_name is None and named_subcommand:
            self._write_local_event(
                "command.malformed", {"command": raw, "subcommand": subword})
            self._set_selected_work(
                f"{command.upper()} {subword.upper()} does not take those "
                "arguments.\n\nUse : help for the command surface.")
            return

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
            elif action_name in {"action_chat_mode", "action_workforce_new",
                                 "action_theme", "action_claude_cli"}:
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
        # The dialog the operator just answered IS the approval decision, so it
        # is recorded as one. A boolean in a request body is self-attestation
        # any local process can send; the persisted approval binds this plan's
        # digest, its targets, and the book revision as of this moment, so a
        # book that moved between preview and confirm refuses instead of
        # filling against figures the operator never saw.
        self._run_api_action(
            "paper plan execution", "/api/plans/execute",
            {"offline": self.offline, "plan_id": plan_id, "human_confirmed": True},
            active_agent="reporter",
            prepare=lambda: self._record_paper_approval(plan_id),
        )

    def _record_paper_approval(self, plan_id: str) -> dict:
        """Create and approve the record the execution will consume.

        Runs on the action worker, not the app thread, because it is two owner
        round-trips. Returns the body fragment the execution call merges in.
        """
        created = self.client.post(
            "/api/approvals", {"plan_id": plan_id, "offline": self.offline})
        approval_id = str(created.get("approval_id") or "")
        if not approval_id:
            raise RuntimeError("the owner created no approval for this plan")
        self.client.post(f"/api/approvals/{approval_id}/approve", {})
        return {"approval_id": approval_id}

    def _run_api_action(
        self,
        label: str,
        path: str,
        body: dict,
        *,
        active_agent: str | None,
        http_method: str = "post",
        prepare=None,
    ) -> None:
        """Run one owner call on a worker thread.

        `prepare` runs first, on the same worker, and its returned mapping is
        merged into the request body — for a call that must be preceded by
        other owner round-trips whose result it depends on.
        """
        if self._action_running:
            self._write_local_event("action.rejected", {"reason": "another action is running"})
            return
        self._action_running = True
        if active_agent is not None:
            self._agent_states = {name: "queued" for name in _AGENT_NAMES}
            self._agent_states[active_agent] = "working"
            self._render_agents()
        self._set_selected_work(f"{label.upper()}\n\nRunning through the owner API…")
        self._write_local_event("action.started", {"action": label})

        def run() -> None:
            try:
                call_body = dict(body)
                if prepare is not None:
                    call_body.update(prepare() or {})
                if http_method == "get":
                    result = self.client.get(path, **call_body)
                elif http_method == "post":
                    result = self.client.post(path, call_body)
                else:
                    raise ValueError(
                        f"unsupported owner API method {http_method!r}")
                self._call_from_worker(
                    self._finish_api_action,
                    label,
                    result,
                    None,
                    active_agent,
                )
            except Exception as exc:
                self._call_from_worker(
                    self._finish_api_action,
                    label,
                    None,
                    exc,
                    active_agent,
                )

        threading.Thread(
            target=run,
            daemon=True,
            name="qlab-tui-action",
        ).start()

    def _finish_api_action(
        self,
        label: str,
        result: dict | None,
        error: Exception | None,
        active_agent: str | None,
    ) -> None:
        self._action_running = False
        if error is not None:
            if active_agent is not None:
                self._agent_states = {
                    name: "failed" if state == "working" else "idle"
                    for name, state in self._agent_states.items()
                }
            self._set_selected_work(f"{label.upper()} FAILED\n\n{error!r}")
            self._write_local_event("action.failed", {"action": label, "error": repr(error)})
        else:
            if active_agent is not None:
                self._agent_states = {
                    name: "done" for name in self._agent_states}
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
        if active_agent is not None:
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
            if self._control_workflow(
                workflow_id,
                "resume",
                "operator explicitly resumed the workflow",
            ) is None:
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
            if workflow_id:
                self._control_workflow(
                    workflow_id,
                    "interrupt",
                    "Claude could not start the resumed workflow",
                    quiet=True,
                )
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
        self._launched_workflow_id = workflow_id
        self._pending_workflow_control = None
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
        self._render_mode_chip()
        return True

    def _receive_claude_event(self, event: ClaudeEvent) -> None:
        self._call_from_worker(self._apply_claude_event, event)

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
                workflow_id = self._launched_workflow_id
                reason = f"coordinator exited before completion: {event.text[-600:]}"
                if workflow_id:
                    self._control_workflow(
                        workflow_id, "interrupt", reason, quiet=True)
                elif self._pending_workflow:
                    self._pending_workflow_control = ("interrupt", reason)
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
                # A successful CLI result is not proof that all durable phases
                # finished. If the coordinator returned early, freeze the
                # live-looking phase instead of leaving every agent "working".
                workflow_id = self._launched_workflow_id
                reason = (
                    "coordinator returned before the durable workflow completed")
                if workflow_id:
                    self._control_workflow(
                        workflow_id, "interrupt", reason, quiet=True)
                elif self._pending_workflow:
                    self._pending_workflow_control = ("interrupt", reason)
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
        self._render_mode_chip()

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
