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
from qlab.tui.formatting import money, pct, sparkline, weight_bar
from qlab.paths import workspace_root


_WORKSPACE_ROOT = workspace_root()
_DEFAULT_TICKERS = ["ACWI", "BNDW", "GSG", "IGF", "GLD", "VNQ", "EMB"]
_VIEWS = ("desk", "market", "workforce", "research", "audit")
_AGENT_NAMES = (
    "moments-analyst", "challenger", "optimization-runner", "referee", "reporter",
)


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


class PaperConfirmScreen(ModalScreen[bool]):
    """Explicit confirmation for the only mutating action exposed by v1."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    CSS = """
    PaperConfirmScreen {
        align: center middle;
        background: #070a0ecc;
    }
    #paper-dialog {
        width: 68;
        height: auto;
        padding: 2 3;
        background: #111820;
        border: solid #a97832;
    }
    #paper-dialog-title {
        color: #e7edf3;
        text-style: bold;
        margin-bottom: 1;
    }
    #paper-dialog-copy {
        color: #aeb9c4;
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
        background: #070a0ecc;
    }
    #workforce-dialog {
        width: 72;
        height: auto;
        padding: 2 3;
        background: #111820;
        border: solid #48677d;
    }
    #workforce-dialog-title {
        color: #e7edf3;
        text-style: bold;
        margin-bottom: 1;
    }
    #workforce-dialog-copy {
        color: #aeb9c4;
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
                "It may deploy only the analyst, challenger, optimizer, referee, "
                "and reporter through the owner-backed MCP proxy. It receives no "
                "code, shell, filesystem, or paper-execution tools.",
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
        background: #080c11;
        color: #c5ced7;
    }

    #workspace {
        height: 1fr;
    }

    #spine {
        width: 24;
        min-width: 18;
        padding: 1 1 0 1;
        background: #0b1016;
        border-right: solid #22303c;
    }
    #wordmark {
        height: 3;
        color: #edf3f7;
        text-style: bold;
    }
    #nav {
        height: 7;
        color: #8f9ba6;
    }
    #universe-label {
        height: 2;
        margin-top: 1;
        color: #64717d;
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
        color: #9ca8b3;
    }
    #universe ListItem.-highlight {
        background: #17232e;
        color: #e6eef5;
        text-style: bold;
    }

    #canvas {
        width: 1fr;
        height: 1fr;
        padding: 1 2;
        background: #080c11;
    }
    .canvas-view {
        width: 1fr;
        height: 1fr;
        overflow-y: auto;
        scrollbar-size: 1 1;
    }
    .canvas-title {
        height: 2;
        color: #7d8995;
    }
    #desk-content, #market-content, #workforce-content {
        height: 1fr;
    }
    #research-summary, #audit-summary {
        height: 7;
        color: #aeb9c4;
    }
    #runs-table, #audit-table {
        height: 1fr;
        background: transparent;
        border: none;
        scrollbar-size: 1 1;
    }
    DataTable > .datatable--header {
        background: #111820;
        color: #7f8c98;
        text-style: none;
    }
    DataTable > .datatable--cursor {
        background: #17232e;
        color: #e6eef5;
    }

    #agent-rail {
        width: 38;
        min-width: 31;
        padding: 1 1 0 2;
        background: #0a0f15;
        border-left: solid #22303c;
    }
    #agent-label, #work-label {
        height: 2;
        color: #64717d;
    }
    #agent-list {
        height: 16;
    }
    #work-label {
        margin-top: 1;
    }
    #selected-work {
        height: 1fr;
        color: #aeb9c4;
        overflow-y: auto;
        scrollbar-size: 1 1;
    }

    #timeline {
        height: 10;
        display: none;
        padding: 0 2;
        background: #0b1016;
        border-top: solid #22303c;
        color: #96a2ad;
        scrollbar-size: 1 1;
    }
    #event-strip {
        height: 1;
        padding: 0 1;
        background: #10171e;
        color: #8d99a4;
    }
    #command-row {
        height: 2;
        padding: 0 1;
        background: #0b1016;
    }
    #command {
        width: 1fr;
        height: 1;
        border: none;
        padding: 0;
        background: transparent;
        color: #d7e0e8;
    }
    #command:focus {
        border: none;
    }
    #system-status {
        width: auto;
        min-width: 28;
        height: 1;
        text-align: right;
        color: #73808b;
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
                yield Static(id="nav", markup=True)
                yield Static("UNIVERSE", id="universe-label")
                yield ListView(
                    *(ListItem(Label(ticker)) for ticker in _DEFAULT_TICKERS),
                    id="universe",
                )

            with ContentSwitcher(initial="desk", id="canvas"):
                with Vertical(id="desk", classes="canvas-view"):
                    yield Static("DESK", classes="canvas-title")
                    yield Static(id="desk-content", markup=True)
                with Vertical(id="market", classes="canvas-view"):
                    yield Static("MARKET", classes="canvas-title")
                    yield Static(id="market-content", markup=True)
                with Vertical(id="workforce", classes="canvas-view"):
                    yield Static("WORKFORCE", classes="canvas-title")
                    yield Static(id="workforce-content", markup=True)
                with Vertical(id="research", classes="canvas-view"):
                    yield Static("RESEARCH", classes="canvas-title")
                    yield Static(id="research-summary", markup=True)
                    yield DataTable(id="runs-table", cursor_type="row")
                with Vertical(id="audit", classes="canvas-view"):
                    yield Static("AUDIT", classes="canvas-title")
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
        self._render_nav()
        self._render_agents()
        self._start_refresh()
        if self.refresh_interval > 0:
            self.set_interval(self.refresh_interval, self._start_refresh)

    def on_unmount(self) -> None:
        self.claude.stop()
        if self.owned_server is not None and self.owned_server.poll() is None:
            self.owned_server.terminate()

    def on_resize(self, event: events.Resize) -> None:
        width = event.size.width
        self.screen.set_class(105 <= width < 125, "compact")
        self.screen.set_class(width < 105, "narrow")
        if width >= 105 and self._agent_focus:
            self._agent_focus = False
            self.screen.remove_class("agent-focus")

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
                lines.append(f"[bold #dce7ef]› {key}  {view.title()}[/]")
            else:
                lines.append(f"[#7d8995]  {key}  {view.title()}[/]")
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

        lines = [
            f"[bold #edf3f7]{money(equity)}[/]   [#7d8995]cash {money(cash)}[/]",
            f"[#7d8995]drawdown[/] {pct(drawdown)}   "
            f"[#7d8995]kill switch[/] {pct(kill_at, 0)}",
            "",
            "[#64717d]CURRENT ALLOCATION                         TARGET[/]",
        ]
        held_outside = [t for t in current if t not in self.universe_tickers
                        and abs(float(current[t])) > 0.0005]
        for ticker in [*self.universe_tickers, *held_outside]:
            cur = float(current.get(ticker, 0.0))
            target = targets.get(ticker)
            target_text = "—" if target is None else f"{float(target):5.1%}"
            lines.append(
                f"{ticker:<5} [#5f8199]{weight_bar(cur)}[/] {cur:5.1%}  →  {target_text}"
            )

        lines.extend([
            "",
            "[#64717d]MARKET REGIME[/]",
            f"[bold]{str(regime.get('regime', 'unknown')).upper()}[/]   "
            f"[#7d8995]{escape(str(regime.get('method', 'unavailable')))}[/]",
            f"realized volatility signal  {pct(regime.get('signal'))}",
            f"stress threshold             {pct(regime.get('threshold'))}",
            "",
        ])

        plans = self.snapshot.get("plans", [])
        if plans:
            plan = plans[0]
            turnover = plan.get("pre_trade", {}).get("turnover")
            lines.extend([
                "[#64717d]LATEST PROPOSAL[/]",
                f"[bold]{escape(plan['plan_id'][:12])}[/]   "
                f"state {escape(str(plan.get('state', 'unknown')))}   "
                f"turnover {pct(turnover)}",
                "Use [bold]: view audit[/] to inspect the decision and order trail.",
            ])
        else:
            lines.extend([
                "[#64717d]PROPOSAL[/]",
                "No active rebalance proposal.",
            ])

        source = str(market.get("source", "unknown")).upper()
        policy = self.snapshot.get("policy") or {}
        lines.extend([
            "",
            f"[#64717d]POLICY[/] {escape(str(policy.get('label', policy.get('id', '—'))))} "
            "· MVSK research only",
            f"[#64717d]DATA[/] {source} · {market.get('frequency', 'unknown')} · "
            f"as of {market.get('as_of', '—')} · age {market.get('bar_age_days', '—')}d",
        ])
        self.query_one("#desk-content", Static).update("\n".join(lines))

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
        line = sparkline(row.get("history", []))
        direction = "#79a88b" if row.get("change_1d", 0.0) >= 0 else "#c97878"
        content = "\n".join([
            f"[bold #edf3f7]{escape(self.active_ticker)}[/]   "
            f"[bold]{money(row.get('price'))}[/]   "
            f"[{direction}]{pct(row.get('change_1d'))} today[/]",
            "",
            f"[#5f8199]{line}[/]",
            "[#64717d]40 DAILY BARS[/]",
            "",
            f"20-day change          {pct(row.get('change_20d'))}",
            f"63-day realized vol    {pct(row.get('realized_vol'))}",
            f"portfolio weight       {pct(current)}",
            f"target weight          {pct(target)}",
            "",
            "[#64717d]CONTEXT[/]",
            f"regime                 {str(market.get('regime', {}).get('regime', 'unknown')).upper()}",
            f"source                 {str(market.get('source', 'unknown')).upper()}",
            f"as of                  {market.get('as_of', '—')}",
            f"bar age                {market.get('bar_age_days', '—')} days",
            "",
            "[#64717d]Daily adjusted-close context; this is not a streaming quote.[/]",
        ])
        self.query_one("#market-content", Static).update(content)

    def _render_workforce(self) -> None:
        workflows = self.snapshot.get("workflows", []) if self.snapshot else []
        if not workflows:
            content = (
                "[#64717d]NO DURABLE RUN[/]\n\n"
                "Start one with [bold]: workforce GOAL[/]. Claude becomes the "
                "coordinator and deploys the five role-bound agents in order.\n\n"
                "The owner process persists every phase. Stopping Claude leaves "
                "the run inspectable and resumable."
            )
            self.query_one("#workforce-content", Static).update(content)
            return

        workflow = workflows[0]
        request = workflow.get("request") or {}
        lines = [
            f"[bold #edf3f7]{escape(str(workflow.get('workflow_id', '—')))}[/]   "
            f"{escape(str(workflow.get('status', 'unknown')).upper())}",
            f"[#64717d]{escape(str(workflow.get('kind', 'portfolio_review')))} · "
            f"as of {escape(str(request.get('as_of', '—')))} · "
            f"{escape(str(request.get('universe', 'core')))}[/]",
            "",
            f"{escape(str(request.get('goal', 'Governed portfolio review')))}",
            "",
            "[#64717d]PHASE                                      STATE[/]",
        ]
        for step in workflow.get("steps", []):
            state = str(step.get("status", "queued"))
            glyph, color = {
                "working": ("●", "#6d9fbf"),
                "queued": ("◐", "#b38b54"),
                "done": ("✓", "#79a88b"),
                "failed": ("×", "#c97878"),
                "blocked": ("!", "#d0a45c"),
            }.get(state, ("◌", "#5f6b76"))
            lines.append(
                f"[{color}]{glyph}[/] {escape(str(step.get('phase', ''))):<12}  "
                f"{escape(str(step.get('agent', ''))):<22} {escape(state)}"
            )
            summary = str(step.get("summary") or "").strip()
            if summary:
                lines.append(f"   [#7d8995]{escape(summary[:140])}[/]")
        earlier = workflows[1:5]
        if earlier:
            lines.extend(["", "[#64717d]EARLIER RUNS"
                          "                (resume with : workforce resume ID)[/]"])
            for row in earlier:
                lines.append(
                    f"{escape(str(row.get('workflow_id', '—')))}  "
                    f"{escape(str(row.get('status', '')))}"
                    f" · {escape(str(row.get('current_phase', '')))}"
                )
        lines.extend([
            "",
            "[#64717d]Claude is coordination only. Paper execution remains a "
            "separate human-confirmed action.[/]",
        ])
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
        """Surface a selected decision's challenger case and verdict reasons.

        The quiet workstation has no detail pane, so the one-line event strip
        carries the governance context on row selection — no banner, no flood.
        """
        decision = self._audit_decisions.get(str(key))
        if not decision:
            return
        challenger = str(decision.get("challenger_view") or "").strip() or "no challenge recorded"
        verdict = decision.get("verdict") or {}
        label = str(verdict.get("verdict", "—")).upper() if verdict else "—"
        reasons = verdict.get("reasons") or []
        reason_text = "; ".join(str(reason) for reason in reasons) if reasons else "—"
        line = (
            f"{str(key)[:10]}  challenger: {challenger}  ·  "
            f"verdict {label}: {reason_text}"
        )
        if len(line) > 200:
            line = line[:199] + "…"
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
                "working": ("●", "#6d9fbf"),
                "queued": ("◐", "#b38b54"),
                "waiting": ("◐", "#b38b54"),
                "done": ("✓", "#79a88b"),
                "failed": ("×", "#c97878"),
                "blocked": ("!", "#d0a45c"),
            }.get(state, ("◌", "#5f6b76"))
            lines.append(
                f"[{color}]{glyph}[/] [bold]{escape(name)}[/]\n"
                f"   [#6f7c87]{escape(state)} · {escape(str(agent.get('authority', '—')))}[/]"
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

    # -- events -----------------------------------------------------------
    def _ingest_events(self, events_: list[dict]) -> None:
        for event in events_:
            event_id = str(event.get("event_id", ""))
            if event_id and event_id in self._event_ids:
                continue
            if event_id:
                self._event_ids.add(event_id)
            self._append_event(event)

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
        if raw.startswith(":"):
            raw = raw[1:].strip()
        if not raw:
            return
        self._handle_command(raw)

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
                "workforce GOAL\n"
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
        elif command in {"workforce", "governed"}:
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
        self._start_claude(prompt, governed=True)

    def _start_claude(self, prompt: str, *, governed: bool) -> None:
        if self.claude.running:
            self._write_local_event("claude.rejected", {"reason": "session already running"})
            return
        self._claude_buffer = ""
        self._claude_saw_delta = False
        mode = "WORKFORCE" if governed else "READ-ONLY"
        self._set_selected_work(f"CLAUDE · {mode}\n\nStarting streaming session…")
        if not self.claude.start(prompt, governed=governed):
            self._set_selected_work("Claude Code is not available or a session is already running.")
            return
        self._write_local_event(
            "claude.started",
            {"mode": "workforce" if governed else "read-only", "prompt": prompt[:120]},
        )
        self._render_status()

    def _receive_claude_event(self, event: ClaudeEvent) -> None:
        try:
            self.call_from_thread(self._apply_claude_event, event)
        except Exception:
            pass  # app closed while the reader thread was finishing

    def _apply_claude_event(self, event: ClaudeEvent) -> None:
        if event.kind == "text_delta":
            self._claude_saw_delta = True
            self._claude_buffer += event.text
            self._set_selected_work(
                f"CLAUDE · {self.claude.mode.upper()}\n\n" + self._claude_buffer[-6000:])
        elif event.kind == "text":
            if not self._claude_saw_delta:
                self._claude_buffer += event.text
                self._set_selected_work(
                    f"CLAUDE · {self.claude.mode.upper()}\n\n" + self._claude_buffer[-6000:])
        elif event.kind == "tool_start":
            self._set_agent_from_tool(event.tool, event.agent)
            payload = {"tool": event.tool}
            if event.agent:
                payload["agent"] = event.agent
            self._write_local_event("claude.tool", payload)
        elif event.kind == "error":
            self._set_selected_work("CLAUDE FAILED\n\n" + event.text[-4000:])
            self._write_local_event("claude.failed", {"error": event.text[-400:]})
            self._start_refresh()
        elif event.kind == "result":
            if not self._claude_buffer and event.text:
                self._set_selected_work(
                    f"CLAUDE · {self.claude.mode.upper()}\n\n" + event.text[-6000:])
            self._write_local_event("claude.completed", {})
            self._start_refresh()
        self._render_status()

    def _set_agent_from_tool(self, tool: str, explicit_agent: str = "") -> None:
        if explicit_agent in _AGENT_NAMES:
            agent = explicit_agent
        elif "workflow.analyst" in tool or "objective.build" in tool:
            agent = "moments-analyst"
        elif "workflow.challenger" in tool:
            agent = "challenger"
        elif "workflow.optimizer" in tool or "algorithms.solve" in tool:
            agent = "optimization-runner"
        elif "workflow.referee" in tool or "log_verdict" in tool or "backtest.run" in tool:
            agent = "referee"
        elif ("workflow.reporter" in tool or "rebalance_preview" in tool
              or "daily_ops" in tool or "portfolio.state" in tool
              or "report.recommendation" in tool):
            agent = "reporter"
        else:
            return
        self._agent_states[agent] = "working"
        self._render_agents()

    def _set_selected_work(self, text: str) -> None:
        self.query_one("#selected-work", Static).update(escape(text))
