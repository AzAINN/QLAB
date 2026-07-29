"""A dependency-free HTTP owner runtime exposing qlab operations as JSON.

The socket server is threaded so parallel browser connections stay responsive;
all dispatch is serialized under one lock because the shared DuckDB registry is
the paper book. The UI, CLI, TUI, and autopilot therefore see one owner state.

Routes
------
GET  /                         the single-page app
GET  /api/bootstrap            universe, mandate, agents, portfolio, defaults
GET  /api/portfolio            broker-truth positions + risk report
GET  /api/portfolio/live       live mark-to-market: P&L, exposure, provenance
GET  /api/market               provenance-tagged daily market snapshot
GET  /api/system               runtime health and authority state
GET  /api/desk_mode            the chosen data source and book + credential status
POST /api/desk_mode            choose the data source and the book
GET  /api/data/health          data-policy provenance, freshness, eligibility
GET  /api/data/permit/current  the latest recorded data permit for a purpose
GET  /api/quotes               latest cached quotes + live market-stream health
GET  /api/regime/panel         all regime indicators on one snapshot (diagnostic)
GET  /api/decisions/similar    point-in-time recall of analogous decisions
GET  /api/decisions/<id>/outcome   the immutable resolved outcome
GET  /api/decisions/<id>/lesson    advisory lesson over that outcome (if any)
GET  /api/workflows/<id>/debate    debates, turns, and adjudication
GET  /api/models/invocations   model tier/route audit records
GET  /api/atlas/status           Atlas mode, lifecycle state, heartbeat
GET  /api/atlas/read             Atlas's composed read: signals + news + research
POST /api/atlas/escalate         open a bounded debate on a material disagreement
GET  /api/atlas/tasks            Atlas's deduplicated autonomous task history
GET  /api/atlas/templates        the registered workflow templates Atlas may start
GET  /api/atlas/startable        queued tasks Atlas may start now, with refusals
GET  /api/atlas/shadow           shadow-rollout scorecard (evidence, not a grant)
POST /api/atlas/tasks/<id>/start start one queued task's registered template
POST /api/atlas/observe          run one deterministic Atlas observe tick
POST /api/atlas/mode             set Atlas mode (observe|research|propose|paused)
POST /api/atlas/pause            pause Atlas's autonomous work
POST /api/atlas/resume           resume Atlas into a mode
POST /api/atlas/message          ask Atlas a question (never grants authority)
GET  /api/events               event stream with cursor and limit
GET  /api/plans                recent order plans
GET  /api/orders               recent orders
GET  /api/agents               deployed agent definitions
GET  /api/algorithms           categorized algorithm deployment catalog
GET  /api/policy               configured operational allocation policy
GET  /api/reference                curated component catalog + live champion/stage overlay
GET  /api/performance          realized equity curve + metrics from the equity marks
GET  /api/workflows            durable Claude-workforce runs and phase state
GET  /api/workflows/<id>       one durable workflow and its ordered steps
GET  /api/stream               durable audit + transient market events (live)
GET  /api/tui                  one consistent terminal snapshot
POST /api/lab/<tool>           bounded research tool executed by this owner
POST /api/workflows/start      begin a standard or panel workforce run
POST /api/workflows/<phase>    update one role-bound workflow phase
POST /api/workflows/<id>/interrupt  pause a run for explicit resumption
POST /api/workflows/<id>/resume     reopen an interrupted/failed/blocked run
POST /api/workflows/<id>/abandon    permanently close an incomplete run
POST /api/rebalance_preview    build an exact, referee-bound checked plan
POST /api/plans/execute        human-confirm one existing checked paper plan
POST /api/approvals            create a plan-bound, expiring approval request
GET  /api/approvals            list approval requests (optionally by status)
GET  /api/approvals/<id>       one approval request
POST /api/approvals/<id>/approve|reject|challenge   the human decision
POST /api/plans/<id>/execute   execute by consuming a matching human approval
POST /api/performance/backfill merge the broker's own equity history into the marks
POST /api/recommend            an operational allocation recommendation
POST /api/run_once             one autopilot iteration (analyze -> solve -> trade)
POST /api/daily_ops            heartbeat (reconcile/risk/triggers; never trades)
POST /api/batch                the reproducible ablation
GET  /api/runs                 recent registry runs
GET  /api/decisions            recent decisions (the reflection loop)
POST /api/reset                reset the paper book to starting capital
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import threading
import time
import uuid
import webbrowser
from collections import deque
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from qlab.core.desk_mode import (
    DEFAULT_DESK_MODE, DeskMode, load_desk_mode, save_desk_mode)
from qlab.core.types import _jsonable
from qlab.paths import workspace_root

_HERE = Path(__file__).resolve().parent
_INDEX = _HERE / "index.html"

# A ThreadingHTTPServer keeps the browser's parallel/keep-alive connections
# responsive, but the shared DuckDB connection is not thread-safe, so every
# dispatch runs under this lock.
# Effectively one request computes at a time (fine for a local single user),
# while the socket layer never stalls.
_LOCK = threading.Lock()
# How long a stream poll waits for the dispatch lock before proving the socket
# is alive instead. Must stay comfortably under the client's stream read
# deadline, or a long owner action expires the client before it hears anything.
_STREAM_LOCK_WAIT_SECONDS = 2.0

_MARKET_EVENT_LIMIT = 500
_STREAM_PAGE_CEILING = 5000
_QUOTE_REFRESH_TTL_SECONDS = 30.0
_QUOTE_MIN_INTERVAL_SECONDS = 5.0
_MARKET_THREAD_NAME = "qlab-market-topics"
_MARKET_THREAD_JOIN_TIMEOUT_SECONDS = 5.0
_MARKET_THREAD_LOCK = threading.Lock()
_ACTIVE_MARKET_THREAD: threading.Thread | None = None
# Claude's hard session ceiling is 30 minutes. A running registry row with no
# update beyond this grace cannot belong to a healthy qlab coordinator.
_WORKFLOW_STALE_AFTER_SECONDS = 35 * 60
_WORKFLOW_REAP_INTERVAL_SECONDS = 60.0

# Realized-performance window. `_MARK_WINDOW` is the newest-N read cap; when the
# book has more marks than that, the payload says so rather than letting a
# truncated series pass for the whole history.
_MARK_WINDOW = 5000
_CHART_DAYS = 365
_DAYS_PER_YEAR = 365.25

_GATED_WORKFORCE_ROLES = frozenset({
    "moments-analyst",
    "challenger",
    "optimization-runner",
    "referee",
    "reporter",
})


# These research operations may be reached through the TUI's stateless MCP
# proxy. They can write research/audit rows, but none can mutate the paper book
# or submit an order. The allowlist is enforced again here at the owner boundary.
OWNER_LAB_TOOLS = frozenset({
    "data.fetch_universe",
    "data.snapshot_summary",
    "qa.data_integrity",
    "moments.estimate",
    "selection.run",
    "regime.hmm",
    "regime.turbulence",
    "regime.absorption",
    "regime.volatility_term_structure",
    "regime.drawdown",
    "regime.tail_risk",
    "news.market",
    "objective.build",
    "algorithms.list",
    "algorithms.describe",
    "policy.current",
    "algorithms.solve",
    "solve.classical",
    "backtest.run",
    "news.fetch",
    "research.apply_views",
    "research.equilibrium_returns",
    "research.predict_vol",
    "research.window_evidence",
    "registry.list_runs",
    "registry.report",
    "registry.log_decision",
    "registry.recent_decisions",
    "registry.attach_challenge",
    "registry.log_verdict",
    "report.recommendation",
})


class _OwnerToolApp:
    """Minimal decorator target used to mount the existing lab tool functions."""

    def __init__(self):
        self.tools: dict[str, object] = {}

    def tool(self, *, name: str):
        def register(fn):
            self.tools[name] = fn
            return fn

        return register


class UISession:
    """Process-wide state: one registry (the paper book) + the mandate."""

    def __init__(self, offline_default: bool = True, seed: int = 7, registry=None,
                 desk_mode: DeskMode | None = None):
        from qlab.trader.mandate import load_mandate
        from qlab.mcp.guardrails import LabState
        from qlab.mcp.quant_lab import register_lab_tools
        from qlab.state.registry import Registry

        self.registry = registry or Registry()
        # A coordinator is process-local. If a new owner acquired this registry,
        # no old coordinator lease survived with it; leave completed evidence
        # intact and turn only live-looking rows into resumable interruptions.
        self.registry.interrupt_running_workflows(
            "owner runtime restarted before the coordinator completed")
        self.mandate = load_mandate()
        # The operator's explicit choice; the persisted value is authoritative
        # when the caller passes none, and ``offline_default`` only seeds the
        # mode nobody has chosen yet — never a second opinion about it.
        self.desk_mode = desk_mode or load_desk_mode() or (
            DEFAULT_DESK_MODE if offline_default else DeskMode("live", "simulated"))
        # Derived, never carried alongside: a launcher flag and a persisted (or
        # POSTed) mode used to disagree, and the disagreement reconstructed
        # `synthetic` + `alpaca` — synthetic quotes on the SSE bus and a
        # synthetic portfolio for a real Alpaca book.
        self.offline_default = self.desk_mode.offline
        self.seed = seed
        self._market_events: deque[dict] = deque(maxlen=_MARKET_EVENT_LIMIT)
        self._market_lock = threading.Lock()
        self._last_quote_signature: tuple[tuple[str, float, float], ...] | None = None
        self._regime_hmm_cache: dict[str, dict[str, object]] = {}
        # The live market stream is attached only under an operational policy
        # (a real Alpaca feed); it stays None for demo/offline runtimes.
        self.market_stream = None
        self._last_poll_mark = 0.0
        self._last_workflow_reap = 0.0
        # Atlas's composed qualitative read, refreshed by the heartbeat.
        self._desk_read: dict | None = None
        # The last news window, written only by fetch_desk_news — which runs
        # outside the owner dispatch lock. desk_read composes from this and
        # never fetches, so a cold cache cannot stall the lock on RSS timeouts.
        self._desk_news: dict | None = None
        self.heartbeat = None
        # Autonomy is a runtime switch the operator owns from the UI.
        # The env var only seeds its initial value.
        self.autonomous = os.environ.get("QLAB_ATLAS_AUTONOMOUS") == "1"
        self.registry.init_account(self.mandate.paper_capital)
        self.lab_state = LabState(
            registry=self.registry, max_calls=200,
            offline=self.offline_default, seed=seed,
        )
        owner_tools = _OwnerToolApp()
        register_lab_tools(owner_tools, self.lab_state, owner_only=True)
        self._lab_tools = owner_tools.tools
        # Atlas: the deterministic desk supervisor. It degrades (not fails)
        # when the coordinator (Claude) is absent and holds no execution or
        # proposal authority in Observe mode.
        from qlab.operator.atlas import AtlasSupervisor

        self.atlas = AtlasSupervisor(
            self.registry,
            coordinator_available=lambda: bool(shutil.which("claude")))
        # A dispatched workflow can reach a terminal state while no owner is
        # running, and the restart above has just interrupted anything that was
        # still live. Either way the bound task must be resolved now rather than
        # waiting for the first observe tick -- a task that outlives its
        # workflow is the state this whole path exists to prevent.
        self.atlas.reconcile_tasks()

    # -- Claude workforce --------------------------------------------------
    def call_lab_tool(self, name: str, body: dict, offline: bool) -> object:
        """Run a safe research tool in-process so this owner keeps DuckDB."""
        if name not in OWNER_LAB_TOOLS or name not in self._lab_tools:
            raise PermissionError(f"lab tool {name!r} is not exposed by the owner")
        self.lab_state.offline = offline
        args = {key: value for key, value in body.items() if key != "offline"}
        return self._lab_tools[name](**args)  # type: ignore[operator]

    def start_workflow(self, body: dict, *,
                       phases: tuple[str, ...] | None = None) -> dict:
        """Start a durable portfolio/research workforce run.

        `phases` is an in-process argument only, never read from `body`: letting
        a network caller shape the phase graph would let it drop a gate phase.
        The registry validates whatever graph it is given for dependency
        closure, so a graph that omits the referee cannot reach a reporter and
        therefore cannot produce a plan either way -- but the narrower surface
        is the point.
        """
        from qlab.mcp.guardrails import CallBudget

        kind = str(body.get("kind") or "portfolio_review")
        variants = body.get("variants")
        if kind == "panel":
            if not isinstance(variants, list):
                raise ValueError(
                    "panel variants must be a list[dict] with 2..5 entries"
                )
            if not 2 <= len(variants) <= 5:
                raise ValueError(
                    f"panel variants must contain 2..5 entries, got {len(variants)}"
                )
            if not all(isinstance(variant, dict) for variant in variants):
                raise ValueError("every panel variant must be an object")

        self.lab_state.budget = CallBudget(
            200,
            on_charge=lambda tool: self.registry.record_event(
                "tool_call", {"tool": tool},
            ),
        )
        request = {
            "goal": str(
                body.get("goal") or "Prepare a governed portfolio review."
            )[:4000],
            "as_of": str(body.get("as_of") or date.today().isoformat()),
            "universe": str(body.get("universe") or "core"),
            "offline": bool(body.get("offline", self.offline_default)),
        }
        if kind == "panel":
            request["variants"] = [dict(variant) for variant in variants]
            # Panels build their own instance DAG; a declared graph would fight it.
            return self.registry.start_workflow(kind, request)
        if phases:
            return self.registry.start_workflow(kind, request, phases=phases)
        return self.registry.start_workflow(kind, request)

    def update_workflow(self, phase: str, body: dict) -> dict:
        return self.registry.update_workflow_phase(
            str(body.get("workflow_id") or ""),
            phase,
            str(body.get("status") or "working"),
            str(body.get("summary") or ""),
            body.get("artifacts") if isinstance(body.get("artifacts"), dict) else {},
        )

    def control_workflow(
        self,
        workflow_id: str,
        action: str,
        body: dict,
    ) -> dict:
        """Apply a human/operator lifecycle transition through the sole writer."""
        reason = str(body.get("reason") or "").strip()
        if action == "interrupt":
            return self.registry.interrupt_workflow(
                workflow_id,
                reason or "operator stopped the coordinator before completion",
            )
        if action == "resume":
            return self.registry.resume_workflow(workflow_id)
        if action == "abandon":
            return self.registry.abandon_workflow(
                workflow_id,
                reason or "operator abandoned the incomplete workflow",
            )
        raise ValueError(f"unknown workflow control action {action!r}")

    def reap_stale_workflows(self, *, force: bool = False) -> list[dict]:
        """Turn expired live-looking rows into resumable interruptions."""
        now = time.monotonic()
        if (
            not force
            and now - self._last_workflow_reap < _WORKFLOW_REAP_INTERVAL_SECONDS
        ):
            return []
        self._last_workflow_reap = now
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(seconds=_WORKFLOW_STALE_AFTER_SECONDS)
        ).isoformat()
        return self.registry.interrupt_running_workflows(
            "coordinator lease expired after the workforce time limit",
            updated_before=cutoff,
        )

    def rebalance_preview(self, body: dict, offline: bool) -> dict:
        """Build a checked plan from the optimizer's exact reviewed targets."""
        from qlab.state.registry import targets_hash
        from qlab.trader.broker import get_broker
        from qlab.trader.mandate import MandateViolation
        from qlab.trader.plan import build_plan
        from qlab.trader.reconcile import reconcile

        decision_id = str(body.get("decision_id") or "")
        raw_targets = body.get("targets")
        if not decision_id or not isinstance(raw_targets, dict) or not raw_targets:
            raise ValueError("decision_id and non-empty targets are required")
        targets = {str(ticker): float(weight) for ticker, weight in raw_targets.items()}
        if self.registry.get_decision(decision_id) is None:
            return {
                "accepted": False, "blocked_by": "decision",
                "reason": f"unknown decision {decision_id!r}",
            }
        verdict = self.registry.get_verdict(decision_id)
        if not verdict or verdict.get("verdict") != "PASS":
            return {
                "accepted": False, "blocked_by": "referee",
                "reason": f"no PASS for decision {decision_id!r}",
            }
        if verdict.get("targets_hash") != targets_hash(targets):
            return {
                "accepted": False, "blocked_by": "referee",
                "reason": "PASS does not cover these exact targets",
            }
        broker = get_broker(
            self.registry, offline=offline,
            starting_cash=self.mandate.paper_capital, seed=self.seed,
            universe=self.mandate.universe_whitelist,
            book=self.desk_mode.book,
        )
        rec = reconcile(
            self.registry, broker, self.mandate.universe_whitelist,
        )
        if not rec["clean"]:
            return {"accepted": False, "blocked_by": "reconcile", "reconcile": rec}
        # Drawdown-tier gate at preview: an agent PASS binds the targets but a
        # control-tier drawdown still forbids increasing gross exposure, so the
        # preview must re-run the deterministic tier check against the live book
        # — otherwise a preview could accept an exposure increase the referee
        # would fail.
        from qlab.governance.referee import deterministic_referee

        book = broker.portfolio_state(self.mandate.universe_whitelist)
        _, tier_reasons = deterministic_referee(
            targets, self.mandate, date.today(), portfolio_state=book)
        drawdown_reasons = [
            r for r in tier_reasons
            if "tier blocks gross exposure" in r
            or "tier permits liquidation only" in r]
        if drawdown_reasons:
            return {"accepted": False, "blocked_by": "drawdown_tier",
                    "reasons": drawdown_reasons}
        try:
            plan = build_plan(
                self.registry, broker, self.mandate, targets, decision_id,
            )
        except MandateViolation as exc:
            return {"accepted": False, "mandate_violation": str(exc)}
        # The net-alpha gate applies to previews too — a refused plan is
        # terminally refused in the registry, so the human-confirm path
        # cannot execute it either.
        from qlab.governance.referee import cost_gate

        state = broker.portfolio_state(self.mandate.universe_whitelist)
        weights = state.get("weights", {})
        gate_reasons = cost_gate(
            plan.pre_trade, float(state["equity"]),
            sum(abs(float(w)) for w in weights.values()),
            len(state.get("positions", {})), self.mandate)
        if gate_reasons:
            self.registry.set_plan_state(plan.plan_id, "refused")
            self.registry.record_event("cost_gate_refusal", {
                "decision_id": decision_id, "plan_id": plan.plan_id,
                "reasons": gate_reasons})
            return {"accepted": False, "blocked_by": "cost_gate",
                    "reasons": gate_reasons, "plan_id": plan.plan_id,
                    "expected_cost": plan.pre_trade.get("expected_cost")}
        return {
            "accepted": True, "plan_id": plan.plan_id, "state": plan.state,
            "pre_trade": plan.pre_trade, "n_legs": len(plan.legs),
            "decision_id": decision_id, "targets_hash": targets_hash(targets),
            "note": "checked dry preview only; Claude cannot execute it",
        }

    # -- portfolio view -----------------------------------------------------
    def portfolio(self, offline: bool) -> dict:
        from qlab.trader.broker import get_broker

        broker = get_broker(self.registry, offline=offline,
                            starting_cash=self.mandate.paper_capital,
                            seed=self.seed, universe=self.mandate.universe_whitelist,
                            book=self.desk_mode.book)
        state = broker.portfolio_state(self.mandate.universe_whitelist)
        hwm = state.get("high_water_mark", state["equity"])
        dd = 1.0 - state["equity"] / hwm if hwm > 0 else 0.0
        last = self.registry.recent_decisions(limit=1)
        targets = last[0].get("choice", {}).get("targets", {}) if last else {}
        return {
            "broker": broker.name,
            "cash": state["cash"], "equity": state["equity"],
            "high_water_mark": hwm, "drawdown": round(dd, 4),
            "kill_switch_at": self.mandate.trailing_drawdown_pct,
            "kill_switch_distance": round(self.mandate.trailing_drawdown_pct - dd, 4),
            "halted": state["halted"],
            "positions": state["positions"], "weights": state["weights"],
            "target_weights": targets,
        }

    def live_portfolio(self, offline: bool) -> dict:
        """Mark the paper book to live prices and evaluate it, with provenance.

        Unlike :meth:`portfolio` this reports a full mark-to-market: per-position
        unrealized P&L against the booked average price, gross/net exposure, and
        the provenance of the marks (live Alpaca trades vs demo synthetic). In an
        operational policy it fails loud into a ``blocked`` report when live data
        is unavailable rather than valuing the book on fabricated prices.

        Two P&L views exist and must never be shown under one label: the broker's
        own ``unrealized_pl`` (in ``portfolio_state``) is the venue's view and is
        the reconcile target, while the ``unrealized_pnl`` computed here is the
        registry-booked view, marked against the average price qlab recorded.
        Disagreement between them is a reconciliation finding, not a display
        choice.

        Quote-level (seconds) freshness arrives with the market stream; here the
        marks are the broker's latest available prices and provenance is reported
        at the daily-bar level.
        """
        from qlab.core import data as market
        from qlab.trader.broker import get_broker

        policy = market.policy_for(offline, seed=self.seed)
        universe = self.mandate.universe_whitelist
        try:
            broker = get_broker(
                self.registry, offline=offline,
                starting_cash=self.mandate.paper_capital,
                seed=self.seed, universe=universe, book=self.desk_mode.book)
            state = broker.portfolio_state(universe)
        except (market.DataUnavailable, RuntimeError) as exc:
            return {"blocked": True, "mode": policy.mode, "provider": policy.provider,
                    "feed": policy.feed, "reason": str(exc)}

        equity = float(state["equity"])
        booked = self.registry.get_positions()
        marked = state.get("positions", {})
        rows = []
        gross = net = unrealized = 0.0
        for ticker, pos in booked.items():
            qty = float(pos.get("qty", 0.0))
            if abs(qty) < 1e-12:
                continue
            avg_price = float(pos.get("avg_price", 0.0))
            price = float(marked.get(ticker, {}).get("price", avg_price))
            value = qty * price
            cost = qty * avg_price
            pnl = value - cost
            weight = value / equity if equity > 0 else 0.0
            gross += abs(weight)
            net += weight
            unrealized += pnl
            rows.append({
                "ticker": ticker, "qty": qty, "avg_price": avg_price,
                "price": price, "value": value, "weight": round(weight, 6),
                "unrealized_pnl": pnl,
                "unrealized_pnl_pct": (pnl / cost if abs(cost) > 1e-9 else 0.0),
            })

        hwm = float(state.get("high_water_mark", equity))
        dd = 1.0 - equity / hwm if hwm > 0 else 0.0
        live = broker.name == "alpaca_paper"
        stream = self.market_stream
        quotes_fresh = stream.quotes_fresh() if stream else None
        return {
            "blocked": False,
            "broker": broker.name,
            "cash": float(state["cash"]),
            "equity": equity,
            "high_water_mark": hwm,
            "drawdown": round(dd, 4),
            "kill_switch_at": self.mandate.trailing_drawdown_pct,
            "kill_switch_distance": round(self.mandate.trailing_drawdown_pct - dd, 4),
            "halted": bool(state["halted"]),
            "gross_exposure": round(gross, 6),
            "net_exposure": round(net, 6),
            "unrealized_pnl": unrealized,
            "positions": rows,
            "marks": {
                "live": live,
                "source": "alpaca" if live else policy.provider,
                "feed": policy.feed if live else None,
                # Execution-grade requires an operational policy AND a fresh
                # live quote stream; a stale stream withdraws it.
                "execution_grade": bool(
                    live and policy.execution_eligible and quotes_fresh),
                "quotes_fresh": quotes_fresh,
                "quote_health": stream.health() if stream else None,
            },
        }

    # -- desk mode ----------------------------------------------------------
    def set_desk_mode(self, mode: DeskMode) -> DeskMode:
        self.desk_mode = mode
        # The mode owns the data lane too: the TUI retunes an owner that was
        # spawned with no flags, so leaving these behind would keep publishing
        # synthetic quotes and pricing a real book off the synthetic feed.
        self.offline_default = mode.offline
        self.lab_state.offline = mode.offline
        save_desk_mode(mode)
        return mode

    def desk_mode_payload(self) -> dict:
        from qlab.trader.alpaca_auth import (
            AlpacaAuthError, describe_credentials, resolve_alpaca_credentials)

        try:
            creds = resolve_alpaca_credentials()
            description, ok = describe_credentials(creds), creds is not None
        except AlpacaAuthError as exc:
            # A broken credential source is not the same as absence: say so.
            description, ok = str(exc), False
        return {
            "data": self.desk_mode.data,
            "book": self.desk_mode.book,
            "label": self.desk_mode.label,
            "offline": self.desk_mode.offline,
            "credentials": description,
            "credentials_ok": ok,
        }

    # -- realized performance ----------------------------------------------
    def current_book(self, offline: bool) -> str:
        """Name of the book being traded now — the marks' partition key.

        Constructing the broker is the only honest answer: the chosen book can
        change between sessions without anything in the registry changing.
        """
        from qlab.trader.broker import get_broker

        return get_broker(
            self.registry, offline=offline,
            starting_cash=self.mandate.paper_capital, seed=self.seed,
            universe=self.mandate.universe_whitelist,
            book=self.desk_mode.book).name

    def record_equity_mark(self, source: str, offline: bool) -> None:
        state = self.portfolio(offline)
        self.registry.log_equity_mark(
            datetime.now(timezone.utc).isoformat(),
            state["equity"], cash=state["cash"], source=source,
            book=state["broker"])

    def performance(self, offline: bool) -> dict:
        """Realized equity curve and metrics for the CURRENT book's marks.

        Two invariants the payload has to carry, not assume:

        * One book per series. A simulated book near $10k and an Alpaca account
          at a different equity level share a marks table but not a return
          series — a venue switch is a bookkeeping event, not a market move.
          Marks from another book are excluded and the exclusion is stated in
          ``note``; they are never silently dropped.
        * The annualization factor is observed, not assumed. Marks land whenever
          the desk ran (hourly polls on any day the TUI was open, weekends
          included; ``daily`` marks whenever daily ops ran), so 252 periods a
          year is a claim the data cannot support. ``cadence`` reports the factor
          actually used and the span it came from.
        """
        import pandas as pd

        from qlab.core.metrics import compute_metrics

        book = self.current_book(offline)
        rows = self.registry.equity_marks(_MARK_WINDOW, book=book)
        total = self.registry.count_equity_marks(book=book)
        excluded = self.registry.count_equity_marks() - total
        notes: list[str] = []
        if excluded:
            notes.append(f"{excluded} mark(s) from another book excluded; "
                         f"this series is {book} only")
        if total > len(rows):
            notes.append(f"history capped at the newest {_MARK_WINDOW} "
                         f"marks of {total}")
        payload = {
            "book": book, "marks": len(rows), "marks_total": total,
            "marks_capped": total > len(rows), "mark_limit": _MARK_WINDOW,
            "excluded_marks": excluded,
        }
        if not rows:
            notes.insert(0, "no equity history yet")
            return {"series": [], "metrics": None, "since_start": None,
                    "window_change": None, "cadence": None,
                    "note": "; ".join(notes), **payload}
        frame = pd.DataFrame(rows)
        frame["ts"] = pd.to_datetime(frame["ts"], utc=True, format="ISO8601")
        daily = (frame.set_index("ts").sort_index()["equity"]
                 .resample("1D").last().dropna())
        window = daily.tail(_CHART_DAYS)
        series = [{"ts": stamp.date().isoformat(), "equity": float(value)}
                  for stamp, value in window.items()]
        returns = daily.pct_change().dropna()
        cadence = _observed_cadence(daily)
        metrics = (compute_metrics(
            returns, periods_per_year=cadence["periods_per_year"])
            if cadence is not None and len(returns) >= 3 else None)
        # compute_metrics reports only n_obs below three observations; a partial
        # bundle must not reach the client as if it were a full one.
        if metrics is not None and "sharpe" not in metrics:
            metrics = None
        if metrics is None:
            notes.append("insufficient history for realized metrics "
                         "(need >=4 daily marks)")
        start = float(daily.iloc[0])
        since_start = float(daily.iloc[-1] / start - 1.0) if start > 0 else None
        # The chart draws `series`; the percentage rendered beside it must come
        # from that same window, not from a first mark off its left edge.
        first = float(window.iloc[0])
        window_change = (float(window.iloc[-1] / first - 1.0)
                         if first > 0 else None)
        return {"series": series, "metrics": metrics,
                "since_start": since_start, "window_change": window_change,
                "cadence": cadence, "note": "; ".join(notes) or None, **payload}

    def backfill_equity_history(self, offline: bool) -> dict:
        """Merge the broker's own equity history into the marks, idempotently."""
        from qlab.trader.broker import get_broker

        broker = get_broker(
            self.registry, offline=offline,
            starting_cash=self.mandate.paper_capital, seed=self.seed,
            universe=self.mandate.universe_whitelist, book=self.desk_mode.book)
        if not hasattr(broker, "portfolio_history"):
            raise RuntimeError(
                f"broker {broker.name!r} exposes no portfolio history to backfill")
        inserted = sum(
            self.registry.log_equity_mark(
                row["ts"], row["equity"], cash=None, source="alpaca_backfill",
                book=broker.name)
            for row in broker.portfolio_history())
        return {"backfilled": int(inserted)}

    def market(self, offline: bool) -> dict:
        """Compact, provenance-first daily-bar snapshot for terminal clients."""
        import numpy as np

        from qlab.core import data as market
        from qlab.core.moments import detect_regime
        from qlab.signals.hard import detect_regime_robust

        tickers = self.mandate.universe_whitelist
        snap = market.snapshot(
            tickers, date.today().isoformat(), lookback_days=252,
            offline=offline, seed=self.seed,
        )
        prices = snap.prices.dropna(how="any")
        returns = prices.pct_change(fill_method=None)
        last_dt = prices.index[-1].date()
        assets = []
        for ticker in tickers:
            series = prices[ticker]
            one_day = float(series.iloc[-1] / series.iloc[-2] - 1.0) if len(series) > 1 else 0.0
            twenty_day = (
                float(series.iloc[-1] / series.iloc[-21] - 1.0)
                if len(series) > 20 else 0.0
            )
            vol = float(returns[ticker].dropna().tail(63).std() * np.sqrt(252.0))
            assets.append({
                "ticker": ticker,
                "price": float(series.iloc[-1]),
                "change_1d": one_day,
                "change_20d": twenty_day,
                "realized_vol": vol,
                "history": [float(x) for x in series.tail(40)],
            })

        hmm_reading = None
        hmm_posterior = None
        if importlib.util.find_spec("hmmlearn") is not None:
            from qlab.signals.hmm import fit_regime_hmm

            cache_key = snap.content_hash()
            hmm_reading = self._regime_hmm_cache.get(cache_key)
            if hmm_reading is None:
                fitted = fit_regime_hmm(
                    snap.log_returns().dropna(how="any"),
                    n_states=3,
                    seed=self.seed,
                )
                latest = fitted["posteriors"].iloc[-1]
                labels = fitted["state_labels"]
                posterior = {
                    labels[int(state)]: float(probability)
                    for state, probability in latest.items()
                }
                hmm_reading = {
                    "posterior": posterior,
                    "label": max(posterior, key=posterior.get),
                }
                self._regime_hmm_cache = {cache_key: hmm_reading}
            hmm_posterior = hmm_reading["posterior"]

        robust = detect_regime_robust(
            snap,
            hmm_posterior=hmm_posterior,
        )
        regime = detect_regime(snap)
        regime.update({
            "robust_state": robust.regime,
            "confidence": robust.confidence,
            "effective_risk_fraction": robust.effective_risk_fraction,
        })
        if hmm_reading is not None:
            regime.update({
                "posterior": hmm_reading["posterior"],
                "hmm_label": hmm_reading["label"],
            })
        return {
            "source": snap.source,
            "as_of": last_dt.isoformat(),
            "bar_age_days": max(0, (date.today() - last_dt).days),
            "frequency": "daily",
            "regime": regime,
            "assets": assets,
        }

    def historical_replays(
        self,
        offline: bool,
        weights: dict[str, float],
    ) -> dict:
        """Replay current weights on the full cached point-in-time price panel."""
        from qlab.core import data as market
        from qlab.core.stress import replay_scenarios

        snap = market.snapshot(
            self.mandate.universe_whitelist,
            date.today().isoformat(),
            offline=offline,
            seed=self.seed,
        )
        snap.prices.attrs["source"] = snap.source
        snap.prices.attrs["synthetic"] = snap.source == "synthetic"
        return replay_scenarios(weights, snap.prices)

    def stress_payload(
        self,
        portfolio: dict,
        market_snapshot: dict,
        replays: dict,
        events: list[dict],
    ) -> dict:
        """Guardrail and scenario facts for the live dashboard stress tile."""
        from qlab.core.stress import stress_correlation_to_one

        raw_weights = portfolio.get("weights", {})
        if not isinstance(raw_weights, dict):
            raise ValueError("portfolio weights must be a mapping")
        weights = {
            str(ticker): float(weight)
            for ticker, weight in raw_weights.items()
        }
        vols = {
            str(asset["ticker"]): float(asset["realized_vol"])
            for asset in market_snapshot.get("assets", [])
            if isinstance(asset, dict)
            and "ticker" in asset
            and "realized_vol" in asset
        }
        gross = sum(abs(weight) for weight in weights.values())
        equity = float(portfolio["equity"])
        high_water_mark = float(portfolio.get("high_water_mark", equity))
        drawdown = (
            1.0 - equity / high_water_mark
            if high_water_mark > 0
            else 0.0
        )
        stressed_vol = stress_correlation_to_one(weights, vols)

        refusals = []
        for event in reversed(events):
            if event.get("kind") != "cost_gate_refusal":
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            reasons = payload.get("reasons")
            refusals.append({
                "ts": event.get("ts"),
                "plan_id": payload.get("plan_id"),
                "reasons": (
                    [str(reason) for reason in reasons]
                    if isinstance(reasons, list)
                    else []
                ),
            })
            if len(refusals) == 3:
                break

        tiers = self.mandate.drawdown_tiers
        return {
            "drawdown_tier": self.mandate.drawdown_tier(drawdown),
            "drawdown_thresholds": {
                "warning": tiers.warning,
                "control": tiers.control,
                "breaker": tiers.breaker,
            },
            "gross_exposure": gross,
            "max_gross_exposure": self.mandate.max_gross_exposure,
            "leverage_headroom": self.mandate.max_gross_exposure - gross,
            "stressed_vol": stressed_vol,
            "stress_vol_limit": self.mandate.stress_vol_limit,
            "replays": replays,
            "cost_gate_refusals": refusals,
        }

    def agents(self) -> list[dict]:
        """Agent definitions shaped for the persistent work rail."""
        from qlab.agents.loader import load_agents

        self.reap_stale_workflows()
        latest = self.registry.list_workflows(limit=1)
        step_states = {
            step["agent"]: step["status"]
            for step in (latest[0]["steps"] if latest else [])
        }
        rows = []
        for agent in load_agents():
            if agent.name not in _GATED_WORKFORCE_ROLES:
                continue
            authority = {
                "moments-analyst": "RESEARCH",
                "challenger": "CHALLENGE",
                "optimization-runner": "SOLVE",
                "referee": "VETO",
                "reporter": "PROPOSE",
            }.get(agent.name, "OBSERVE")
            rows.append({
                "name": agent.name,
                "description": agent.description,
                "authority": authority,
                "state": step_states.get(agent.name, "idle"),
                "tools": agent.tools,
            })
        return rows

    def system_status(self, offline: bool) -> dict:
        """Health and authority facts shown quietly at the bottom edge."""
        from qlab.core import data

        config_path = workspace_root() / ".mcp.json"
        servers: list[str] = []
        if config_path.exists():
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
                servers = sorted(config.get("mcpServers", {}))
            except Exception:
                servers = []
        proxy_available = importlib.util.find_spec("fastmcp") is not None
        # Cache-only provenance: never a network fetch from a status poll.
        provenance = data.cached_provenance(self.mandate.universe_whitelist)
        events = self.registry.read_events(500)
        last_daily_ops = next(
            (
                event
                for event in reversed(events)
                if event.get("kind") == "daily_ops"
            ),
            None,
        )
        triggers = (
            (last_daily_ops.get("payload") or {}).get("triggers", [])
            if last_daily_ops
            else []
        )
        autopilot = {
            "last_run_at": last_daily_ops.get("ts") if last_daily_ops else None,
            "triggers_fired": len(triggers) if isinstance(triggers, list) else 0,
        }
        return {
            "mode": "paper",
            "offline": offline,
            "claude_available": bool(shutil.which("claude")),
            "mcp_configured": bool(servers),
            "mcp_servers": servers,
            "mcp_proxy_available": proxy_available,
            "governed_available": proxy_available and bool(shutil.which("claude")),
            "governed_authority": "propose_only",
            "claude_role": "workforce_orchestrator",
            "workforce_available": proxy_available and bool(shutil.which("claude")),
            "governed_lock_reason": (
                "agent authority is intentionally propose-only; paper execution "
                "requires explicit human confirmation"
            ),
            "data_source": provenance[0] if provenance else "none",
            "data_age_days": provenance[1] if provenance else None,
            "autopilot": autopilot,
        }

    def data_health(self, offline: bool, purpose: str = "paper_proposal") -> dict:
        """Evaluate the active universe's data health and record a permit.

        Fails soft into a ``blocked`` report (not an exception) when an
        operational policy cannot obtain real data, so the TUI can surface the
        outage instead of the owner thread dying.
        """
        from qlab.core import data as market
        from qlab.data.health import evaluate_panel_health
        from qlab.data.permit import build_permit

        tickers = self.mandate.universe_whitelist
        policy = market.policy_for(offline, seed=self.seed)
        try:
            snap = market.snapshot(
                tickers, date.today().isoformat(), lookback_days=252,
                policy=policy, seed=self.seed)
        except market.DataUnavailable as exc:
            return {
                "blocked": True, "mode": policy.mode, "provider": policy.provider,
                "feed": policy.feed, "reason": str(exc),
                "eligible_for_paper_proposal": False,
                "eligible_for_execution": False,
            }
        quotes_fresh = (
            self.market_stream.quotes_fresh() if self.market_stream else None)
        health = evaluate_panel_health(
            snap.prices, policy, tickers=tickers, quotes_fresh=quotes_fresh)
        permit = build_permit(
            snapshot_id=snap.content_hash(), purpose=purpose, policy=policy,
            health=health, universe=tickers, as_of=str(snap.as_of))
        self.registry.record_data_permit(permit.to_dict())
        return {
            "blocked": False, "mode": policy.mode, "feed": policy.feed,
            "permit_id": permit.permit_id,
            "quote_health": (
                self.market_stream.health() if self.market_stream else None),
            **health.to_dict(),
        }

    def quotes(self, symbols: list[str] | None = None) -> dict:
        """Latest cached quotes and stream health, or a no-stream report."""
        stream = self.market_stream
        wanted = symbols or self.mandate.universe_whitelist
        if stream is None:
            return {"live_stream": False,
                    "reason": "no live market stream (demo/offline runtime)",
                    "quotes": {}, "health": None}
        snap = stream.snapshot()
        health = stream.health()
        ages = health.get("quote_ages", {})
        return {
            "live_stream": True,
            "feed": stream.feed,
            "quotes": {
                s: {"price": q.price, "age_seconds": ages.get(s)}
                for s, q in snap.items() if s in wanted
            },
            "health": health,
        }

    def data_permit_current(self, purpose: str = "paper_proposal") -> dict:
        """The most recently recorded data permit for ``purpose`` (or null)."""
        permit = self.registry.current_data_permit(purpose)
        return {"purpose": purpose, "permit": (permit or {}).get("permit")
                if permit else None}

    def regime_panel(self, offline: bool) -> dict:
        """All regime indicators read off ONE snapshot, with a fingerprint.

        A diagnostic of market state — never a trading signal, forecast, or
        weight recommendation.
        """
        from qlab.core import data as market
        from qlab.signals.panel import build_panel

        snap = market.snapshot(
            self.mandate.universe_whitelist, date.today().isoformat(),
            lookback_days=504, offline=offline, seed=self.seed)
        return build_panel(snap).to_dict()

    def similar_decisions(self, query: dict) -> dict:
        """Point-in-time recall of analogous reflected decisions."""
        def _one(key, default=None):
            values = query.get(key)
            return values[0] if isinstance(values, list) and values else default

        as_of = _one("as_of")
        fingerprint = {
            "vol_percentile": float(_one("vol_percentile", 0.5)),
            "turbulence_percentile": float(_one("turbulence_percentile", 0.5)),
            "regime_label": _one("regime_label", "calm"),
        }
        rows = self.registry.recall_similar_decisions(
            fingerprint, kind=_one("kind", "regime"),
            limit=int(_one("limit", 5)), as_of=as_of,
            min_similarity=float(_one("min_similarity", 0.5)))
        return {"as_of": as_of, "fingerprint": fingerprint, "decisions": rows}

    # -- Atlas desk manager -------------------------------------------
    def atlas_facts(self, offline: bool) -> dict:
        """Assemble the deterministic owner facts Atlas observes (no LLM)."""
        port = self.portfolio(offline)
        health = self.data_health(offline)
        weights = port.get("weights", {}) or {}
        targets = port.get("target_weights", {}) or {}
        drift = max(
            (abs(float(weights.get(t, 0.0)) - float(targets.get(t, 0.0)))
             for t in set(weights) | set(targets)),
            default=0.0)
        dd = float(port.get("drawdown", 0.0))
        orders = self.registry.list_orders(20)
        anomaly = any(o.get("state") in ("rejected", "expired") for o in orders)
        return {
            "universe": self.mandate.universe_whitelist,
            "data": {
                "provider": health.get("provider"),
                "blocked": bool(health.get("blocked")),
                "eligible_for_paper_proposal": bool(
                    health.get("eligible_for_paper_proposal")),
                "reason": health.get("reason"),
            },
            "portfolio": {
                "equity": port.get("equity"),
                "drawdown": round(dd, 4),
                "drawdown_tier": self.mandate.drawdown_tier(dd),
                "halted": bool(port.get("halted")),
                "gross_exposure": round(
                    sum(abs(float(w)) for w in weights.values()), 6),
                "drift": round(drift, 4),
            },
            "regime": {"robust_state": None, "flip": False},
            "open_workflows": len(self.registry.list_workflows(50)),
            "pending_approvals": 0,
            "order_anomaly": anomaly,
            # The grounded window the news-analyst would interpret. Present so
            # template preconditions can refuse an empty record rather than
            # letting the analyst narrate silence.
            "news_window_items": len(
                (self.desk_read(offline).get("grounding") or {})
                .get("hashes", [])),
        }

    def atlas_observe(self, offline: bool) -> dict:
        """Run one deterministic Atlas observe tick against current owner facts.

        Reconciliation runs first: a dispatched workflow may have reached a
        terminal state since the last tick (or while this process was down), and
        its task must be resolved from that state before new work is considered.
        """
        reconciled = self.atlas.reconcile_tasks()
        facts = self.atlas_facts(offline)
        observed = self.atlas.observe(facts, trading_date=date.today().isoformat())
        if reconciled:
            observed = {**observed, "reconciled_tasks": reconciled}
        return observed

    def desk_read(self, offline: bool, *, refresh: bool = False) -> dict:
        """Atlas's composed qualitative read across signals, news, and research.

        Never fetches. Every caller of this method — ``tui_snapshot``,
        ``atlas_facts``, ``/api/atlas/read``, ``/api/atlas/startable`` — reaches
        it while the owner holds its dispatch lock, and a cold cache used to
        turn that into six RSS timeouts with the whole desk queued behind them.
        News arrives only through ``fetch_desk_news``, which the heartbeat and
        the explicit refresh route call outside the lock; ``refresh`` here means
        recompose from the newest window already fetched, not go get one.
        """
        if not refresh and self._desk_read is not None:
            return self._desk_read
        return self.compose_desk_read(
            offline, prefetched_news=self.desk_news_window())

    def desk_news_window(self) -> dict:
        """The last window fetched outside the lock, or a loud stand-in.

        Before the first heartbeat tick lands there is no window at all. Saying
        so through the existing ``news_error`` channel keeps a not-yet-fetched
        desk distinguishable from a genuinely quiet one; returning an empty
        window silently would read as 'no news', which is a different claim.
        """
        if self._desk_news is not None:
            return self._desk_news
        return {
            "items": [],
            "provider_name": "synthetic",
            "error": "news window not fetched yet (owner is still starting up)",
        }

    def refresh_desk_read(self, offline: bool) -> dict:
        """Fetch a news window and recompose the read from it.

        Does network I/O; callers holding the owner dispatch lock must not use
        it. The owner's own paths (heartbeat, ``/api/atlas/read?refresh=1``)
        call ``fetch_desk_news`` then ``compose_desk_read`` around the lock
        instead. This stays as the single-shot entry point for surfaces that
        have no heartbeat behind them.
        """
        return self.compose_desk_read(
            offline,
            prefetched_news=self.fetch_desk_news(offline),
        )

    def fetch_desk_news(self, offline: bool) -> dict:
        """Fetch external news without touching the registry.

        The owner heartbeat calls this before taking its dispatch lock. Network
        latency therefore cannot stall TUI requests, while the subsequent
        grounding and registry reads remain serialized under the one-writer
        boundary. The window is cached so ``desk_read`` can compose under the
        lock without ever reaching the network itself.
        """
        from qlab.news.feed import fetch_news

        universe = self.mandate.universe_whitelist
        as_of = date.today().isoformat()
        provider_name = (
            "synthetic"
            if offline
            else os.environ.get("QLAB_NEWS_PROVIDER", "synthetic")
        )
        try:
            items = fetch_news(
                as_of,
                universe,
                lookback_hours=48,
                offline=offline,
            )
        except Exception as exc:
            window = {
                "items": [],
                "provider_name": provider_name,
                "error": str(exc),
            }
        else:
            window = {
                "items": items,
                "provider_name": provider_name,
                "error": None,
            }
        self._desk_news = window
        return window

    def compose_desk_read(
        self,
        offline: bool,
        *,
        prefetched_news: dict,
    ) -> dict:
        """Ground a fetched news window and compose it with owner state."""
        from qlab.operator.synthesis import compose_read, read_news

        universe = self.mandate.universe_whitelist
        as_of = date.today().isoformat()
        try:
            panel = self.regime_panel(offline)
        except Exception as exc:
            panel = {"robust_state": "unknown",
                     "uncertainty_reason": f"panel unavailable: {exc}"}
        items = list(prefetched_news.get("items") or [])
        news_error = prefetched_news.get("error")
        # Ground the window before interpreting it: enforce the point-in-time
        # boundary, hash each record so an edited headline is a new record
        # rather than a silent rewrite, and cluster so corroboration is visible.
        from qlab.news.grounding import ground

        provider_name = str(
            prefetched_news.get("provider_name") or "synthetic")
        grounded = ground(
            items, as_of=datetime.now(timezone.utc).isoformat(),
            provider=provider_name, universe=universe)
        news = read_news(grounded.items)
        portfolio = {"drawdown_tier": self.mandate.drawdown_tier(
            float(self.portfolio(offline).get("drawdown", 0.0)))}
        decisions = self.registry.recent_decisions(limit=10)
        verdicts = self.registry.verdicts_for(
            [d["decision_id"] for d in decisions])
        read = compose_read(
            as_of=as_of, panel=panel, news=news, portfolio=portfolio,
            recent_verdicts=[v for v in verdicts.values() if v])
        payload = read.to_dict()
        payload["news_source"] = (
            "synthetic (demo)" if offline else provider_name)
        payload["grounding"] = grounded.to_dict()
        # Claims a human should actually weigh: primary documents and
        # multi-publisher stories. Single secondary takes stay visible in the
        # full window but are not promoted as established.
        payload["supported_claims"] = [
            c.to_dict() for c in grounded.corroborated_claims[:6]]
        if news_error:
            payload["news_error"] = news_error[:400]
            payload["observations"] = [
                f"News feed is UNAVAILABLE ({news_error[:160]}); the "
                "qualitative side of this read is missing, not quiet.",
                *payload["observations"],
            ]
        self._desk_read = payload
        return self._desk_read

    def mark_desk_read_stale(self, error: str) -> None:
        """Record that the read could not be recomposed.

        The cached payload is a previous tick's window. Leaving it intact makes
        a failed refresh indistinguishable from a genuinely unchanged desk, and
        `atlas_facts` derives `news_window_items` from it — so a template
        precondition would admit a news read against evidence that is no longer
        current. Zero the grounding and say so, the same way a fetch outage
        does, so the refusal is the loud one rather than a stale number.
        """
        payload = dict(self._desk_read or {})
        payload["grounding"] = {"hashes": []}
        payload["news_error"] = str(error)[:400]
        payload["observations"] = [
            f"Desk read could not be recomposed ({str(error)[:160]}); the "
            "qualitative side of this read is STALE, not quiet.",
            *(payload.get("observations") or []),
        ]
        self._desk_read = payload

    def set_autonomy(self, enabled: bool) -> dict:
        """Turn autonomous work on or off at runtime.

        This does not widen authority: the mode still decides what may run, so
        enabling autonomy in Observe mode still launches nothing. It only
        removes the need for a human to press start on permitted work.
        """
        self.autonomous = bool(enabled)
        mode = self.atlas.status().get("mode", "observe")
        self.registry.record_event(
            "atlas_autonomy", {"enabled": self.autonomous, "mode": mode})
        return {
            "autonomous": self.autonomous,
            "mode": mode,
            "effect": (
                "Atlas will start work its mode permits on each heartbeat"
                if self.autonomous and mode in ("research", "propose")
                else f"enabled, but {mode!r} mode starts no workflows"
                if self.autonomous
                else "Atlas will queue work and wait for you to start it"),
        }

    def atlas_escalate_debate(self, offline: bool) -> dict:
        """Open a bounded debate when Atlas's read finds material disagreement.

        Atlas does not get a private argument channel: it opens the SAME
        registry-enforced debate the workforce uses, with an allowlisted claim,
        a two-round ceiling, and an adjudication the reporter waits on.
        """
        from qlab.governance.debate import DebateViolation, open_debate
        from qlab.operator.synthesis import should_open_debate

        read = self.desk_read(offline)
        should, claim = should_open_debate(read)
        if not should:
            return {"opened": False,
                    "reason": "no material disagreement in the current read"}
        decisions = self.registry.recent_decisions(limit=1)
        decision_id = decisions[0]["decision_id"] if decisions else "no-decision"
        try:
            debate_id = open_debate(
                self.registry, workflow_id=f"atlas-{read.get('read_hash')}",
                original_decision_id=decision_id, material_claims=[claim],
                panel_snapshot_id=(read.get("evidence_refs") or [None])[0])
        except DebateViolation as exc:
            return {"opened": False, "reason": str(exc)}
        self.registry.record_event(
            "atlas_opened_debate",
            {"debate_id": debate_id, "claim": claim,
             "tension": (read.get("tensions") or [""])[0][:200]})
        return {"opened": True, "debate_id": debate_id, "claim": claim,
                "tensions": read.get("tensions")}

    def atlas_workflow_runner(self, task: dict, template_id: str) -> dict:
        """Start the durable workforce run a Atlas task selected.

        This is the seam AtlasSupervisor.start_task calls once authority has
        already been checked. It only *starts* a governed workflow — the same
        one a human could start — and returns the handle. It grants nothing:
        the workflow's own phase gates, referee binding, and approval
        requirement are unchanged by having been started autonomously.
        """
        from qlab.operator.atlas import Dispatched
        from qlab.operator.templates import get_template

        template = get_template(template_id)
        if not template.needs_coordinator:
            # Deterministic templates (desk_brief) need no workforce at all, so
            # this genuinely is the conclusion.
            return {"template_id": template_id, "workflow_id": None,
                    "action_taken": False,
                    "brief": self.atlas.desk_brief(self.atlas_facts(True))}
        # The template's declared graph is what runs. Before this it was ignored
        # and every template silently got the standard portfolio graph, so the
        # declarations were decorative and four of them were unrunnable.
        started = self.start_workflow(
            {
                "kind": "portfolio_review",
                "goal": f"[{template_id}] {template.purpose} "
                        f"(trigger: {task.get('trigger_kind')})",
                "started_by": "atlas",
            },
            phases=template.phases or None,
        )
        workflow_id = (started or {}).get("workflow_id")
        if not workflow_id:
            # Returning a handle with workflow_id=None is how a failed dispatch
            # used to be recorded as a completed task. Refuse instead.
            raise RuntimeError(
                f"no workflow could be started for template {template_id!r}; "
                "the task cannot be dispatched")
        # A workflow row is not a finding. Report the dispatch and let
        # AtlasSupervisor.reconcile_tasks resolve the task from the workflow's
        # own terminal state.
        return Dispatched(
            workflow_id=str(workflow_id),
            detail={"template_id": template_id, "action_taken": True},
        )

    def atlas_run_startable(self, offline: bool, *, limit: int = 1) -> list[dict]:
        """Start the queued work Atlas's current mode already permits.

        Autonomy is a *convenience*, never an authority widening: every task
        still goes through ``start_task``, so mode checks, the retry budget,
        and the plan-creation boundary all apply unchanged. In Research mode
        this launches research and still cannot create a paper plan.
        """
        facts = self.atlas_facts(offline)
        started: list[dict] = []
        for candidate in self.atlas.startable_tasks(facts):
            if len(started) >= limit:
                break
            if not candidate.get("startable"):
                continue
            result = self.atlas.start_task(
                candidate["task_id"], facts, runner=self.atlas_workflow_runner)
            started.append({"task_id": candidate["task_id"],
                            "template_id": candidate.get("template_id"),
                            **{k: v for k, v in result.items()
                               if k in ("started", "completed", "blocked_by")}})
        return started

    def atlas_start_task(self, task_id: str, offline: bool) -> dict:
        """Start one queued Atlas task through the governed workflow runner."""
        facts = self.atlas_facts(offline)
        return self.atlas.start_task(task_id, facts,
                                   runner=self.atlas_workflow_runner)

    def atlas_message(self, body: dict) -> dict:
        """Accept a human question or explicit workflow request.

        This never grants authority. The message is recorded; a substantive
        answer needs the coordinator, so when Claude is absent Atlas acknowledges
        and reports itself degraded rather than fabricating an answer.
        """
        text = str(body.get("text") or "").strip()
        if not text:
            raise ValueError("message text is required")
        self.registry.record_event("atlas_message", {"text": text[:500]})
        available = bool(shutil.which("claude"))
        return {
            "received": True,
            "coordinator_available": available,
            "note": ("queued for the interpreting agent" if available
                     else "coordinator unavailable; Atlas is degraded and cannot "
                          "answer, but the owner, data, and book remain usable"),
        }

    # -- human approvals (the persisted execution gate) ---------------------
    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def create_approval(self, body: dict, offline: bool) -> dict:
        """Create a pending approval bound to an exact checked plan."""
        from qlab.governance.approval import book_revision, build_approval_request

        plan_id = str(body.get("plan_id") or "")
        plan = self.registry.get_plan(plan_id)
        if plan is None:
            raise KeyError(f"unknown plan_id {plan_id!r}")
        permit = self.registry.current_data_permit("execution")
        approval = build_approval_request(
            plan,
            broker=("alpaca_paper" if not offline else "simulated_paper"),
            data_permit_id=(permit or {}).get("permit_id"),
            current_book_revision=book_revision(self.registry.get_positions()),
            summary={"n_legs": (plan.get("pre_trade") or {}).get("n_legs"),
                     "turnover": (plan.get("pre_trade") or {}).get("turnover")},
            task_id=body.get("task_id"))
        self.registry.create_approval_request(approval)
        self.registry.record_event("approval_created",
                                   {"approval_id": approval["approval_id"],
                                    "plan_id": plan_id})
        return {"approval_id": approval["approval_id"], "status": "pending",
                "expires_at": approval["expires_at"]}

    def list_approvals(self, status: str | None = None) -> dict:
        self.registry.expire_due_approvals(self._now_iso())
        return {"approvals": self.registry.list_approval_requests(50, status)}

    def get_approval(self, approval_id: str) -> dict:
        self.registry.expire_due_approvals(self._now_iso())
        approval = self.registry.get_approval_request(approval_id)
        if approval is None:
            raise KeyError(f"unknown approval_id {approval_id!r}")
        return approval

    def challenge_approval(self, approval_id: str, body: dict) -> dict:
        import hashlib

        text = str(body.get("challenge") or "").strip()
        if not text:
            raise ValueError("challenge text is required")
        digest = hashlib.sha256(text.encode()).hexdigest()[:16]
        self.registry.transition_approval(
            approval_id, "pending", challenge_digest=digest)
        self.registry.record_event("approval_challenged",
                                   {"approval_id": approval_id})
        return {"approval_id": approval_id, "challenge_digest": digest}

    def decide_approval(self, approval_id: str, decision: str) -> dict:
        """Approve or reject a pending approval (the human decision)."""
        approval = self.registry.get_approval_request(approval_id)
        if approval is None:
            raise KeyError(f"unknown approval_id {approval_id!r}")
        if approval.get("status") != "pending":
            raise PermissionError(
                f"approval is {approval.get('status')!r}, not pending")
        status = {"approve": "approved", "reject": "rejected"}[decision]
        self.registry.transition_approval(
            approval_id, status, decided_at=self._now_iso())
        self.registry.record_event("approval_" + status,
                                   {"approval_id": approval_id})
        return {"approval_id": approval_id, "status": status}

    def execute_plan_with_approval(self, plan_id: str, body: dict,
                                   offline: bool) -> dict:
        """Governed execution: consume a persisted, matching human approval.

        This is the invariant-#14 path — a boolean cannot stand in for a human.
        The approval must be 'approved', unexpired, and still bind the exact
        plan, targets, and book; otherwise it is invalidated, not executed.
        """
        from qlab.governance.approval import book_revision, check_approval_for_execution
        from qlab.trader.broker import get_broker
        from qlab.trader.mandate import MandateViolation
        from qlab.trader.plan import OrderLeg, OrderPlan, execute_plan

        approval_id = str(body.get("approval_id") or "")
        self.registry.expire_due_approvals(self._now_iso())
        approval = self.registry.get_approval_request(approval_id)
        stored = self.registry.get_plan(plan_id)
        if stored is None:
            raise KeyError(f"unknown plan_id {plan_id!r}")

        reasons = check_approval_for_execution(
            approval or {}, stored,
            current_book_revision=book_revision(self.registry.get_positions()),
            now_iso=self._now_iso(),
            data_permit_id=(approval or {}).get("data_permit_id"))
        if reasons:
            if approval and approval.get("status") == "approved":
                self.registry.transition_approval(
                    approval_id, "invalidated",
                    invalidated_reason="; ".join(reasons))
            return {"executed": False, "blocked_by": "approval", "reasons": reasons}

        # Operational execution additionally revalidates data at submission.
        from qlab.core import data as market

        policy = market.policy_for(offline, seed=self.seed)
        if policy.execution_eligible:
            health = self.data_health(offline, purpose="execution")
            if health.get("blocked") or not health.get("eligible_for_execution"):
                return {"executed": False, "blocked_by": "data_revalidation",
                        "data_health": health}

        legs = [OrderLeg(**leg) for leg in (stored.get("legs") or [])]
        # A legacy or truncated row must be re-proposed, never executed with
        # whatever legs happen to have survived — a partial plan that fills is
        # indistinguishable from the plan the human actually approved.
        expected_legs = int((stored.get("pre_trade") or {}).get("n_legs", 0))
        if len(legs) != expected_legs:
            raise RuntimeError(
                f"plan {plan_id!r} has incomplete persisted legs; re-propose")
        plan = OrderPlan(plan_id=stored["plan_id"], decision_id=stored["decision_id"],
                         targets=stored["targets"], legs=legs,
                         pre_trade=stored["pre_trade"], state=stored["state"])
        broker = get_broker(self.registry, offline=offline,
                            starting_cash=self.mandate.paper_capital, seed=self.seed,
                            universe=self.mandate.universe_whitelist,
                            book=self.desk_mode.book)
        try:
            result = execute_plan(self.registry, broker, plan)
        except MandateViolation as exc:
            return {"executed": False, "mandate_violation": str(exc)}
        self.registry.transition_approval(
            approval_id, "consumed", consumed_at=self._now_iso())
        self.registry.record_event("approval_consumed",
                                   {"approval_id": approval_id, "plan_id": plan_id})
        return {"executed": True, "approval_id": approval_id, **result}

    def allocation_policy(self) -> dict:
        from qlab.algorithms import get_operational_policy

        policy = get_operational_policy(self.mandate.operational_policy).to_dict()
        policy["constraints"] = {
            "long_only": self.mandate.long_only,
            "budget": 1.0,
            "min_weight": self.mandate.min_weight_per_asset,
            "max_weight": self.mandate.max_weight_per_asset,
        }
        return policy

    def latest_equilibrium_returns(self) -> dict | None:
        """Compact summary of the newest persisted equilibrium research run."""
        for run in self.registry.list_runs(1000):
            if run.get("kind") != "equilibrium":
                continue
            spec = run.get("spec")
            if not isinstance(spec, dict):
                continue
            portfolio = spec.get("portfolio")
            if not isinstance(portfolio, dict):
                continue
            return {
                "run_id": run.get("run_id"),
                "as_of": spec.get("as_of"),
                "portfolio": portfolio,
                "caveats": spec.get("caveats"),
            }
        return None

    def latest_ablation_metrics(self) -> dict[str, dict]:
        """Per-arm metrics from the newest staged ablation run.

        Only ``kind == "ablation"`` runs are comparable: ``backtest.run`` and
        ``research.apply_views`` also write the backtests table, one raw arm id
        at a time, and a newer one of those must never displace the ablation.
        ``list_runs`` is already newest-first, and filtering before ``report``
        keeps the TUI poll path off an N+1 scan of unrelated research history.
        """
        from qlab.experiment import ABLATION_RUN_KIND

        for run in self.registry.list_runs(200):
            if run.get("kind") != ABLATION_RUN_KIND:
                continue
            backtests = self.registry.report(run["run_id"]).get("backtests") or []
            if backtests:
                return {bt["arm_id"]: bt["metrics"] for bt in backtests}
        return {}

    def reference(self) -> dict:
        """The curated catalog with live facts overlaid, never stored in prose."""
        from dataclasses import asdict

        from qlab.algorithms import list_algorithms
        from qlab.core.reference import REFERENCE_ENTRIES

        catalog = {row["id"]: row for row in list_algorithms()}
        champion = self.mandate.operational_policy
        ablation = self.latest_ablation_metrics()
        entries = []
        for entry in REFERENCE_ENTRIES:
            row = asdict(entry)
            spec = catalog.get(entry.algorithm_key) if entry.algorithm_key else None
            row["stage"] = spec["stage"] if spec else None
            row["champion"] = bool(
                entry.group == "arm" and entry.algorithm_key == champion)
            # Absent ablation evidence stays absent — never a zero.
            row["ablation"] = (
                ablation.get(entry.arm_id) if entry.arm_id else None)
            entries.append(row)
        return {"entries": entries, "champion_policy": champion}

    def leaderboard(self) -> list[dict]:
        """Newest ablation ranked by Sharpe, in operator-readable method names."""
        from qlab.algorithms import list_algorithms
        from qlab.core.reference import (
            OVERLAY_METRICS,
            arm_algorithm_key,
            arm_display_name,
        )

        catalog = {row["id"]: row for row in list_algorithms()}
        champion = self.mandate.operational_policy
        rows = []
        for arm_id, metrics in self.latest_ablation_metrics().items():
            key = arm_algorithm_key(arm_id)
            spec = catalog.get(key) if key else None
            rows.append({
                "arm_id": arm_id,
                "name": arm_display_name(arm_id),
                "champion": bool(key == champion),
                "benchmark": bool(spec and spec["category"] == "benchmark"),
                **{name: metrics.get(name) for name in OVERLAY_METRICS},
            })
        # Arms without a Sharpe sort last instead of claiming a rank.
        rows.sort(key=lambda row: -(
            row["sharpe"] if row["sharpe"] is not None else float("-inf")))
        return rows

    def tui_snapshot(self, offline: bool, event_limit: int = 100) -> dict:
        """One consistent payload for a complete TUI refresh."""
        from qlab.algorithms import list_algorithms

        # One poll-sourced mark an hour keeps intraday granularity while the TUI
        # is open without turning the 2s refresh into 43k rows a day.
        # The throttle is advanced BEFORE the write on purpose: a failing mark is
        # then not retried on the next 2s tick (it waits an hour and self-heals),
        # and the write reads the broker exactly as self.portfolio(offline) does
        # on the next line — so leaving it unguarded adds no new failure class.
        # Do not "fix" this ordering into write-then-advance.
        if time.time() - self._last_poll_mark > 3600.0:
            self._last_poll_mark = time.time()
            self.record_equity_mark("poll", offline)
        portfolio = self.portfolio(offline)
        market_snapshot = self.market(offline)
        events = self.read_audit_stream_events(event_limit, after=None)
        plans = self.registry.list_plans(20)
        decisions = self.registry.recent_decisions(limit=30)
        verdicts = self.registry.verdicts_for(
            [decision["decision_id"] for decision in decisions])
        for decision in decisions:
            decision["verdict"] = verdicts.get(decision["decision_id"])
        stress = {}
        if "drawdown" in portfolio and isinstance(market_snapshot.get("assets"), list):
            raw_weights = portfolio.get("weights", {})
            if not isinstance(raw_weights, dict):
                raise ValueError("portfolio weights must be a mapping")
            replays = self.historical_replays(offline, raw_weights)
            stress = self.stress_payload(
                portfolio,
                market_snapshot,
                replays,
                events,
            )
        self.registry.expire_due_approvals(self._now_iso())
        return {
            "desk_mode": self.desk_mode_payload(),
            "portfolio": portfolio,
            "live_portfolio": self.live_portfolio(offline),
            "market": market_snapshot,
            "stress": stress,
            "atlas": self.atlas.status(),
            "atlas_read": self.desk_read(offline),
            "atlas_heartbeat": {
                **(self.heartbeat.status() if self.heartbeat
                   else {"running": False, "ticks": 0}),
                "autonomous": self.autonomous,
            },
            "atlas_tasks": self.registry.list_atlas_tasks(10),
            "approvals": self.registry.list_approval_requests(10, "pending"),
            "quotes": self.quotes(),
            "agents": self.agents(),
            "decisions": decisions,
            "runs": self.registry.list_runs(30),
            "plans": plans,
            "orders": self.registry.list_orders(50),
            "events": events,
            "system": self.system_status(offline),
            "algorithms": list_algorithms(),
            "policy": self.allocation_policy(),
            "equilibrium_returns": self.latest_equilibrium_returns(),
            "leaderboard": self.leaderboard(),
            "performance": self.performance(offline),
            "workflows": self.registry.list_workflows(10),
        }

    def read_market_events(
        self,
        limit: int = 100,
        after: str | None = None,
    ) -> list[dict]:
        """Read transient market events with the registry bus cursor contract."""
        limit = max(1, min(int(limit), _MARKET_EVENT_LIMIT))
        with self._market_lock:
            events = list(self._market_events)
        if after:
            return [
                event for event in events
                if str(event.get("ts") or "") > after
            ][:limit]
        return events[-limit:]

    def read_audit_stream_events(
        self,
        limit: int,
        after: str | None,
    ) -> list[dict]:
        """Read a bounded audit page in the merged stream's stable ordering."""
        limit = max(1, min(int(limit), _STREAM_PAGE_CEILING))
        # Registry.read_events caps ordinary observers at 500 and orders only
        # by timestamp. The owner needs the full tuple order to page one dense
        # timestamp without adding another registry connection or writer.
        if after:
            return self.registry._rows(
                "SELECT * FROM events WHERE ts > ? "
                "ORDER BY ts ASC, event_id ASC LIMIT ?",
                [after, limit],
            )
        return self.registry._rows(
            "SELECT * FROM ("
            "SELECT * FROM events ORDER BY ts DESC, event_id DESC LIMIT ?"
            ") ORDER BY ts ASC, event_id ASC",
            [limit],
        )


def _publish_quote_event(session: UISession) -> dict | None:
    """Recompute compact quotes and publish only a changed transient snapshot."""
    snapshot = session.market(session.offline_default)
    rows = [
        {
            "ticker": str(asset["ticker"]),
            "price": float(asset["price"]),
            "change_1d": float(asset["change_1d"]),
        }
        for asset in snapshot["assets"]
    ]
    if not rows:
        raise RuntimeError("market quote topic produced no rows")
    signature = tuple(
        (row["ticker"], row["price"], row["change_1d"]) for row in rows
    )
    with session._market_lock:
        if signature == session._last_quote_signature:
            return None
        event = {
            "event_id": str(uuid.uuid4()),
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": "quote",
            "payload": {"rows": rows},
        }
        session._last_quote_signature = signature
        session._market_events.append(event)
        return event


def _run_market_topics(
    session: UISession,
    stop_event: threading.Event,
    refresh_seconds: float = _QUOTE_REFRESH_TTL_SECONDS,
) -> None:
    """Run the owner's quote policy without ever touching the registry."""
    interval = max(_QUOTE_MIN_INTERVAL_SECONDS, float(refresh_seconds))
    while not stop_event.is_set():
        try:
            _publish_quote_event(session)
        except Exception as exc:
            print(f"[qlab] quote topic failed: {exc!r}", flush=True)
        stop_event.wait(interval)


def _start_market_topics(
    session: UISession,
    refresh_seconds: float = _QUOTE_REFRESH_TTL_SECONDS,
) -> tuple[threading.Event, threading.Thread]:
    """Start the one process-wide transient market producer."""
    global _ACTIVE_MARKET_THREAD

    with _MARKET_THREAD_LOCK:
        if (
            _ACTIVE_MARKET_THREAD is not None
            and _ACTIVE_MARKET_THREAD.is_alive()
        ):
            raise RuntimeError("market topic producer is already running")
        stop_event = threading.Event()
        producer = threading.Thread(
            target=_run_market_topics,
            args=(session, stop_event, refresh_seconds),
            daemon=True,
            name=_MARKET_THREAD_NAME,
        )
        _ACTIVE_MARKET_THREAD = producer
        try:
            producer.start()
        except Exception:
            _ACTIVE_MARKET_THREAD = None
            raise
    return stop_event, producer


def _stop_market_topics(
    stop_event: threading.Event,
    producer: threading.Thread,
    *,
    timeout: float = _MARKET_THREAD_JOIN_TIMEOUT_SECONDS,
) -> None:
    """Signal and join a producer before another owner may start one."""
    global _ACTIVE_MARKET_THREAD

    stop_event.set()
    producer.join(timeout=max(0.0, float(timeout)))
    if producer.is_alive():
        raise RuntimeError(
            f"market topic producer did not stop within {float(timeout):g}s"
        )
    with _MARKET_THREAD_LOCK:
        if _ACTIVE_MARKET_THREAD is producer:
            _ACTIVE_MARKET_THREAD = None


def _overlap_stream_cursor(cursor: str) -> str:
    """Return a cursor just before the boundary for same-ts deduplication."""
    try:
        parsed = datetime.fromisoformat(cursor.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return cursor
    return (parsed - timedelta(microseconds=1)).isoformat()


def _observed_cadence(daily) -> dict | None:
    """Annualization basis derived from the marks that actually exist.

    The equity marks are not a trading calendar: a Fri→Mon step and a Tue→Wed
    step are both one observation, and marks appear on weekends whenever the TUI
    was open. Annualizing that at 252 periods a year is an unstated, unstable
    time-base error, so the factor here is the observed number of return
    observations per year across the observed span. It is returned for the
    payload as well as used, because the convention must never be implicit.
    """
    steps = len(daily) - 1
    if steps < 1:
        return None
    span_days = (daily.index[-1] - daily.index[0]).total_seconds() / 86400.0
    if span_days <= 0.0:
        return None
    return {
        "periods_per_year": round(_DAYS_PER_YEAR * steps / span_days, 4),
        "observed_span_days": round(span_days, 4),
        "mean_step_days": round(span_days / steps, 4),
        "basis": "observed mark cadence",
    }


def _offline_for_book(session: UISession, off: bool) -> bool:
    """The data lane can never contradict the book.

    ``off`` is the request body's operator-supplied flag, and the bundled web
    dashboard's checkbox re-defaults it to True on every page load — honouring
    it verbatim on an Alpaca-book desk could reconstruct the synthetic+alpaca
    pairing ``DeskMode.__post_init__`` forbids. The real book always runs on
    the desk mode's own data lane instead.
    """
    if session.desk_mode.book == "alpaca":
        return session.desk_mode.offline
    return off


def _mark_after_mutation(session: UISession, source: str, offline: bool) -> None:
    """Mark the book after a mutation; a failed mark must never hide the result.

    ``record_equity_mark`` re-reads the broker, so it can fail (a network hiccup)
    right after real legs filled. The trade already happened: refusing the
    response would hide reality from the operator, so the failure fails loud into
    the audit bus instead. Only the post-mutation hooks are guarded — the backfill
    route still surfaces its RuntimeError to the client as a 400.
    """
    try:
        session.record_equity_mark(source, offline)
    except Exception as exc:  # the mutation's own result must still reach the client
        session.registry.record_event(
            "equity_mark_failed", {"source": source, "error": repr(exc)})


# ---------------------------------------------------------------------------
# API dispatch (pure functions of the session; easy to unit-test)
# ---------------------------------------------------------------------------
def handle_api(session: UISession, method: str, path: str,
               query: dict, body: dict) -> tuple[int, dict]:
    # bool("0") is True, so a flag arriving as text has to be parsed, never cast.
    off = _flagbool(body.get("offline"),
                    _qbool(query, "offline", session.offline_default))

    if method == "GET" and path == "/api/bootstrap":
        return 200, _bootstrap(session)

    if method == "GET" and path == "/api/portfolio":
        return 200, session.portfolio(_qbool(query, "offline", session.offline_default))

    if method == "GET" and path == "/api/portfolio/live":
        return 200, session.live_portfolio(
            _qbool(query, "offline", session.offline_default))

    if method == "GET" and path == "/api/market":
        return 200, session.market(_qbool(query, "offline", session.offline_default))

    if method == "GET" and path == "/api/agents":
        return 200, {"agents": session.agents()}

    if method == "GET" and path == "/api/algorithms":
        from qlab.algorithms import list_algorithms

        category = query.get("category", [None])[0]
        stage = query.get("stage", [None])[0]
        if stage not in (None, "operational", "research", "offline"):
            return 400, {"error": "stage must be operational, research, or offline"}
        rows = list_algorithms(category=category, stage=stage)
        return 200, {"algorithms": rows, "count": len(rows)}

    if method == "GET" and path == "/api/policy":
        return 200, session.allocation_policy()

    if method == "GET" and path == "/api/reference":
        return 200, session.reference()

    if method == "GET" and path == "/api/performance":
        return 200, session.performance(
            _qbool(query, "offline", session.offline_default))

    if method == "GET" and path == "/api/workflows":
        session.reap_stale_workflows()
        limit = max(1, min(int(query.get("limit", ["10"])[0]), 50))
        return 200, {"workflows": session.registry.list_workflows(limit)}

    # The suffix route must precede the generic one, which would otherwise
    # match "<id>/debate" as a workflow id and 404.
    if (method == "GET" and path.startswith("/api/workflows/")
            and path.endswith("/debate")):
        workflow_id = path.removeprefix("/api/workflows/").removesuffix("/debate")
        debates = session.registry.list_debates(workflow_id)
        for debate in debates:
            debate["turns"] = session.registry.list_debate_turns(
                debate["debate_id"])
        return 200, {"workflow_id": workflow_id, "debates": debates}

    if method == "GET" and path.startswith("/api/workflows/"):
        workflow_id = path.removeprefix("/api/workflows/")
        workflow = session.registry.get_workflow(workflow_id)
        if workflow is None:
            return 404, {"error": f"unknown workflow_id {workflow_id!r}"}
        return 200, workflow

    if method == "GET" and path == "/api/system":
        offline = _qbool(query, "offline", session.offline_default)
        return 200, session.system_status(offline)

    if method == "GET" and path == "/api/desk_mode":
        return 200, session.desk_mode_payload()

    if method == "POST" and path == "/api/desk_mode":
        try:
            mode = DeskMode(str(body.get("data")), str(body.get("book")))
        except ValueError as exc:
            return 400, {"error": str(exc)}
        session.set_desk_mode(mode)
        return 200, session.desk_mode_payload()

    if method == "GET" and path == "/api/data/health":
        offline = _qbool(query, "offline", session.offline_default)
        purpose = query.get("purpose", ["paper_proposal"])[0]
        return 200, session.data_health(offline, purpose)

    if method == "GET" and path == "/api/data/permit/current":
        purpose = query.get("purpose", ["paper_proposal"])[0]
        return 200, session.data_permit_current(purpose)

    if method == "GET" and path == "/api/quotes":
        raw = query.get("symbols", [None])[0]
        symbols = [s.strip() for s in raw.split(",")] if raw else None
        return 200, session.quotes(symbols)

    if method == "GET" and path == "/api/regime/panel":
        offline = _qbool(query, "offline", session.offline_default)
        return 200, session.regime_panel(offline)

    if method == "GET" and path == "/api/decisions/similar":
        return 200, session.similar_decisions(query)

    if (method == "GET" and path.startswith("/api/decisions/")
            and path.endswith("/outcome")):
        decision_id = path.removeprefix("/api/decisions/").removesuffix("/outcome")
        decision = session.registry.get_decision(decision_id)
        if decision is None:
            return 404, {"error": f"unknown decision_id {decision_id!r}"}
        return 200, {"decision_id": decision_id,
                     "outcome": decision.get("realized_outcome"),
                     "reflection": decision.get("reflection")}

    if (method == "GET" and path.startswith("/api/decisions/")
            and path.endswith("/lesson")):
        decision_id = path.removeprefix("/api/decisions/").removesuffix("/lesson")
        lesson = session.registry.get_lesson(decision_id)
        return 200, {"decision_id": decision_id,
                     "lesson": (lesson or {}).get("lesson") if lesson else None,
                     "stale": bool((lesson or {}).get("stale")) if lesson else None}

    if method == "GET" and path == "/api/models/invocations":
        return 200, {"invocations": session.registry.list_model_invocations(50)}

    if method == "GET" and path == "/api/atlas/status":
        status = session.atlas.status()
        status["heartbeat"] = (
            session.heartbeat.status() if session.heartbeat else
            {"running": False, "ticks": 0})
        return 200, status

    if method == "GET" and path == "/api/atlas/read":
        # refresh=1 recomposes here; the HTTP handler intercepts that case
        # first and does the news fetch outside the dispatch lock, because
        # nothing reached from handle_api may block on the network.
        offline = _qbool(query, "offline", session.offline_default)
        refresh = _qbool(query, "refresh", False)
        return 200, session.desk_read(offline, refresh=refresh)

    if method == "GET" and path == "/api/atlas/tasks":
        return 200, {"tasks": session.registry.list_atlas_tasks(50)}

    if method == "GET" and path == "/api/atlas/templates":
        from qlab.operator.templates import TEMPLATES

        return 200, {"templates": [t.to_dict() for t in TEMPLATES.values()]}

    if method == "GET" and path == "/api/atlas/shadow":
        from qlab.operator.shadow import shadow_scorecard

        return 200, shadow_scorecard(
            session.registry, since=query.get("since", [None])[0])

    if method == "GET" and path == "/api/atlas/startable":
        offline = _qbool(query, "offline", session.offline_default)
        facts = session.atlas_facts(offline)
        return 200, {"startable": session.atlas.startable_tasks(facts)}

    if method == "POST" and path == "/api/atlas/observe":
        return 200, session.atlas_observe(off)

    if method == "POST" and path == "/api/atlas/mode":
        mode = str(body.get("mode") or "")
        try:
            return 200, session.atlas.set_mode(mode)
        except ValueError as exc:
            return 400, {"error": str(exc)}

    if method == "POST" and path == "/api/atlas/pause":
        return 200, session.atlas.pause()

    if method == "POST" and path == "/api/atlas/resume":
        return 200, session.atlas.resume(str(body.get("mode") or "observe"))

    if (method == "POST" and path.startswith("/api/atlas/tasks/")
            and path.endswith("/start")):
        task_id = path.removeprefix("/api/atlas/tasks/").removesuffix("/start")
        try:
            return 200, session.atlas_start_task(task_id, off)
        except KeyError as exc:
            return 404, {"error": str(exc)}
        except PermissionError as exc:
            return 400, {"error": str(exc)}

    if method == "POST" and path == "/api/atlas/autonomy":
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            return 400, {"error": "enabled must be true or false"}
        return 200, session.set_autonomy(enabled)

    if method == "POST" and path == "/api/atlas/escalate":
        return 200, session.atlas_escalate_debate(off)

    if method == "POST" and path == "/api/atlas/message":
        try:
            return 200, session.atlas_message(body)
        except ValueError as exc:
            return 400, {"error": str(exc)}

    if method == "POST" and path == "/api/approvals":
        try:
            return 200, session.create_approval(body, off)
        except KeyError as exc:
            return 404, {"error": str(exc)}
        except ValueError as exc:
            return 400, {"error": str(exc)}

    if method == "GET" and path == "/api/approvals":
        return 200, session.list_approvals(query.get("status", [None])[0])

    if method == "POST" and path.startswith("/api/approvals/"):
        rest = path.removeprefix("/api/approvals/")
        approval_id, _, action = rest.partition("/")
        try:
            if action == "challenge":
                return 200, session.challenge_approval(approval_id, body)
            if action in ("approve", "reject"):
                return 200, session.decide_approval(approval_id, action)
        except KeyError as exc:
            return 404, {"error": str(exc)}
        except (ValueError, PermissionError) as exc:
            return 400, {"error": str(exc)}
        return 404, {"error": f"unknown approval action {action!r}"}

    if method == "GET" and path.startswith("/api/approvals/"):
        approval_id = path.removeprefix("/api/approvals/")
        try:
            return 200, session.get_approval(approval_id)
        except KeyError as exc:
            return 404, {"error": str(exc)}

    if (method == "POST" and path.startswith("/api/plans/")
            and path.endswith("/execute") and path != "/api/plans/execute"):
        plan_id = path.removeprefix("/api/plans/").removesuffix("/execute")
        try:
            return 200, session.execute_plan_with_approval(plan_id, body, off)
        except KeyError as exc:
            return 404, {"error": str(exc)}

    if method == "GET" and path == "/api/events":
        limit = int(query.get("limit", ["100"])[0])
        after = query.get("after", [None])[0]
        return 200, {"events": session.registry.read_events(limit, after)}

    if method == "GET" and path == "/api/plans":
        return 200, {"plans": session.registry.list_plans(20)}

    if method == "GET" and path == "/api/orders":
        return 200, {"orders": session.registry.list_orders(50)}

    if method == "GET" and path == "/api/tui":
        offline = _qbool(query, "offline", session.offline_default)
        limit = int(query.get("event_limit", ["100"])[0])
        return 200, session.tui_snapshot(offline, limit)

    if method == "GET" and path == "/api/runs":
        return 200, {"runs": session.registry.list_runs(20)}

    if method == "GET" and path == "/api/decisions":
        return 200, {"decisions": session.registry.recent_decisions(limit=20)}

    if method == "POST" and path == "/api/recommend":
        from qlab.experiment import recommend

        rec = recommend(
            as_of=body.get("as_of") or None, universe=body.get("universe", "core"),
            skew_lambda=float(body.get("skew", 0.5)),
            kurt_lambda=float(body.get("kurt", 0.5)),
            offline=off, seed=session.seed,
            policy_id=session.mandate.operational_policy)
        return 200, rec

    if method == "POST" and path.startswith("/api/lab/"):
        name = path.removeprefix("/api/lab/")
        return 200, {"result": session.call_lab_tool(name, body, off)}

    if method == "POST" and path == "/api/workflows/start":
        try:
            return 200, session.start_workflow(body)
        except ValueError as exc:
            return 400, {"error": str(exc)}

    if method == "POST" and path == "/api/rebalance_preview":
        return 200, session.rebalance_preview(body, off)

    if method == "POST" and path == "/api/plans/execute":
        # Retained as the TUI's entry point, but it is now the same governed
        # path as /api/plans/<id>/execute: a bare human_confirmed flag is
        # self-attestation any local process can send, so the approval record
        # — not the boolean — is what authorises a fill.
        clamped_off = _offline_for_book(session, off)
        if body.get("human_confirmed") is not True:
            return 400, {"error": "human_confirmed=true is required"}
        if not str(body.get("approval_id") or ""):
            # Distinct from an approval that fails to cover the plan (200 with
            # reasons): presenting no approval at all is a malformed request,
            # and saying so plainly keeps a caller from reading the refusal as
            # "the desk declined this trade".
            return 400, {"error": "execution requires an approval_id: a bare "
                                  "human_confirmed flag cannot book a trade"}
        try:
            result = session.execute_plan_with_approval(
                str(body.get("plan_id") or ""), body, clamped_off)
        except KeyError as exc:
            return 404, {"error": str(exc)}
        # A mark sourced "execution" asserts that legs filled; a refused or
        # mandate-violating plan must not forge that provenance.
        if result.get("executed") is True:
            _mark_after_mutation(session, "execution", clamped_off)
        return 200, result

    if method == "POST" and path == "/api/performance/backfill":
        try:
            return 200, session.backfill_equity_history(off)
        except RuntimeError as exc:
            return 400, {"error": str(exc)}

    if method == "POST" and path.startswith("/api/workflows/"):
        rest = path.removeprefix("/api/workflows/")
        workflow_id, separator, action = rest.rpartition("/")
        if separator and action in {"interrupt", "resume", "abandon"}:
            try:
                return 200, session.control_workflow(
                    workflow_id, action, body)
            except KeyError as exc:
                return 404, {"error": str(exc)}
            except RuntimeError as exc:
                return 409, {"error": str(exc)}
            except ValueError as exc:
                return 400, {"error": str(exc)}

    if method == "POST" and path.startswith("/api/workflows/"):
        phase = path.removeprefix("/api/workflows/")
        try:
            return 200, session.update_workflow(phase, body)
        except KeyError as exc:
            return 404, {"error": str(exc)}
        except RuntimeError as exc:
            return 409, {"error": str(exc)}
        except ValueError as exc:
            return 400, {"error": str(exc)}

    if method == "POST" and path == "/api/run_once":
        from qlab.autopilot.loop import run_once

        summary = run_once(
            registry=session.registry, mandate=session.mandate,
            offline=_offline_for_book(session, off),
            # Parsed, never cast: bool("false") is True, so a client that
            # stringifies booleans would ask for a dry run and get a live one.
            execute=_flagbool(body.get("execute"), True),
            skew_lambda=float(body.get("skew", 0.5)),
            kurt_lambda=float(body.get("kurt", 0.5)),
            as_of=body.get("as_of") or None, seed=session.seed,
            book=session.desk_mode.book)
        return 200, summary

    if method == "POST" and path == "/api/daily_ops":
        from qlab.autopilot.loop import daily_ops

        clamped_off = _offline_for_book(session, off)
        summary = daily_ops(registry=session.registry, mandate=session.mandate,
                            offline=clamped_off, seed=session.seed,
                            book=session.desk_mode.book)
        # The same lane the heartbeat ran on: one route, one answer.
        _mark_after_mutation(session, "daily", clamped_off)
        return 200, summary

    if method == "POST" and path == "/api/batch":
        from qlab.experiment import run_ablation

        spec = body.get("spec") or _default_ui_spec()
        report = run_ablation(spec, registry=session.registry, offline=off)
        return 200, report

    if method == "POST" and path == "/api/reset":
        # A reset discards qlab's own book. It cannot discard an Alpaca
        # account, so on that desk it would only delete the local marks and
        # leave the recorded history disagreeing with the real account —
        # refuse rather than manufacture that gap.
        if session.desk_mode.book == "alpaca":
            return 400, {
                "error": "the Alpaca book cannot be reset from here: this "
                         "would erase the local history of an account it "
                         "cannot touch. Switch to the simulated book first."}
        state = session.portfolio(off)
        session.registry.reset_book(session.mandate.paper_capital,
                                    book=state["broker"])
        return 200, {"reset": True, "cash": session.mandate.paper_capital,
                     "book": state["broker"]}

    return 404, {"error": f"no route for {method} {path}"}


def _bootstrap(session: UISession) -> dict:
    from qlab.algorithms import list_algorithms
    from qlab.agents.loader import load_agents
    from qlab.core.universe import load_universe
    from qlab.solvers.base import available_solvers

    uni = load_universe()
    agents = [{"name": a.name, "description": a.description,
               "servers": sorted(a.server_scopes), "n_tools": len(a.tools),
               "tools": a.tools} for a in load_agents()
              if a.name in _GATED_WORKFORCE_ROLES]
    m = session.mandate
    return {
        "today": date.today().isoformat(),
        "offline_default": session.offline_default,
        "universe": {
            "core": [{"ticker": a.ticker, "name": a.name, "asset_class": a.asset_class}
                     for a in uni.core],
            "candidates": uni.candidates, "selection_k": uni.selection_k,
        },
        "mandate": {
            "paper_capital": m.paper_capital, "whitelist": m.universe_whitelist,
            "max_weight_per_asset": m.max_weight_per_asset,
            "max_turnover_per_rebalance": m.max_turnover_per_rebalance,
            "max_gross_exposure": m.max_gross_exposure,
            "stress_vol_limit": m.stress_vol_limit,
            "drawdown_tiers": {
                "warning": m.drawdown_tiers.warning,
                "control": m.drawdown_tiers.control,
                "breaker": m.drawdown_tiers.breaker,
            },
            "trailing_drawdown_pct": m.trailing_drawdown_pct,
            "cadence": m.cadence, "order_type": m.order_type,
            "operational_policy": m.operational_policy,
        },
        "agents": agents,
        "algorithms": list_algorithms(),
        "solvers": available_solvers(),
        "portfolio": session.portfolio(session.offline_default),
    }


def _default_ui_spec() -> dict:
    """A compact staged ablation for the UI (short window, key arms)."""
    return {
        "name": "ui_quick", "seed": 7,
        "data": {"universe": "core", "start": "2016-01-01", "end": "2022-12-31"},
        "backtest": {"rebalance": "quarterly", "lookback_days": 504, "cost_bps": 5},
        "moments": {"shrinkage": "ledoit_wolf", "denoise": "marchenko_pastur",
                    "comoment_shrinkage": 0.5},
        "arms": [
            {"id": "B1", "objective": "equal_weight", "solver": "none"},
            {"id": "B2", "objective": "hrp", "solver": "hrp"},
            {"id": "B3", "objective": "risk_parity", "solver": "risk_parity"},
            {"id": "A1", "objective": "min_variance", "solver": "classical"},
            {"id": "A2", "objective": "scenario_cvar", "solver": "cvar_lp"},
            {"id": "A3", "objective": "mvsk", "solver": "classical_multistart",
             "params": {"skew_lambda": 0.5, "kurt_lambda": 0.5}},
        ],
    }


def _flagbool(value: object, default: bool) -> bool:
    """Coerce one flag value; text is parsed, because bool("0") is True."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _qbool(query: dict, key: str, default: bool) -> bool:
    v = query.get(key)
    if not v:
        return default
    return _flagbool(v[0], default)


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    session: UISession = None  # type: ignore[assignment]
    stream_page_cap = _MARKET_EVENT_LIMIT
    stream_page_ceiling = _STREAM_PAGE_CEILING
    protocol_version = "HTTP/1.1"          # keep-alive; Content-Length is always sent

    def log_message(self, *args):  # keep the console clean
        pass

    def _send(self, status: int, body: bytes, ctype: str):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj: dict):
        payload = json.dumps(_jsonable(obj), default=str).encode("utf-8")
        self._send(status, payload, "application/json")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send(200, _INDEX.read_bytes(), "text/html; charset=utf-8")
            return
        if parsed.path == "/readyz":
            self._json(200, {"ready": True})
            return
        if parsed.path == "/api/stream":
            self._stream(parse_qs(parsed.query))
            return
        if parsed.path.startswith("/api/"):
            query = parse_qs(parsed.query)
            try:
                if (
                    parsed.path == "/api/atlas/read"
                    and _qbool(query, "refresh", False)
                ):
                    offline = _qbool(
                        query, "offline", self.session.offline_default)
                    prefetched_news = self.session.fetch_desk_news(offline)
                    with _LOCK:
                        obj = self.session.compose_desk_read(
                            offline,
                            prefetched_news=prefetched_news,
                        )
                    status = 200
                else:
                    with _LOCK:
                        status, obj = handle_api(
                            self.session, "GET", parsed.path, query, {})
            except Exception as exc:  # never crash the server on a bad call
                status, obj = 500, {"error": repr(exc)}
            self._json(status, obj)
            return
        self._send(404, b"not found", "text/plain")

    def _stream(self, query: dict) -> None:
        """Server-sent events from the durable audit and transient market buses.

        Each connection runs on its own ThreadingHTTPServer thread and takes
        the dispatch lock only for the brief per-poll registry read, so a
        live stream never starves normal API calls. ``after`` resumes from an
        ISO timestamp cursor. Supplying ``after_id`` with it resumes strictly
        after that ``(ts, event_id)`` pair. Once connected, the cursor also
        retains event ids delivered at its timestamp boundary. ``kind`` filters
        events; a comment heartbeat is emitted while idle so clients can detect
        a dead socket.
        """
        import time

        cursor = (query.get("after") or [None])[0]
        after_id = (query.get("after_id") or [None])[0] if cursor else None
        boundary_event_ids = {str(after_id)} if after_id is not None else set()
        boundary_floor_id = str(after_id) if after_id is not None else None
        tracks_boundary = cursor is None or boundary_floor_id is not None
        kind = (query.get("kind") or [None])[0]
        page_cap = max(
            1,
            min(int(self.stream_page_cap), _MARKET_EVENT_LIMIT),
        )
        page_ceiling = max(
            page_cap,
            min(int(self.stream_page_ceiling), _STREAM_PAGE_CEILING),
        )
        overflow_logged = False
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        idle_polls = 0
        try:
            while True:
                # A fresh connection gets a short primer backlog, not history.
                limit = 200 if cursor else 25
                read_limit = min(page_cap, limit)
                read_after = cursor
                if cursor and tracks_boundary:
                    read_after = _overlap_stream_cursor(cursor)
                    read_limit = min(
                        page_cap,
                        limit + len(boundary_event_ids),
                    )

                while True:
                    # Waiting indefinitely here is what turns one long owner
                    # action into reconnect churn: no ping is emitted while the
                    # lock is held, the client's read deadline expires, and the
                    # replacement connection blocks on the same lock while this
                    # thread survives to write to a dead socket. Bound the wait
                    # and prove liveness instead.
                    if not _LOCK.acquire(timeout=_STREAM_LOCK_WAIT_SECONDS):
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    try:
                        audit_events = self.session.read_audit_stream_events(
                            read_limit, read_after)
                    finally:
                        _LOCK.release()
                    market_events = self.session.read_market_events(
                        read_limit, read_after)

                    def is_pending_boundary_event(event: dict) -> bool:
                        if not cursor:
                            return True
                        event_ts = str(event.get("ts") or "")
                        event_id = str(event.get("event_id") or "")
                        if event_ts > cursor:
                            return True
                        if event_ts != cursor or not tracks_boundary:
                            return False
                        if (
                            boundary_floor_id is not None
                            and event_id <= boundary_floor_id
                        ):
                            return False
                        return event_id not in boundary_event_ids

                    saturated_boundary = bool(cursor and tracks_boundary) and any(
                        len(source) >= read_limit
                        and all(
                            str(event.get("ts") or "") == cursor
                            and not is_pending_boundary_event(event)
                            for event in source
                        )
                        for source in (audit_events, market_events)
                    )
                    if not saturated_boundary:
                        break
                    if read_limit < page_cap:
                        read_limit = min(page_cap, read_limit * 2)
                        continue
                    if not overflow_logged:
                        print(
                            "[qlab] WARNING stream boundary page full at "
                            f"{cursor}; expanding fetch up to {page_ceiling}",
                            flush=True,
                        )
                        overflow_logged = True
                    if read_limit >= page_ceiling:
                        return
                    read_limit = min(page_ceiling, read_limit * 2)

                events = sorted(
                    [*audit_events, *market_events],
                    key=lambda event: (
                        str(event.get("ts") or ""),
                        str(event.get("event_id") or ""),
                    ),
                )
                # Cursor reads page the merged timeline so a newer quote cannot
                # advance past an audit row waiting in the next registry page.
                if cursor:
                    events = [
                        event
                        for event in events
                        if is_pending_boundary_event(event)
                    ]
                    events = events[:limit]
                if events:
                    idle_polls = 0
                    for event in events:
                        event_ts = str(event.get("ts") or cursor or "")
                        event_id = str(event.get("event_id") or "")
                        if cursor is None or event_ts > cursor:
                            cursor = event_ts
                            boundary_event_ids.clear()
                            boundary_floor_id = None
                            tracks_boundary = True
                        if event_ts == cursor:
                            boundary_event_ids.add(event_id)
                        if kind and event.get("kind") != kind:
                            continue
                        payload = json.dumps(
                            _jsonable(event), default=str, sort_keys=True)
                        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                else:
                    idle_polls += 1
                    if idle_polls % 20 == 0:  # ~10 s of silence
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            # The declared length is unusable, so the body cannot be consumed
            # and this connection can no longer be framed.
            self.close_connection = True
            self._json(400, {"error": "Content-Length must be an integer"})
            return
        raw = self.rfile.read(length) if length > 0 else b"{}"
        # A body the owner failed to parse must never be replaced by {}. The
        # route defaults are permissive on purpose -- /api/run_once reads
        # execute=True out of an empty body -- so substituting {} turns a
        # truncated request into an unrequested paper trade, and every other
        # POST into a 200 computed from parameters the caller never sent.
        try:
            body = json.loads(raw or b"{}")
        except (ValueError, UnicodeDecodeError) as exc:
            self._json(400, {"error": f"request body is not valid JSON: {exc}"})
            return
        if not isinstance(body, dict):
            self._json(400, {"error": "request body must be a JSON object"})
            return
        try:
            with _LOCK:
                status, obj = handle_api(self.session, "POST", parsed.path,
                                         parse_qs(parsed.query), body)
        except Exception as exc:
            status, obj = 500, {"error": repr(exc)}
        self._json(status, obj)


def _startup_banner(mode: DeskMode, url: str) -> str:
    """The owner's one startup line — deliberately pure ASCII.

    ``qlab tui`` spawns this process with ``stdout=DEVNULL``, so CPython encodes
    here with the locale ANSI codepage under ``errors='strict'``, not UTF-8. The
    chip's ``label`` carries U+00B7, which cp932 and cp874 cannot encode — and
    this line runs after the port bind and before ``serve_forever()``, so an
    encode error would take the owner down on every launch in those locales.
    The mode's own words are ASCII and say more than the decorated label anyway.
    """
    return (f"[qlab] UI at {url}  "
            f"(offline={'on' if mode.offline else 'off'}; "
            f"data={mode.data} book={mode.book}; paper capital only)")


def _start_atlas_heartbeat(session: UISession, *, offline: bool,
                         interval_s: float | None = None):
    """Start the desk manager's heartbeat inside the owner process.

    Ticks under the owner's dispatch lock, so Atlas observing never races a
    request — the one-writer rule is preserved.
    """
    from qlab.operator.heartbeat import AtlasHeartbeat, build_owner_tick

    seconds = float(os.environ.get("QLAB_ATLAS_INTERVAL_S", interval_s or 30.0))
    # Autonomy is opt-in and does not widen authority: the mode still decides
    # what may run, so QLAB_ATLAS_AUTONOMOUS=1 in Observe mode still launches
    # nothing.
    autonomous = os.environ.get("QLAB_ATLAS_AUTONOMOUS") == "1"
    heartbeat = AtlasHeartbeat(
        build_owner_tick(session, _LOCK, offline=offline,
                         autonomous=autonomous),
        interval_s=seconds,
        on_error=lambda exc: print(f"[qlab] atlas heartbeat: {exc!r}", flush=True),
    )
    session.heartbeat = heartbeat
    heartbeat.start()
    return heartbeat


def serve(port: int = 8765, *, offline: bool = True, open_browser: bool = True,
          desk_mode: DeskMode | None = None) -> None:
    """Start the UI server (blocking). Ctrl-C to stop.

    ``desk_mode=None`` means no launcher flag chose one, so the session loads
    the operator's persisted choice rather than being handed a guess — and
    ``offline`` only seeds a desk that has never been chosen. The mode the
    session settles on is what the banner reports.
    """
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    except Exception:
        # Resolve ownership before opening DuckDB or recovering workflows. A
        # second process that cannot bind this port must remain a pure refusal,
        # never a transient second writer that interrupts the real owner's run.
        raise
    try:
        session = UISession(offline_default=offline, desk_mode=desk_mode)
        market_stop, market_thread = _start_market_topics(session)
        _start_atlas_heartbeat(session, offline=offline)
    except Exception:
        httpd.server_close()
        raise
    httpd.daemon_threads = True
    _Handler.session = session
    url = f"http://127.0.0.1:{port}/"
    print(_startup_banner(session.desk_mode, url))
    print("[qlab] press Ctrl-C to stop.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[qlab] UI stopped.")
    finally:
        try:
            if session.heartbeat is not None:
                session.heartbeat.stop()
        finally:
            try:
                _stop_market_topics(market_stop, market_thread)
            finally:
                httpd.server_close()
