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
POST /api/desk/posture         arm the desk, or put it back to read-only
GET  /api/desk/method          the operational method + holdings cap, and the choices
POST /api/desk/method          choose the operational method and/or the cap
POST /api/alpaca/credentials   store an Alpaca paper login (never switches book)
POST /api/alpaca/test          ask the venue whether the stored login works
GET  /api/llm/backends         model backends, availability, and what they serve
POST /api/llm                  point a surface at a model, or switch the reasoner
GET  /api/data/health          data-policy provenance, freshness, eligibility
GET  /api/data/permit/current  the latest recorded data permit for a purpose
GET  /api/quotes               latest cached quotes + live market-stream health
GET  /api/regime/panel         all regime indicators on one snapshot (diagnostic)
GET  /api/decisions/similar    point-in-time recall of analogous decisions
GET  /api/decisions/<id>/outcome   the immutable resolved outcome
GET  /api/decisions/<id>/lesson    advisory lesson over that outcome (if any)
GET  /api/workflows/<id>/debate    debates, turns, and adjudication
GET  /api/debates                  open debates across every workflow
POST /api/debates/<id>/adjudicate  close a debate (human act; no agent may)
GET  /api/models/invocations   model tier/route audit records
GET  /api/atlas/status           Atlas mode, lifecycle state, heartbeat
GET  /api/news                   the news window, its coverage, and provenance
GET  /api/atlas/context          the rich surface a reasoning Atlas forms a view from
GET  /api/research/predictors    the predictor board: augmented lane vs its control
GET  /api/workforce              recent runs, how far each got, and where it stopped
GET  /api/workforce/stream       what the agents said and did, off the audit bus
GET  /api/atlas/read             Atlas's composed read: signals + news + research
POST /api/atlas/escalate         open a bounded debate on a material disagreement
GET  /api/atlas/tasks            Atlas's deduplicated autonomous task history
GET  /api/atlas/templates        the registered workflow templates Atlas may start
GET  /api/atlas/startable        queued tasks Atlas may start now, with refusals
POST /api/atlas/actionables      what Atlas would do next: the ranked menu with
                                 the gate's verdict on each, every offer
                                 persisted as a proposal you can approve
GET  /api/atlas/shadow           shadow-rollout scorecard (evidence, not a grant)
POST /api/atlas/tasks/<id>/start start one queued task's registered template
POST /api/atlas/observe          run one deterministic Atlas observe tick
POST /api/atlas/mode             set Atlas mode (observe|research|propose|paused)
POST /api/atlas/pause            pause Atlas's autonomous work
POST /api/atlas/resume           resume Atlas into a mode
POST /api/atlas/message          ask Atlas a question; the configured reasoner
                                 answers on the bus (never grants authority)
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
GET  /api/desk/proposal        the single checked plan the desk is asking about
POST /api/desk/proposal/book   approve and execute that one proposal, once
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
import threading
import time
import uuid
from collections import deque
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from qlab.core.desk_mode import (
    DEFAULT_DESK_MODE, DeskMode, load_desk_mode, save_desk_mode)
from qlab.core.posture import (
    DEFAULT_POSTURE, Posture, load_posture, save_posture)
from qlab.core.llm_config import SurfaceModel, save_llm_config, startup_llm_config
from qlab.core.types import _jsonable
from qlab.paths import workspace_root

# Workflow statuses where a run stopped and a human decision is what unblocks
# it. `abandoned` is deliberately absent: that run also stopped, but the
# operator already made the call, so counting it as outstanding work would
# report a decision back to the person who made it.
_AWAITING_OPERATOR = frozenset({"blocked", "failed", "interrupted"})

# A ThreadingHTTPServer keeps the browser's parallel/keep-alive connections
# responsive, but the shared DuckDB connection is not thread-safe, so every
# dispatch runs under this lock.
# Effectively one request computes at a time (fine for a local single user),
# while the socket layer never stalls.
# The payload a handler returns is serialized AFTER the lock is released, so
# anything reachable from one must be treated as replace-never-mutate: editing
# a dict in place is a write racing that serialization, with no lock over it.
_LOCK = threading.Lock()
# Operational policies that fund every whitelisted name by construction, so any
# cap below the size of the universe refuses every plan they produce. The desk
# still accepts the cap — an operator may be setting it before the method — but
# it says what will happen. Minimum variance can hold fewer names, so it is not
# here and carries no such warning.
_FULL_UNIVERSE_POLICIES = frozenset({"hrp", "risk_parity"})
# How long a stream poll waits for the dispatch lock before proving the socket
# is alive instead. Must stay comfortably under the client's stream read
# deadline, or a long owner action expires the client before it hears anything.
_STREAM_LOCK_WAIT_SECONDS = 2.0
# How long a broker valuation may be reused on a real venue. The TUI polls
# every two seconds and carries the valuation in that payload, so without this
# an idle Alpaca desk makes one or two API calls a second for a book that only
# changes when this desk trades. Any mutation drops the cache, so a fill shows
# up at once rather than up to this long later.
_VALUATION_TTL_SECONDS = 15.0
# The archive summary and the three run summaries share one staleness budget:
# a poll is 30s apart, so this is "at most one rebuild per poll" and never a
# stale answer, because a logged run invalidates them regardless.
_RESEARCH_TTL_SECONDS = 30.0
# How long a backend availability probe may be reused. Probing Ollama costs a
# round trip per backend, and a picker that re-reads on every keystroke (or a
# client that asked on every /api/tui poll) would turn an idle settings panel
# into a steady load on the daemon. Five seconds is short enough that an
# `ollama pull` finishing is visible almost at once, and long enough that a
# burst of clicks costs one probe.
_LLM_CATALOG_TTL_SECONDS = 5.0
# The chat surface's budget, env-tunable and generous by default: the operator
# asked for unabridged answers, and 8000 tokens is effectively unbounded for a
# chat reply while still being a number the completion protocols require. The
# real guard on a runaway answer stays `_ATLAS_REPLY_TIMEOUT_S`, which bounds
# the worker thread a human is waiting on.
_ATLAS_REPLY_TOKENS = int(os.environ.get("QLAB_ATLAS_REPLY_TOKENS", "8000"))
# And its ceiling. Far below the backends' own batch defaults (300s for Ollama,
# 600s for the CLI) because a human is watching this one, and the thread it
# holds is an owner worker.
_ATLAS_REPLY_TIMEOUT_S = 60.0
# What a single bus row may carry of an answer. `max_tokens` bounds a backend
# that honours it; the claude CLI has no such flag, so the ceiling is enforced
# here too — an unbounded model row rides the SSE bus to every client.
_ATLAS_REPLY_CHARS = 4000
# How many template judgments one observe tick will pay for. Triggers are
# already deduplicated before they get here, so this bites only when several
# distinct conditions appear at once — and a tick that turned into a batch of
# model calls would stall the desk's own loop for their sum. The rest fall back
# to the table, which is exactly why the table is still here.
_REASONER_MAX_PER_TICK = 2

# The desk manager's role, in qlab's own voice. This is the *judgment* half of
# planning-docs/2026-07-31-atlas-as-llm.md:28-47 stated to the model, and the
# refusals it names are not instructions the model is trusted to follow — they
# are code it cannot reach. `check_startable`, the workflow budget, the
# referee's targets_hash binding and human confirmation on every fill refuse it
# whether or not it has read this. Saying so anyway is what keeps a reply
# arguing inside the desk's authority instead of proposing work that will
# simply be refused.
_ATLAS_DESK_MANAGER_PROMPT = """\
You are Atlas, the desk manager of qlab — a governed, single-operator quant \
research desk. You are answering your operator directly.

You decide: what is worth investigating and why now rather than later; which \
registered workflow template fits the situation; how the qualitative record \
and the quantitative panel relate, especially where they disagree; what the \
operator needs told, and when silence is the right answer.

You never execute. You hold no order path, no approval, and no way to create a \
plan. The mode gate, the daily workflow budget, the referee's hash binding and \
the human confirmation on every fill are deterministic code and refuse you as \
written. Propose only work the `startable` list already shows as startable; \
when nothing is, say so and say what would have to change.

Answer in conclusions, not process. No preamble, no restating the question, no \
reading the context back. Cite the numbers you actually used — a level, a \
percentile, a headline — and name the ones that disagree with you. If the \
context does not support an answer, say what is missing; an invented reading \
is worse than none. Keep it to what fits on a desk card."""

_MARKET_EVENT_LIMIT = 500
# How many approvals of *each* actionable status ride in one /api/tui payload.
# Per status, not shared — see `UISession.actionable_approvals`.
_SNAPSHOT_APPROVALS = 10
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
# How long a workflow may go without phase progress before the desk stops
# calling it live. Far longer than the reap above, and a different claim: the
# reap says "this coordinator is gone, the run is resumable", this says "a week
# passed and nobody resumed it". The row is marked, never deleted.
_WORKFLOW_IDLE_STALE_DAYS = 7
# How many parked workflows one sweep may ASK to drive. A refusal is per-graph
# (a daemon-backed one-role read can be refused while a claude review would
# start), so the sweep must look past one — but a desk-wide refusal, like no
# `claude` on PATH, refuses every candidate, and without a cap that writes one
# skip row per running task per beat, forever.
_DRIVE_ATTEMPTS_PER_SWEEP = 3

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
# "Audit rows" is not the same as inert: a verdict or decision row a proxy
# session logs is read later by `rebalance_preview`'s gates. What keeps a role
# from writing the row that clears its own work is the per-role agent
# allowlists, not this set — this one only bounds what the proxy can reach.
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
    "research.qualitative_matrix",
    "moments.condition",
    "research.equilibrium_returns",
    "research.predict_vol",
    "research.predictor_board",
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
        # The mandate is loaded once here and read by every plan check, so the
        # desk's method/cap change is a read-modify-write of the override file
        # plus a swap of this attribute. Its own lock, not `_LOCK`: the route
        # already runs under the dispatch lock and `_LOCK` is not reentrant —
        # but the heartbeat also reads `self.mandate`, so the swap still needs
        # one (invariant 9).
        self._mandate_lock = threading.Lock()
        # The operator's explicit choice; the persisted value is authoritative
        # when the caller passes none, and ``offline_default`` only seeds the
        # mode nobody has chosen yet — never a second opinion about it.
        persisted = load_desk_mode()
        self.desk_mode = desk_mode or persisted or (
            DEFAULT_DESK_MODE if offline_default else DeskMode("live", "simulated"))
        # Whether anything *named* that pair, computed here because here is
        # where the answer stops being visible: past this line a fallback and a
        # deliberate ``synthetic · simulated`` are the same object. It means
        # "came from a launcher flag or the state file", never "there is a
        # file" — a flag-chosen desk is not persisted, and asking again about a
        # desk the operator just named on the command line would be the same
        # false question the flag exists to retire.
        self.desk_mode_chosen = bool(desk_mode or persisted)
        # Posture is never seeded from a launcher flag: the operator says it or
        # nobody has. ``None`` here is "not chosen yet", which reads as
        # read-only without claiming anyone chose read-only.
        self._posture_lock = threading.Lock()
        self._posture: Posture | None = load_posture()
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
        # (a real Alpaca feed); it stays None for demo/offline runtimes. The
        # transport is injected by serve(): a bare session — every test, every
        # tool — can flip desk modes without ever opening a websocket.
        self.market_stream = None
        self.market_stream_reason = "no live market stream (demo/offline runtime)"
        self._stream_runner = None
        self._stream_stop: threading.Event | None = None
        self._stream_lock = threading.Lock()
        self._last_poll_mark = 0.0
        self._last_workflow_reap = 0.0
        # Atlas's composed qualitative read, refreshed by the heartbeat.
        self._desk_read: dict | None = None
        # The last news window, written only by fetch_desk_news — which runs
        # outside the owner dispatch lock. desk_read composes from this and
        # never fetches, so a cold cache cannot stall the lock on RSS timeouts.
        self._desk_news: dict | None = None
        # Guards publication of the window above, and the grounded form of it
        # below. Two fetchers legitimately run concurrently — a heartbeat tick
        # and an operator refresh — and both write here from outside the
        # dispatch lock.
        self._news_lock = threading.Lock()
        # (window key, GroundedNews) for the window above. Grounding hashes,
        # windows and clusters every record; the read and the matrix are two
        # views of ONE window, and re-deriving the identical result per call
        # put that work on the dispatch lock twice a poll.
        self._grounded_news: tuple[tuple, object] | None = None
        # Previous robust regime, for flip detection. In memory only: after a
        # restart there is no prior observation, and claiming a flip without
        # one would launch a workflow off a cold start.
        self._last_robust_state: str | None = None
        # (key, monotonic stamp, payload) for the display valuation.
        self._valuation_cache: tuple[tuple[bool, str], float, dict] | None = None
        self.heartbeat = None
        # Autonomy is a runtime switch the operator owns from the UI.
        # The env var only seeds its initial value.
        # On by default, and switchable off. Autonomy off meant Atlas queued
        # tasks and waited for a human to press start, so the desk did nothing
        # unattended — which is not what a personal quant is for. It stays
        # bounded by the mode (Research creates no plans), the daily workflow
        # budget, and the approval requirement on every execution.
        self.autonomous = os.environ.get("QLAB_ATLAS_AUTONOMOUS", "1") != "0"
        # The port serve() actually bound. A driven coordinator talks back to
        # this owner over HTTP, so a wrong port means it opens the registry as a
        # second writer instead — the one thing the architecture forbids.
        self.port = int(os.environ.get("QLAB_UI_PORT", "8765") or 8765)
        # Seeded from the same env var ClaudeSession reads, so one switch
        # governs both an owner-driven coordinator and a hand-started one.
        self.fast_mode = os.environ.get("QLAB_LLM_FAST", "0") == "1"
        # Which model answers for which surface. The persisted choice wins over
        # the environment, which only seeds a desk that has never chosen.
        self.llm_config = startup_llm_config()
        # (monotonic stamp, payload) for the backend availability probe. The
        # owner is threaded and the heartbeat is a second writer of shared
        # state, so this cache takes a lock like every other mutable field here
        # (invariant 9) — an unguarded one hands two callers two readings and
        # makes "when was this probed" unanswerable.
        self._llm_catalog: tuple[float, dict] | None = None
        self._llm_catalog_lock = threading.Lock()
        self._driver = None
        # The owner is a ThreadingHTTPServer and the heartbeat is another
        # thread. Without this, a lazy build races and hands out one driver per
        # caller — each with its own lock and its own session slot, which is
        # exactly the "one coordinator at a time" guarantee gone.
        self._driver_lock = threading.Lock()
        # Which queued tasks have already had their "waiting on the slot" line
        # said, per running workflow. Process-local chatter control, never a
        # record — see `_announce_queued_task` (invariant 9: it is written from
        # the heartbeat thread and read from handler threads).
        self._queued_notice: set[tuple[str, str]] = set()
        self._queued_notice_lock = threading.Lock()
        # Threaded owner: the TTL cache is read from handler threads and
        # invalidated from the heartbeat thread (invariant 9).
        self._archive_lock = threading.Lock()
        self._archive_stats_cache = None
        # (monotonic stamp, run revision, payload) per summary. These three are
        # recomposed on every /api/tui poll under the dispatch lock and each
        # one scans hundreds of run rows. The registry's run revision makes the
        # cache exact against new RUNS; a backtest row written under an
        # existing run does not move it, so the ablation metrics are
        # TTL-bounded (<= 30s) rather than exact. Same locking reason
        # as the archive cache above.
        self._research_lock = threading.Lock()
        self._research_cache: dict[str, tuple[float, int, object]] = {}
        # The qualitative matrix is logged once per news window, and "has this
        # window already been logged" is a check followed by a write. The owner
        # is threaded (invariant 9): without this, a handler thread and the
        # heartbeat can both pass the check and log the same window twice.
        self._matrix_lock = threading.Lock()
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

        from qlab.tui.claude import resolve_claude_executable

        # The resolver, not a bare which(): on Windows the extensionless npm
        # shim resolves but does not run (WinError 193), so a bare which()
        # would report a coordinator the dispatcher then cannot start.
        self.atlas = AtlasSupervisor(
            self.registry,
            coordinator_available=lambda: bool(resolve_claude_executable()))
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
        # The domain gate, before any budget is charged: a goal that names an
        # email, a poem or a plumber is refused here with the sentence that
        # says what a research goal is made of, not forty seconds in by an
        # analyst whose tools cannot serve it. ValueError reaches the route
        # as the 400 every other refusal on this surface uses.
        from qlab.governance.goal_guard import check_goal

        goal = check_goal(
            str(body.get("goal") or "Prepare a governed portfolio review."),
            self.mandate.universe_whitelist,
        )
        request = {
            "goal": goal[:4000],
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

    def _stamped_template(self, workflow: dict) -> str:
        """The template id a workflow's goal was stamped with, or "".

        `atlas_workflow_runner` and `start_template_workflow` both write
        `[template_id] …` as the goal, and that stamp is the only place a
        registered template survives on the workflow row. One reader, because
        two would disagree the first time the stamp moved — the 409 path names
        the running run from this, and the resume gate below authorises from it.
        """
        goal = str((workflow.get("request") or {}).get("goal") or "")
        if goal.startswith("[") and "]" in goal:
            return goal[1:goal.index("]")].strip()
        return ""

    def _check_resumable_under_mode(self, workflow_id: str) -> None:
        """Refuse, by name, to resume a plan-creating run below Propose.

        Resuming is starting, for every purpose the mode gate cares about: a
        `desk_rebalance_review` interrupted in Propose still ends at a checked
        plan, and walking it to that plan under Research would create one in a
        mode that may not. `check_authority` is the same gate `start_task` and
        `start_template_workflow` ask, asked here about the template the run
        was stamped with — so there is one rule about plans, not two.

        A run with no stamp is not refused. Every template-started run carries
        one; an unstamped run is a human's own workflow, and its graph cannot
        be inferred from a goal string. The plan it might reach is still
        refused where it matters — a plan needs a referee PASS and an approval,
        neither of which this gate is standing in for.
        """
        from qlab.operator.templates import (
            TEMPLATES, TemplateNotAllowed, check_authority)

        workflow = self.registry.get_workflow(workflow_id)
        if workflow is None:
            raise KeyError(f"unknown workflow_id {workflow_id!r}")
        template_id = self._stamped_template(workflow)
        if not template_id or template_id not in TEMPLATES:
            return
        if not TEMPLATES[template_id].creates_plan:
            # Only the plan boundary is enforced here. Refusing to resume a
            # research run because the desk sits in Observe would strand work
            # the operator started, and Observe is a claim about what Atlas
            # starts unattended, not about what a human may pick back up.
            return
        try:
            check_authority(template_id, self.atlas.mode)
        except TemplateNotAllowed as exc:
            raise TemplateNotAllowed(
                f"{exc}; workflow {workflow_id} cannot be resumed") from exc

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
            self._check_resumable_under_mode(workflow_id)
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

        Cached briefly on a real venue. The TUI polls `/api/tui` every two
        seconds and that payload carries this valuation, so on the Alpaca book
        an idle desk was making one or two broker calls a second — positions
        and account, forever, for a book that only changes when this desk
        trades. The cache is invalidated on every mutation, so a fill is still
        reflected immediately; quote-level freshness arrives on the market
        stream and does not come through here.

        `portfolio()` is deliberately NOT cached: execution and equity marks
        read it, and those must see the venue's current truth.
        """
        cached = self._valuation_cache
        if cached is not None:
            key, stamped, payload = cached
            if key == (bool(offline), self.desk_mode.book) and (
                    time.monotonic() - stamped) < _VALUATION_TTL_SECONDS:
                return payload
        payload = self._compute_live_portfolio(offline)
        # Only a real venue needs this; the simulator is local and free, and
        # caching it would only add a way for the demo to look stale.
        if self.desk_mode.book == "alpaca":
            self._valuation_cache = (
                (bool(offline), self.desk_mode.book), time.monotonic(), payload)
        return payload

    def invalidate_valuation(self) -> None:
        """Drop the cached valuation — call after anything that moves the book."""
        self._valuation_cache = None

    def _compute_live_portfolio(self, offline: bool) -> dict:
        """Uncached mark-to-market. See :meth:`live_portfolio`.

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

        policy = self.data_policy(offline)
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
        # Somebody answered. Set here rather than derived from the file, because
        # the three-way ``or`` above runs once at construction and no POST goes
        # near it: without this line a desk that was asked and answered would go
        # on reporting that nobody had, and every client keyed on that would ask
        # again on the next run.
        self.desk_mode_chosen = True
        # The mode owns the data lane too: the TUI retunes an owner that was
        # spawned with no flags, so leaving these behind would keep publishing
        # synthetic quotes and pricing a real book off the synthetic feed.
        self.offline_default = mode.offline
        self.lab_state.offline = mode.offline
        # A different book is a different set of positions; the cache key
        # covers that, but dropping it here means the switch is visible on the
        # very next poll rather than after the TTL.
        self.invalidate_valuation()
        save_desk_mode(mode)
        self.retune_market_stream()
        return mode

    # -- posture -------------------------------------------------------------
    def posture_payload(self) -> dict:
        """Armed or not, and whether anyone has said so.

        The two facts are separate for the same reason ``chosen`` exists on the
        desk mode: an unasked desk and a deliberately read-only desk serve the
        same ``armed``, and a client that must ask the question needs to tell
        them apart.
        """
        with self._posture_lock:
            posture = self._posture
        chosen = posture is not None
        # Both booleans, always: a client reads an absent block as an owner too
        # old to serve one, and that is a different fact from "nobody chose".
        return {"armed": (posture or DEFAULT_POSTURE).armed, "chosen": chosen}

    def set_posture(self, armed: bool) -> dict:
        """Record the operator's choice. Persists before memory changes.

        A failed write must not leave the running owner believing it is armed
        when nothing on disk says so — the next start would silently disarm.
        The disk write is inside the lock for invariant 9: this runtime is
        threaded, and two concurrent POSTs whose disk and memory writes
        interleaved would leave the file and ``self._posture`` disagreeing.
        """
        posture = Posture(bool(armed))
        with self._posture_lock:
            save_posture(posture)
            self._posture = posture
        self.registry.record_event("desk.posture_chosen", {"armed": posture.armed})
        return self.posture_payload()

    # -- the method and the cap ----------------------------------------------
    def _cap_warning(self, policy_id: str, cap: int | None) -> str | None:
        """What a cap will do to the plans the chosen method produces.

        A warning, never a refusal (G3 ruling): the operator may legitimately
        set the cap first and the method second, and a desk that refuses the
        first half of a two-step change is a desk that cannot be reconfigured.

        Two independent ways a cap bites, and both may apply at once:
        the chosen method funds every name, and — whatever the method — too few
        names cannot add up to a fully invested book under the per-asset cap.
        """
        if cap is None:
            return None
        mandate = self.mandate
        warnings: list[str] = []
        if policy_id in _FULL_UNIVERSE_POLICIES and (
                cap < len(mandate.universe_whitelist)):
            from qlab.algorithms.policy import get_operational_policy

            try:
                label = get_operational_policy(policy_id).label
            except (ValueError, RuntimeError):
                label = policy_id
            warnings.append(
                f"{label} holds every name; a cap of {cap} will refuse its "
                "plans — choose a policy that honours the cap or raise it")
        # Method-independent arithmetic: k names at the per-asset cap must be
        # able to reach 1.0, or a fully invested plan cannot exist at all.
        if mandate.fully_invested and cap * mandate.max_weight_per_asset < 1.0:
            warnings.append(
                f"a cap of {cap} cannot reach 100% at a "
                f"{mandate.max_weight_per_asset:.0%} per-asset cap; every plan "
                "will refuse")
        return " · ".join(warnings) or None

    def method_payload(self) -> dict:
        """The chosen method and cap, what may be chosen, and what may not.

        The research entries are listed with their stage precisely so the desk
        can say *why* an operator cannot pick one, rather than leaving a method
        the catalog knows about missing from the card with no explanation.
        """
        from qlab.algorithms.catalog import list_algorithms
        from qlab.algorithms.policy import list_operational_policies
        from qlab.trader.mandate import load_mandate_overrides

        current = self.mandate.operational_policy
        operational = [
            {"id": row["id"], "label": row["label"], "arm_id": row["arm_id"],
             "rationale": row["rationale"], "current": row["id"] == current}
            for row in list_operational_policies()
        ]
        research = [
            {"id": spec["id"], "label": spec["label"], "stage": spec["stage"],
             "choosable": False}
            for spec in list_algorithms(category="allocation")
            if spec["stage"] != "operational"
        ]
        return {
            "current": {"operational_policy": current,
                        "max_holdings": self.mandate.max_holdings},
            "operational": operational,
            "research": research,
            "overrides": load_mandate_overrides(),
            "warning": self._cap_warning(current, self.mandate.max_holdings),
        }

    def _validated_policy(self, value: object) -> str:
        """The requested method, or a refusal that names why it is not one."""
        from qlab.algorithms.catalog import get_algorithm
        from qlab.algorithms.policy import get_operational_policy

        if not isinstance(value, str) or not value.strip():
            raise ValueError("operational_policy must be a method id or null")
        policy_id = value.strip()
        try:
            spec = get_algorithm(policy_id)
        except KeyError:
            spec = None
        if spec is not None and spec.stage != "operational":
            # The desk must say the stage, not merely "unknown": a research arm
            # an operator has read about in the ablation is exactly what they
            # will try, and "cardinal_min_variance is not a method" would be a
            # lie about why. Promotion is an evidence decision, not a POST.
            raise ValueError(
                f"{policy_id!r} is stage={spec.stage!r} and is not an "
                "operational method; promotion out of research takes evidence "
                "and a catalog change, not a desk setting")
        try:
            get_operational_policy(policy_id)
        except (ValueError, RuntimeError) as exc:
            raise ValueError(str(exc)) from exc
        return policy_id

    def _validated_cap(self, value: object) -> int:
        """The requested cap, bounded by the universe it has to fit in."""
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("max_holdings must be an integer or null")
        ceiling = len(self.mandate.universe_whitelist)
        if not 1 <= value <= ceiling:
            raise ValueError(
                f"max_holdings must be between 1 and {ceiling} (the size of "
                f"the mandated universe), got {value}")
        return value

    def set_method(self, body: dict) -> dict:
        """Record the operator's method and/or cap, and re-load the mandate.

        Persisting is not enough: `self.mandate` is loaded once at construction
        and is what every plan check reads, so a saved override that did not
        replace it would take effect only after a restart — while the desk said
        it was already in force.
        """
        from qlab.trader.mandate import (
            OVERRIDABLE_FIELDS, load_mandate_overrides, save_mandate_overrides)

        if not isinstance(body, dict) or not body:
            raise ValueError(
                "send operational_policy, max_holdings, or both (null clears)")
        unknown = sorted(set(body) - set(OVERRIDABLE_FIELDS))
        if unknown:
            named = ", ".join(repr(key) for key in unknown)
            raise ValueError(
                f"only operational_policy and max_holdings may be set from the "
                f"desk; refusing {named} — every other limit lives in "
                "mandate.yaml")
        with self._mandate_lock:
            # Snapshot before anything is mutated: this is what goes back if
            # the merged mandate turns out not to load.
            stored_before = load_mandate_overrides()
            overrides = dict(stored_before)
            previous = {
                "operational_policy": self.mandate.operational_policy,
                "max_holdings": self.mandate.max_holdings,
            }
            changes: list[tuple[str, object]] = []
            if "operational_policy" in body:
                value = body["operational_policy"]
                value = None if value is None else self._validated_policy(value)
                changes.append(("operational_policy", value))
            if "max_holdings" in body:
                value = body["max_holdings"]
                value = None if value is None else self._validated_cap(value)
                changes.append(("max_holdings", value))
            for field_name, value in changes:
                if value is None:
                    overrides.pop(field_name, None)
                else:
                    overrides[field_name] = value
            save_mandate_overrides(overrides)
            # Re-load rather than mutate: the merge, and every validation the
            # mandate does over the merged result, lives in `load_mandate`. If
            # the merged mandate does not load, the file must go back: a
            # persisted override the mandate refuses is a desk that cannot
            # start, which is a worse failure than a refused POST. The route's
            # own bounds should make this unreachable, so the branch is pinned
            # by a test that injects the failure rather than by a real one.
            try:
                self.mandate = self._reload_mandate()
            except Exception as exc:
                save_mandate_overrides(stored_before)
                raise ValueError(f"refusing that change: {exc}") from exc
            # Inside the lock: the row says what replaced what, and a second
            # writer landing between the swap and the row would make that pair
            # describe a state nobody was ever in.
            for field_name, value in changes:
                self.registry.record_event("mandate_override", {
                    "field": field_name,
                    # What is in force now, which for a cleared override is the
                    # shipped mandate's own value — not the null that cleared it.
                    "value": getattr(self.mandate, field_name),
                    "previous": previous[field_name],
                })
            return self.method_payload()

    @staticmethod
    def _reload_mandate():
        """Seam: the one call `set_method` rolls back around (invariant 10)."""
        from qlab.trader.mandate import load_mandate

        return load_mandate()

    # -- live quote stream ---------------------------------------------------
    def attach_market_stream_runner(self, runner) -> None:
        """serve() hands the transport in; a bare session never opens a socket.

        ``runner`` is called on its own thread with keyword arguments
        ``supervisor, key, secret, stop_event`` and is expected to block until
        the stop event is set (the shape of ``run_alpaca_market_stream``).
        """
        self._stream_runner = runner
        self.retune_market_stream()

    def retune_market_stream(self) -> None:
        """Start or stop the live quote stream to match the desk mode.

        Refusals are named, never silent: a live desk that cannot stream says
        which credential is missing rather than reporting the demo runtime's
        reason (invariant 4).
        """
        from qlab.data.stream import build_alpaca_market_stream
        from qlab.trader import alpaca_auth

        with self._stream_lock:
            if self._stream_stop is not None:
                self._stream_stop.set()
                self._stream_stop = None
            self.market_stream = None
            if self.desk_mode.offline:
                self.market_stream_reason = (
                    "no live market stream (demo/offline runtime)")
                return
            if self._stream_runner is None:
                self.market_stream_reason = (
                    "no live market stream (this runtime attaches none)")
                return
            try:
                creds = alpaca_auth.resolve_alpaca_credentials()
            except alpaca_auth.AlpacaAuthError as exc:
                self.market_stream_reason = f"no live market stream: {exc}"
                return
            if creds is None:
                self.market_stream_reason = (
                    "no live market stream: live desk mode needs Alpaca API "
                    "keys — set ALPACA_API_KEY/ALPACA_API_SECRET or store "
                    "keys with `alpaca profile login`")
                return
            if not (creds.api_key and creds.secret_key):
                # A browser login authorizes the trading API but the data
                # websocket authenticates with an API key pair only; saying
                # "log in again" here would send the operator in a loop.
                self.market_stream_reason = (
                    f"no live market stream: profile {creds.profile_name!r} "
                    "is a browser login and the data websocket needs an API "
                    "key pair — put paper keys in ALPACA_API_KEY/"
                    "ALPACA_API_SECRET")
                return
            supervisor = build_alpaca_market_stream(
                list(self.mandate.universe_whitelist),
                os.environ.get("ALPACA_FEED", "").strip() or "iex",
                on_event=self._publish_stream_event)
            stop = threading.Event()
            thread = threading.Thread(
                target=self._stream_runner, name="qlab-market-stream",
                kwargs={"supervisor": supervisor, "key": creds.api_key,
                        "secret": creds.secret_key, "stop_event": stop},
                daemon=True)
            self._stream_stop = stop
            self.market_stream = supervisor
            self.market_stream_reason = ""
            thread.start()

    def stop_market_stream(self) -> None:
        with self._stream_lock:
            if self._stream_stop is not None:
                self._stream_stop.set()
                self._stream_stop = None
            self.market_stream = None

    def _publish_stream_event(self, payload: dict) -> None:
        # The supervisor's transitions (never per-tick quotes) onto the SSE
        # bus, in the shape every other market event already has.
        event = {
            "event_id": str(uuid.uuid4()),
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": "market_stream",
            "payload": payload,
        }
        with self._market_lock:
            self._market_events.append(event)

    def data_policy(self, offline: bool):
        """The one data policy this desk runs under, desk-mode aware.

        ``policy_for(offline)`` alone answers from the environment
        (``QLAB_DATA_PROVIDER``, default yfinance) and never from the desk —
        so a LIVE desk with a working Alpaca login still priced itself from
        yfinance, every permit said "research-grade only", and no workflow
        was ever startable. The screenshot of that dead end is why this
        method exists: on a live desk with resolvable Alpaca credentials the
        provider is alpaca (execution-grade); an explicit env override still
        wins, because an operator who set one said something.
        """
        from qlab.core import data as market

        if offline:
            return market.policy_for(True, seed=self.seed)
        if os.environ.get("QLAB_DATA_PROVIDER", "").strip():
            return market.policy_for(False, seed=self.seed)
        from qlab.trader.alpaca_auth import (
            AlpacaAuthError, resolve_alpaca_credentials)
        try:
            creds = resolve_alpaca_credentials()
        except AlpacaAuthError:
            creds = None
        if creds is not None:
            return market.policy_for(False, provider="alpaca", seed=self.seed)
        return market.policy_for(False, seed=self.seed)

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
            # The one thing the pair cannot say about itself. A desk nobody has
            # chosen serves the same six fields as one deliberately pointed at
            # `synthetic · simulated`, so a surface that wants to *ask* which
            # desk this is has to be told the difference by name.
            "chosen": self.desk_mode_chosen,
        }

    def set_alpaca_credentials(self, api_key: object, api_secret: object,
                               *, replace: bool = False) -> dict:
        """Store a login the operator typed, and report what the desk can see.

        The write itself belongs to ``alpaca_auth`` and stays there: it is the
        single secrets authority, so no surface — this one included — learns
        what a profile looks like, what mode it needs, or when overwriting one
        would destroy something. ``replace`` is carried through unread: this
        method decides nothing about it.

        The book is NOT switched. Mode is explicit-never-inferred
        (``desk_mode.py``), so a login makes LIVE·ALPACA *choosable* and
        nothing more; ``credentials_ok`` in the response is how the client
        learns that.
        """
        from qlab.trader.alpaca_auth import write_credentials

        write_credentials(api_key, api_secret, replace=replace)
        # Names nothing else. An event row replays forever, so neither the key,
        # nor the secret, nor a mask of either, nor the path they went to
        # belongs on the bus — "a login was stored, from the desk" is the whole
        # auditable fact.
        self.registry.record_event(
            "alpaca.credentials_updated", {"source": "tui"})
        return self.desk_mode_payload()

    def probe_alpaca_credentials(self) -> dict:
        """Ask the venue whether the resolved credentials work.

        Does network I/O: callers must NOT hold the owner dispatch lock — the
        same rule as ``llm_backends_catalog``, and do_POST routes it outside
        for exactly that reason.

        Every outcome is a result rather than a rejection, including a broken
        credential source. This route takes no body, so there is no input for a
        400 to be about, and a client that has to render "did it work?" should
        read one shape however the answer turned out.
        """
        from qlab.trader.alpaca_auth import (
            AlpacaAuthError, probe_credentials, resolve_alpaca_credentials)

        try:
            creds = resolve_alpaca_credentials()
        except AlpacaAuthError as exc:
            return {"ok": False, "reason": str(exc)}
        return probe_credentials(creds).to_dict()

    # -- model routing ------------------------------------------------------
    def llm_backends_catalog(self, refresh: bool = False) -> dict:
        """Every backend, whether it can serve now, and what it serves.

        Does network I/O: callers must NOT hold the owner dispatch lock (the
        same rule that keeps the news fetch off `/api/tui`). The HTTP handler
        runs this route outside the lock for exactly that reason.

        `available()` and `models()` raise when a backend is present but
        answers wrongly — a misconfiguration the operator has to see. It
        belongs in the entry's own reason, not in a 500 on the one route the
        picker needs to render itself.
        """
        with self._llm_catalog_lock:
            cached = self._llm_catalog
        if not refresh and cached is not None:
            stamped, payload = cached
            if time.monotonic() - stamped < _LLM_CATALOG_TTL_SECONDS:
                return payload
        payload = self._probe_llm_backends()
        with self._llm_catalog_lock:
            self._llm_catalog = (time.monotonic(), payload)
        return payload

    def _probe_llm_backends(self) -> dict:
        """The uncached probe. Deliberately outside `_llm_catalog_lock`.

        Two concurrent callers may both probe; that costs a duplicate round
        trip and nothing else. Holding the lock across the network instead
        would serialize every reader behind a daemon that can take
        `PROBE_TIMEOUT_S` to answer.
        """
        from qlab.operator.llm_backends import (
            BACKENDS, LlmBackendError, build_backend)

        entries = []
        for name in sorted(BACKENDS):
            try:
                backend = build_backend(name)
                available, reason = backend.available()
                # A backend that cannot serve reports no models anyway, so
                # asking is a second round trip for a known empty list.
                models = list(backend.models()) if available else []
            except LlmBackendError as exc:
                entries.append({"name": name, "available": False,
                                "reason": str(exc), "models": []})
                continue
            entries.append({"name": name, "available": bool(available),
                            "reason": str(reason), "models": models})
        return {"backends": entries, "probed_at": self._now_iso()}

    def _refuse_unservable(self, choice: SurfaceModel) -> None:
        """Raise unless the live catalog says this choice can serve right now.

        The refusal carries the catalog's own sentence rather than a second
        opinion composed here, so "why can't I pick this" has one answer.
        Reads the catalog the picker has just fetched to render itself, so it
        normally lands on the warm cache rather than probing again.
        """
        entries = {entry["name"]: entry
                   for entry in self.llm_backends_catalog()["backends"]}
        entry = entries.get(choice.backend)
        if entry is None:
            raise ValueError(
                f"unknown LLM backend {choice.backend!r}; this desk serves "
                f"{', '.join(sorted(entries)) or 'no backend at all'}")
        if not entry["available"]:
            raise ValueError(entry["reason"])
        if choice.model not in entry["models"]:
            raise ValueError(
                f"the {choice.backend} backend cannot serve {choice.model!r} "
                f"right now; it serves {', '.join(entry['models'])}")

    def set_llm_config(self, surface: str, backend: str | None = None,
                       model: str | None = None,
                       enabled: bool | None = None) -> dict:
        """Point one surface at a model, switch the reasoner, or refuse and say why.

        The owner is the single validator: a client may offer whatever it likes
        — a stale catalog, a hand-typed model name — and this is where it is
        checked.

        Turning a surface ON validates the pair it turns on; turning it OFF
        validates nothing; changing an enabled surface's pair validates the new
        pair. An off-switch that required the daemon to be reachable stranded
        an operator whose Ollama had just died with a reasoner they could not
        turn off — the gate blocked the one action that fixed the situation,
        and guarded a surface on its way to being unused. Watching pair changes
        alone was the mirror mistake: choosing while off and enabling
        afterwards changes one thing at a time and slipped an unservable pair
        onto a live surface.

        `backend`/`model` are optional together: sending neither leaves the
        pair alone, which is what makes `{surface, enabled}` a usable switch.
        """
        from qlab.core.llm_config import SURFACES

        if surface not in SURFACES:
            raise ValueError(
                f"unknown model surface {surface!r}; the desk has "
                f"{' and '.join(SURFACES)}")
        if enabled is not None and surface != "reasoner":
            # The workforce is what the desk already is. Accepting a switch
            # here and dropping it would report a change that never happened.
            raise ValueError(
                "only the reasoner surface can be switched on or off")
        if (backend is None) != (model is None):
            raise ValueError(
                "a model choice needs both a backend and a model; send both, "
                "or send enabled alone to switch the reasoner")
        if backend is None and enabled is None:
            raise ValueError(
                "nothing to change: send a backend and a model, or enabled")

        current = getattr(self.llm_config, surface)
        chosen = current if backend is None else SurfaceModel(backend, model)
        # Only the reasoner has an off state; the workforce IS the desk, so a
        # workforce choice is always one that will be used.
        was_in_use = surface != "reasoner" or self.llm_config.reasoner_enabled
        in_use = was_in_use if enabled is None else bool(enabled)
        # Turning a surface ON validates the pair it turns on; turning it OFF
        # validates nothing; changing an enabled surface's pair validates the
        # new pair.
        if in_use and (chosen != current or not was_in_use):
            self._refuse_unservable(chosen)

        updated = self.llm_config.with_surface(surface, chosen, enabled)
        # Disk before memory: a write that fails must not leave the desk
        # running on a choice that will not survive a restart, and must not
        # announce one either.
        save_llm_config(updated)
        self.llm_config = updated
        self.registry.record_event(
            "llm.config_changed",
            # Names only: a backend's URL may carry a credential, and the
            # audit bus is exactly where that must never land.
            {"surface": surface, "backend": chosen.backend,
             "model": chosen.model, "enabled": enabled})
        return {
            "surface": surface,
            **self.llm_config.to_dict(),
            "effect": (
                # Not "off, so nothing happens": the chat surface reads the
                # pair regardless of the switch, and a sentence saying
                # otherwise would describe a desk that is already answering as
                # silent. What the switch buys is the template judgment.
                f"Atlas answers you on {chosen.backend} {chosen.model}; enable "
                f"the reasoner to let it choose templates too"
                if surface == "reasoner" and not self.llm_config.reasoner_enabled
                else f"Atlas reasons with {chosen.backend} {chosen.model}"
                if surface == "reasoner"
                else f"the workforce roles run on "
                     f"{chosen.backend} {chosen.model}"),
        }

    def llm_payload(self) -> dict:
        """The config plus the LAST availability reading — never a probe.

        `tui_snapshot` runs under the owner dispatch lock and the TUI polls it
        every two seconds; probing here would block every other request on a
        daemon that may be a network hop away. The picker's own route is the
        only prober, so this reports what was last seen and when, and says so
        plainly when nothing has been probed yet.
        """
        with self._llm_catalog_lock:
            cached = self._llm_catalog
        payload = self.llm_config.to_dict()
        if cached is None:
            return {**payload, "availability": None, "probed_at": None}
        _, catalog = cached
        return {
            **payload,
            # The catalog minus the model lists: an Ollama host can hold
            # dozens, and this rides in a payload polled every two seconds.
            "availability": [
                {"name": entry["name"], "available": entry["available"],
                 "reason": entry["reason"]}
                for entry in catalog["backends"]],
            "probed_at": catalog["probed_at"],
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
        mcp_error = ""
        if config_path.exists():
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
                servers = sorted(config.get("mcpServers", {}))
            except Exception as exc:
                # A file that exists but does not parse is not the same fact as
                # no file: reporting both as "not configured" sent the operator
                # to re-add a server entry that was already there, while the
                # parse error was surfaced nowhere.
                mcp_error = f"{type(exc).__name__}: {exc}"[:200]
        proxy_available = importlib.util.find_spec("fastmcp") is not None
        # Cache-only provenance: never a network fetch from a status poll.
        # Read the cache namespace of the provider the desk actually runs
        # under — defaulting the namespace left SETTINGS saying "yfinance"
        # while every fetch, permit and workflow ran on alpaca.
        provenance = data.cached_provenance(
            self.mandate.universe_whitelist,
            provider=self.data_policy(offline).provider)
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
        from qlab.news.providers import macro
        from qlab.tui.claude import resolve_claude_executable

        # One resolver for every "is claude here" answer (Windows shims).
        claude_available = bool(resolve_claude_executable())
        # How much life the hand-maintained look-ahead has left. `upcoming`
        # only refuses once it is ALREADY exhausted, which is a day too late to
        # act on; this is the same fact while there is still time to extend the
        # file. An unreadable config is None here and raises where it is
        # actionable, not on a status poll.
        try:
            calendar_days_left = macro.calendar_days_left(
                datetime.now(timezone.utc))
        except Exception:
            calendar_days_left = None
        return {
            "mode": "paper",
            "offline": offline,
            "claude_available": claude_available,
            "mcp_configured": bool(servers),
            "mcp_servers": servers,
            "mcp_config_error": mcp_error,
            "mcp_proxy_available": proxy_available,
            "governed_available": proxy_available and claude_available,
            "governed_authority": "propose_only",
            "claude_role": "workforce_orchestrator",
            "workforce_available": proxy_available and claude_available,
            "governed_lock_reason": (
                "agent authority is intentionally propose-only; paper execution "
                "requires explicit human confirmation"
            ),
            "calendar_days_left": calendar_days_left,
            "data_source": provenance[0] if provenance else "none",
            "data_age_days": provenance[1] if provenance else None,
            "autopilot": autopilot,
            # What the desk has retired. Two COUNT(*)s, cheap enough for the
            # snapshot path, and the only place an operator can see that the
            # queue is short because work expired rather than because nothing
            # ever fired.
            "expired_tasks": self.registry.count_atlas_tasks("expired"),
            "stale_workflows": self.registry.count_workflows("stale"),
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
        policy = self.data_policy(offline)
        try:
            snap = market.snapshot(
                tickers, date.today().isoformat(), lookback_days=252,
                policy=policy, seed=self.seed)
        except market.DataUnavailable as exc:
            return {
                "blocked": True, "mode": policy.mode, "provider": policy.provider,
                "feed": policy.feed, "reason": str(exc),
                # Both spellings, so a client reading either one sees the
                # refusal rather than an empty list next to a bare `false`.
                "reasons": [str(exc)],
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
        payload = health.to_dict()
        return {
            "blocked": False, "mode": policy.mode, "feed": policy.feed,
            "permit_id": permit.permit_id,
            "quote_health": (
                self.market_stream.health() if self.market_stream else None),
            # Singular `reason` is what `atlas_facts` and the TUI read, and
            # only the *blocked* branch above ever set it. On the ordinary
            # ineligible path both surfaces saw `eligible: false` with no
            # cause, so the desk refused without saying why (invariant 4).
            # The first reason is the governing one: `_integrity` writes
            # before the provenance and freshness checks append theirs.
            "reason": (payload["reasons"][0] if payload["reasons"] else None),
            **payload,
        }

    def quotes(self, symbols: list[str] | None = None) -> dict:
        """Latest cached quotes and stream health, or a no-stream report."""
        stream = self.market_stream
        wanted = symbols or self.mandate.universe_whitelist
        if stream is None:
            return {"live_stream": False,
                    "reason": self.market_stream_reason
                    or "no live market stream (demo/offline runtime)",
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
    def predictor_board_summary(self) -> dict:
        """The newest persisted predictor board, summarised for the reasoner.

        Absence is named: a desk that never ran the board, and a desk whose
        newest board row cannot be read, are different facts and render as
        different statuses. Nothing here is a judgment — ``age_days`` is a
        number, and whether it is too old is the reasoner's call.

        TTL-cached against the run revision: a hundred run rows on every poll.
        """
        return self._run_summary_cached(
            "predictor_board", self._predictor_board_summary)

    def _predictor_board_summary(self) -> dict:
        row = next(
            (
                r
                for r in self.registry.list_runs(limit=100)
                if r.get("kind") == "predictor_board"
            ),
            None,
        )
        if row is None:
            return {"status": "never_ran"}
        spec = row.get("spec")
        board = spec.get("board") if isinstance(spec, dict) else None
        models = board.get("models") if isinstance(board, dict) else None
        if not isinstance(models, list):
            return {"status": "unreadable", "run_id": row.get("run_id")}

        by_id = {
            entry.get("model_id"): entry
            for entry in models
            if isinstance(entry, dict)
        }
        baseline = by_id.get(board.get("baseline"))
        champion = by_id.get(board.get("champion"))
        deltas = [
            entry.get("delta_mean_ic_vs_baseline")
            for entry in by_id.values()
            if entry.get("model_id") != board.get("baseline")
            and isinstance(
                entry.get("delta_mean_ic_vs_baseline"), (int, float)
            )
        ]

        from datetime import date

        age_days = None
        try:
            age_days = (
                date.today() - date.fromisoformat(str(spec.get("as_of")))
            ).days
        except (TypeError, ValueError):
            pass

        def _metrics(entry: dict | None) -> dict | None:
            if not isinstance(entry, dict):
                return None
            # Everything the admission verdict was derived from travels with
            # the verdict. `usable: true` alone is a comparison with its
            # threshold, its sample size and its dispersion stripped off, and
            # a reader given only the flattering half of a board reads a
            # scraped bar as a decisive win.
            return {
                "model_id": entry.get("model_id"),
                "family": entry.get("family"),
                "variant": entry.get("variant"),
                "mean_ic": entry.get("mean_ic"),
                "ic_std": entry.get("ic_std"),
                "ic_stability": entry.get("ic_stability"),
                "usable": entry.get("usable"),
                "paired_t_vs_baseline": entry.get("paired_t_vs_baseline"),
                "wins_vs_baseline": entry.get("wins_vs_baseline"),
                "delta_mean_ic_vs_baseline": entry.get(
                    "delta_mean_ic_vs_baseline"),
                # Per-fold IC is what makes a mean interpretable: folds that
                # change sign are not a skill estimate, and only the folds
                # show that. Hyperparameters are dropped — they are a
                # reproducibility detail carried by the run row, not evidence.
                "per_fold": [
                    {"fold": fold.get("fold"), "ic": fold.get("ic")}
                    for fold in (entry.get("per_fold") or [])
                    if isinstance(fold, dict)
                ],
            }

        return {
            "status": "ok",
            "run_id": row.get("run_id"),
            "as_of": spec.get("as_of"),
            "source": spec.get("source"),
            "age_days": age_days,
            "admitted_any": bool(board.get("admitted_any")),
            # The bar a model had to clear, so `usable` can be re-derived
            # rather than trusted, and the sample it was measured on, so a
            # t-statistic arrives with its n.
            "admission": board.get("admission"),
            # Admission is a fixed per-model bar applied to a maximum over
            # seven tuned models. Measured on 100 noise panels, that
            # procedure admitted a champion 66 times and 84 cleared the 0.03
            # bar, so `usable` alone cannot carry the claim. `.get` returns
            # None for a board that predates the null, which is neither
            # established nor refuted and must not read as either.
            "champion_established": board.get("champion_established"),
            "selection_null": board.get("selection_null"),
            "n_obs": board.get("n_obs"),
            "n_folds": board.get("n_folds"),
            "target": board.get("target"),
            "horizon_days": board.get("horizon_days"),
            "embargo_days": board.get("embargo_days"),
            "kernels": board.get("kernels"),
            "champion": _metrics(champion),
            "baseline": _metrics(baseline),
            "best_delta_vs_baseline": max(deltas, default=None),
            "ranking": board.get("ranking"),
        }

    def predictor_board_detail(self) -> dict:
        """The whole predictor board, for a screen rather than for a prompt.

        `predictor_board_summary` is deliberately narrow: it feeds a reasoner
        that must not be flooded. An operator asking "is the quantum feature
        augmentation earning its place" needs the opposite — every model, the
        full ranking, and the per-fold series that says whether a mean IC is a
        skill estimate or an average over folds that changed sign.

        Nothing here is a judgment the algorithm did not make. `significant`
        is a stated |t| convention applied to the board's own numbers, and it
        is false whenever the fold count is unknown, because a t-statistic
        without its n cannot be significant — it is a ratio.
        """
        row = next(
            (
                r
                for r in self.registry.list_runs(limit=100)
                if r.get("kind") == "predictor_board"
            ),
            None,
        )
        lane = (
            "The augmented lane is the angle and ZZ quantum feature maps "
            "(`*:angle`, `*:zz`, `*:angle_zz`), simulated classically at no "
            "quantum cost. `ridge:none` is the unaugmented control, and "
            "`kernel:linear` applies no map at all — it is that same control "
            "in dual form, which is why the two agree to the last digit. A "
            "mapped model above the baseline is the augmentation earning its "
            "place; below it, the augmentation costs accuracy."
        )
        if row is None:
            # A desk that has never run the board is a fact about the desk. A
            # 404 would read as a broken endpoint instead, and an operator
            # would go looking for the wrong problem.
            return {
                "status": "never_ran", "models": [], "lane": lane,
                "reason": "no predictor board has been run on this desk; "
                          "no model has been evaluated, which is not the "
                          "same as one having been evaluated and rejected",
            }

        spec = row.get("spec")
        board = spec.get("board") if isinstance(spec, dict) else None
        models = board.get("models") if isinstance(board, dict) else None
        if not isinstance(models, list):
            return {
                "status": "unreadable", "run_id": row.get("run_id"),
                "models": [], "lane": lane,
                "reason": "the newest board row could not be read; its "
                          "result is unknown, not absent",
            }

        baseline_id = board.get("baseline")
        champion_id = board.get("champion")
        n_folds = board.get("n_folds")

        def _row(entry: dict) -> dict:
            family = str(entry.get("family") or "")
            variant = str(entry.get("variant") or "")
            t_stat = entry.get("paired_t_vs_baseline")
            per_fold = [
                fold.get("ic") for fold in (entry.get("per_fold") or [])
                if isinstance(fold, dict)
                and isinstance(fold.get("ic"), (int, float))
            ]
            # The augmented lane is defined by the FEATURE MAP, not the family.
            # `kernel:linear` sits in the kernel family but `quantum_gram`
            # returns before any map is applied, so it is the dual of the
            # plain ridge baseline and comes back bit-identical to it. Calling
            # it quantum-augmented would file a control in the treatment arm
            # and let the lane claim a row it did not earn.
            augmented = any(m in variant for m in ("angle", "zz"))
            control_note = None
            if family in ("kernel", "groupwise") and not augmented:
                control_note = (
                    f"{family}:{variant} applies no quantum feature map — it "
                    f"is the dual of the plain ridge baseline, a control "
                    f"sitting in the kernel family, not augmentation"
                )
            return {
                "model_id": entry.get("model_id"),
                "family": family or None,
                "variant": entry.get("variant"),
                # Which side of the experiment this model is on, stated rather
                # than left to be inferred from a naming convention.
                "augmented": augmented,
                "control_note": control_note,
                "is_baseline": entry.get("model_id") == baseline_id,
                "is_champion": entry.get("model_id") == champion_id,
                "mean_ic": entry.get("mean_ic"),
                "ic_std": entry.get("ic_std"),
                "ic_stability": entry.get("ic_stability"),
                "usable": entry.get("usable"),
                "delta_mean_ic_vs_baseline": entry.get(
                    "delta_mean_ic_vs_baseline"),
                "wins_vs_baseline": entry.get("wins_vs_baseline"),
                "paired_t_vs_baseline": t_stat,
                # |t| >= 2 is the stated convention. Without a fold count the
                # answer is False, never None: a t with no n is not weak
                # evidence, it is not evidence.
                "significant": bool(
                    isinstance(t_stat, (int, float))
                    and isinstance(n_folds, int) and n_folds > 1
                    and abs(t_stat) >= 2.0
                ),
                "per_fold": per_fold,
                "negative_folds": sum(1 for ic in per_fold if ic < 0),
            }

        rows = [_row(e) for e in models if isinstance(e, dict)]
        admitted = bool(board.get("admitted_any"))
        established = board.get("champion_established")
        null = board.get("selection_null")
        p_value = null.get("p_value") if isinstance(null, dict) else None
        if admitted and champion_id:
            reason = (
                f"{champion_id} was admitted: it cleared both admission "
                f"thresholds. Admitted is not promoted — the authority gate "
                f"never reads this board."
            )
            # The admission bar is a per-model threshold applied to the best
            # of seven tuned models. Measured on 100 pure-noise panels, 84
            # cleared the 0.03 mean_ic bar and 66 produced an admitted
            # champion. So "cleared the bar" cannot stand alone in the one
            # sentence most likely to be read.
            if established is False:
                reason += (
                    f" NOT ESTABLISHED: the same selection procedure run on "
                    f"null resamples reproduces a champion this good "
                    f"about as often (p={p_value}), so this is the fixed bar "
                    f"being cleared, not evidence of skill."
                )
            elif established is True:
                reason += (
                    f" It also beat its own selection null (p={p_value}), so "
                    f"the ranking is not obviously luck."
                )
            elif isinstance(null, dict) and null.get("underpowered_for_alpha"):
                # `None` has two causes. This null ran; it cannot reach alpha
                # because the smallest p available from T trials is 1/(T+1).
                # "not tested" would send an operator hunting a missing run.
                reason += (
                    f" VERDICT WITHHELD: the null ran, but {null.get('trials')}"
                    f" trials cannot establish anything at the 0.05 level —"
                    f" the smallest p reachable is"
                    f" {null.get('p_value_resolution')}. Neither a pass nor a"
                    f" refutation; re-run with more trials for a verdict."
                )
            else:
                reason += (
                    " Whether noise reproduces it was NOT tested by this run,"
                    " which is not the same as it having been tested and"
                    " held up."
                )
        else:
            reason = (
                "the board ran and admitted no model. That is its result, "
                "not a missing value: no candidate cleared both thresholds."
            )
        return {
            "status": "ok",
            "run_id": row.get("run_id"),
            "as_of": spec.get("as_of"),
            "source": spec.get("source"),
            "universe": spec.get("universe"),
            "lane": lane,
            "reason": reason,
            "admitted_any": admitted,
            # None means no null was built, and must stay distinguishable from
            # False: "not tested" is not "tested and refuted".
            "champion_established": established,
            "selection_null": null,
            "champion": champion_id,
            "baseline": baseline_id,
            "admission": board.get("admission"),
            "n_obs": board.get("n_obs"),
            "n_folds": n_folds,
            "target": board.get("target"),
            "horizon_days": board.get("horizon_days"),
            "embargo_days": board.get("embargo_days"),
            "kernels": board.get("kernels"),
            "features": board.get("features"),
            "search": board.get("search"),
            "ranking": board.get("ranking"),
            "models": rows,
            "caveats": spec.get("caveats") or [],
        }

    def workforce_summary(self, limit: int = 10) -> dict:
        """What the agents Atlas directs are actually doing, and where they stuck.

        Atlas is the manager of this workforce, and its reasoning surface used
        to carry regime, news, predictors and decisions but not one key naming
        a workflow, step, phase or agent. Asked "why is my desk stuck", it had
        nothing to answer from — while the registry held ten runs whose failing
        steps each carried a written sentence saying exactly what stopped them.

        A count of blocked runs is not that answer. The words on the step are,
        so the stalled step travels whole: its phase, the agent that owns it,
        and the summary that agent wrote.
        """
        rows = self.registry.list_workflows(limit=limit)
        # A step is terminal-bad if it stopped the run rather than finishing
        # it. Reported in the order the DAG runs, so "how far did it get" is
        # answerable, not just "did it finish".
        stuck_states = ("blocked", "failed", "interrupted", "abandoned")
        out: list[dict] = []
        counts: dict[str, int] = {}
        for wf in rows:
            status = str(wf.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
            steps = wf.get("steps") or []
            stalled = next(
                (s for s in steps if s.get("status") in stuck_states), None)
            request = wf.get("request")
            request = request if isinstance(request, dict) else {}
            out.append({
                "workflow_id": wf.get("workflow_id"),
                "kind": wf.get("kind"),
                "status": status,
                "current_phase": wf.get("current_phase"),
                "as_of": request.get("as_of"),
                "goal": str(request.get("goal") or "")[:300] or None,
                "created_at": str(wf.get("created_at") or "") or None,
                "updated_at": str(wf.get("updated_at") or "") or None,
                "completed_phases": [
                    s.get("phase") for s in steps if s.get("status") == "done"],
                "pending_phases": [
                    s.get("phase") for s in steps
                    if s.get("status") in ("queued", "working")],
                # None means "nothing stopped it", stated rather than left as
                # a missing key that reads the same as an unknown one.
                "stalled_at": None if stalled is None else {
                    "phase": stalled.get("phase"),
                    "agent": stalled.get("agent"),
                    "status": stalled.get("status"),
                    # The whole sentence the agent wrote. Truncating this to a
                    # label is what made the desk unreadable in the first place.
                    "summary": str(stalled.get("summary") or "") or None,
                },
                # Separate from `stalled_at`, because "it stopped here" and
                # "someone is waiting on you" are different claims. An
                # abandoned run stopped at a phase and is nobody's to-do: the
                # operator already decided. Showing the two as one thing put
                # seven amber boxes on a desk with six live decisions.
                "awaiting_operator": status in _AWAITING_OPERATOR,
            })

        # Resumable is the registry's own word for it, so this stays a fact
        # rather than a judgment about what the operator ought to do.
        needs = sum(1 for w in out if w["awaiting_operator"])
        if not out:
            reason = ("no workflow has ever run on this desk; the workforce "
                      "is idle because nothing has been started, which is not "
                      "the same as it having run and produced nothing")
        elif needs:
            reason = (f"{needs} of {len(out)} recent runs stopped short and "
                      f"can be resumed or abandoned; each carries the step "
                      f"that stopped it and the words its agent wrote")
        else:
            reason = (f"all {len(out)} recent runs reached a terminal state "
                      f"without stalling")
        return {
            "workflows": out,
            "counts": counts,
            "needs_attention": needs,
            "reason": reason,
        }

    def agent_stream(self, limit: int = 60) -> dict:
        """What the desk's agents actually said and did, most recent last.

        The coordinator republishes its session onto the audit bus precisely so
        an unattended run is legible, and nothing rendered it: the web UI had
        no reference to /api/events at all. Reasoning that is recorded and
        never read is not visibility, it is a log file.

        Kept to coordinator events. The bus also carries news archiving and
        owner-tick bookkeeping, which would bury the agents in their own feed.

        `session` events are set aside rather than shown. The coordinator no
        longer records them, but the bus is durable, so the heartbeats it
        recorded before that fix are still there: on the live desk 56 of 60
        rows were `Claude session task_progress` against 4 carrying actual
        debate reasoning. They are counted in the reason rather than dropped
        in silence, because an audit surface that quietly discards rows is no
        longer a record of what happened.
        """
        # Over-read, because the liveness rows being filtered are the bulk of
        # the history and would otherwise fill the window on their own.
        rows = self.registry.read_events_of_kind(
            "atlas_coordinator_event", max(limit * 8, 200))
        events = []
        suppressed = 0
        for row in rows:
            payload = row.get("payload") or {}
            if payload.get("event_kind") == "session":
                suppressed += 1
                continue
            events.append({
                "ts": row.get("ts"),
                "workflow_id": payload.get("workflow_id"),
                "event_kind": payload.get("event_kind"),
                # Empty string means the event carried no agent, which is
                # normal: only a subagent handoff names one. It is not a
                # missing value and is reported as the empty string it is.
                "agent": str(payload.get("agent") or ""),
                "tool": str(payload.get("tool") or ""),
                "text": str(payload.get("text") or ""),
            })
        events = events[-limit:]
        aside = (f"; {suppressed} SDK liveness heartbeats set aside"
                 if suppressed else "")
        if not events and suppressed:
            # A coordinator plainly ran; it just never said anything worth
            # keeping. Reporting that as silence would be a wrong reason.
            reason = (f"a coordinator ran and published only {suppressed} SDK "
                      "liveness heartbeats: no reasoning, tool call or result "
                      "was recorded for it")
        elif not events:
            reason = ("no coordinator has published to this desk's bus; the "
                      "agents are silent because none has run under this "
                      "owner, not because a run produced nothing to say")
        else:
            named = sorted({e["agent"] for e in events if e["agent"]})
            reason = (f"{len(events)} events from the coordinator session"
                      + (f", naming {', '.join(named)}" if named else
                         "; none names a subagent, so no handoff was recorded")
                      + aside)
        return {"events": events, "reason": reason,
                "suppressed_liveness": suppressed}

    def atlas_context(self, offline: bool, *, facts: dict | None = None) -> dict:
        """The rich, abstract surface a reasoning Atlas forms a view from.

        Deliberately NOT `atlas_facts`. That surface feeds `check_startable`,
        the authority gate, and a gate whose input is narrow, boolean and stable
        is auditable — one reading a large free-form context is not. Widening it
        would quietly move the gate into the same epistemic class as the thing
        it exists to constrain. So there are two surfaces: the gate keeps its
        nine booleans, the reasoner gets everything.

        What distinguishes this from `atlas_facts` is *content*. `regime.flip`
        is a boolean; a reasoner cannot form a view about a boolean. Here the
        five indicators arrive with their own thresholds, percentiles and
        one-line reasoning, the six qualitative signals arrive with theirs, and
        past decisions arrive with what actually happened. Nothing here is
        summarised down to a verdict, because summarising to a verdict is the
        judgment the reasoner is supposed to be doing.

        ``facts`` is accepted rather than always recomputed because
        ``atlas_facts`` is not idempotent within a tick: ``_atlas_regime_facts``
        records the robust state it saw, so a second call reports ``flip:
        False`` and a context composed that way would tell the reasoner nothing
        happened on the very tick something did.
        """
        from qlab.operator.templates import startable_templates

        if facts is None:
            facts = self.atlas_facts(offline)
        read = self.desk_read(offline)
        try:
            panel = self.regime_panel(offline)
        except Exception as exc:      # a broken panel is a fact, not a crash
            panel = {"error": str(exc)[:200], "readings": []}

        decisions = []
        for row in self.registry.recent_decisions(limit=8):
            decision_id = row.get("decision_id")
            lesson = self.registry.get_lesson(decision_id) if decision_id else None
            decisions.append({
                "decision_id": decision_id,
                "as_of": str(row.get("as_of") or ""),
                "kind": row.get("kind"),
                "rationale": str(row.get("rationale") or "")[:300],
                # The reflection loop resolves outcomes on its own horizon, so
                # this is often absent. Absent means unresolved, never neutral.
                "outcome": (lesson or {}).get("summary") if lesson else None,
            })

        news = self.news_payload(offline)
        try:
            matrix_entry = self._matrix_for_reasoner(offline)
        except Exception as exc:
            # Same rule the panel above follows: `atlas_judgment_request`
            # drops the whole request when composing this raises, so one
            # broken surface must arrive as a named gap, not as no context.
            matrix_entry = {"error": str(exc)[:200], "rows": {}}
        return {
            "as_of": self._now_iso(),
            # The gate's own view, carried verbatim so the reasoner can see
            # exactly what the deterministic layer will and will not permit.
            "gate_facts": facts,
            "mandate": {
                "universe": list(self.mandate.universe_whitelist),
                "max_weight_per_asset": self.mandate.max_weight_per_asset,
                "max_turnover_per_rebalance":
                    self.mandate.max_turnover_per_rebalance,
                "operational_policy": self.mandate.operational_policy,
                # Placeholder until the risk profile object exists. Named rather
                # than omitted so its absence is visible to the reasoner instead
                # of being mistaken for a default it can assume.
                "risk_profile": None,
            },
            # Five indicators, each with its own trailing threshold and the
            # sentence explaining what its number means.
            "regime_panel": {
                # Carried through: a panel that failed and a panel that read
                # nothing are different facts, and an empty `readings` list
                # alone would render the first as the second.
                "error": panel.get("error"),
                "robust_state": panel.get("robust_state"),
                "agreement": panel.get("agreement_count"),
                "disagreement": panel.get("disagreement_count"),
                "readings": [
                    {
                        "indicator": r.get("indicator_id"),
                        "state": r.get("state"),
                        "signal": r.get("signal"),
                        "threshold": r.get("threshold"),
                        "percentile": r.get("percentile"),
                        "reasoning": r.get("reasoning"),
                        "quality_flags": r.get("quality_flags") or [],
                    }
                    for r in (panel.get("readings") or [])
                ],
            },
            # Unsigned by construction: these are properties of the record, and
            # turning them into a view is the reasoner's job, not theirs.
            "qualitative_signals": read.get("qualitative_signals") or {},
            # The same counts, per name, as a table. Rows only: the claim keys
            # are archive ids a reasoner cannot resolve and must not cite, and
            # they stay on the route for a screen that can.
            "qualitative_matrix": matrix_entry,
            "news": {
                "provider": news.get("provider"),
                "error": news.get("error"),
                "counts": news.get("counts"),
                "coverage": news.get("coverage"),
                "uncovered": news.get("uncovered"),
                "headlines": [
                    {"headline": i.get("headline"), "scope": i.get("scope"),
                     "tickers": i.get("tickers"), "source": i.get("source"),
                     "published": i.get("published")}
                    for i in (news.get("items") or [])[:20]
                ],
            },
            "supported_claims": read.get("supported_claims") or [],
            "tensions": read.get("tensions") or [],
            # Additive on the REASONER's surface only. atlas_facts is
            # check_startable's input and must never learn about the archive.
            "archive": self.archive_summary(),
            "recent_decisions": decisions,
            # Forward-looking research evidence. Advisory by construction:
            # the gate never reads it, and a champion here is an admitted
            # model, never a promoted one.
            "predictors": self.predictor_board_summary(),
            # What the gate would allow right now, with its refusal reasons —
            # so the reasoner argues within its authority rather than proposing
            # work that will simply be refused.
            "startable": self.atlas.startable_tasks(facts),
            # And the menu itself: every registered template the gate permits
            # right now, keyed by id. `startable` above is about the QUEUED
            # tasks; this is about the templates, which is the set a template
            # choice has to come from. Derived from `check_startable`, never
            # written down beside it.
            "startable_templates": startable_templates(self.atlas.mode, facts),
            # The agents Atlas actually directs. Without this the manager
            # could describe the market in detail and not its own desk.
            "workforce": self.workforce_summary(),
        }

    def _matrix_for_reasoner(self, offline: bool) -> dict:
        """The matrix as counts, with the archive ids stripped.

        A gap in the look-ahead travels with it: a row whose
        ``days_to_next_release`` is None because nobody extended the calendar
        must not read to the reasoner as a name with nothing scheduled.
        """
        matrix = self.qualitative_matrix(offline)
        entry: dict = {
            "rows": {
                ticker: {k: v for k, v in row.items() if k != "claim_keys"}
                for ticker, row in (matrix.get("rows") or {}).items()
            },
        }
        for key in ("calendar_error", "news_error"):
            if matrix.get(key):
                entry[key] = matrix[key]
        return entry

    def atlas_facts(self, offline: bool, *, consume_flip: bool = False) -> dict:
        """Assemble the deterministic owner facts Atlas observes (no LLM).

        Deliberately narrow. This is `check_startable`'s input — the authority
        gate — so it stays booleans and counts. The reasoning surface is
        `atlas_context`; do not enrich this one.

        ``consume_flip`` is the tick's, and only the tick's. A regime flip is
        computed against a LATCHED previous state, so whoever latches gets the
        flip and everyone after them sees none. While every caller latched, a
        chat-initiated start — or any request that merely wanted to know what
        was startable — landing between the panel refresh and the next observe
        ate the flip, and `regime_review` was never queued. That is the same
        failure the hardcoded regime once caused, rebuilt out of a read.

        So: reads do not consume. ``atlas_observe`` and the heartbeat's
        judgment request pass True, because they are the tick and the tick is
        what acts on a change. Everything else — the chat's tools,
        `/api/atlas/startable`, `atlas_actionables`, the reasoner's context —
        sees the same flip and leaves it where it lies.
        """
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
            # Both of these were hardcoded, which made `regime_review`
            # unreachable autonomously (the flip branch could never fire) and
            # had the desk brief report zero pending approvals with approvals
            # sitting in the table — a confident wrong number rather than a
            # missing one.
            "regime": self._atlas_regime_facts(latch=consume_flip),
            "open_workflows": self._open_workflow_count(),
            "pending_approvals": len(
                self.registry.list_approval_requests(50, "pending")),
            "order_anomaly": anomaly,
            # The grounded window the news-analyst would interpret. Present so
            # template preconditions can refuse an empty record rather than
            # letting the analyst narrate silence.
            "news_window_sufficient": bool(
                (self.desk_read(offline).get("qualitative_signals") or {})
                .get("sufficient")),
            "news_window_items": len(
                (self.desk_read(offline).get("grounding") or {})
                .get("hashes", [])),
        }

    def _atlas_regime_facts(self, *, latch: bool = False) -> dict:
        """The robust regime and whether it just changed.

        Read from the panel the heartbeat already composed rather than
        recomputed here: `atlas_facts` runs on every observe tick and holds the
        dispatch lock, so this must not do data work.

        `flip` compares against the previous observation only. A restart
        therefore reports no flip rather than inventing one from a cold start,
        which is the safe direction: a spurious flip launches a workflow.

        ``latch`` records the state just read as the new previous one, which is
        what makes the flip a one-shot. Only the observe tick may do that — see
        ``atlas_facts``. A read that latched would answer a question and
        silently spend the answer.
        """
        cached = self._desk_read or {}
        panel = cached.get("panel") if isinstance(cached.get("panel"), dict) else {}
        robust = panel.get("robust_state") or cached.get("robust_state")
        state = str(robust) if robust else None
        flip = bool(
            state and self._last_robust_state
            and state != self._last_robust_state
            and state != "unknown" and self._last_robust_state != "unknown")
        if state and latch:
            self._last_robust_state = state
        return {"robust_state": state, "flip": flip}

    def _open_workflow_count(self) -> int:
        """How many workflows are actually still in flight.

        Not `len(list_workflows(...))`: that counts every row the window holds,
        finished ones included, so the number Atlas reasons about grew forever
        and never shrank. Resolved is `reconcile_tasks`' own set, so a status
        added there cannot leave a second opinion here — and `stale`, which
        this branch introduced, is open work by no definition.
        """
        from qlab.operator.atlas import WORKFLOW_RESOLVED_STATUSES

        return sum(
            1 for workflow in self.registry.list_workflows(50)
            if str(workflow.get("status") or "") not in WORKFLOW_RESOLVED_STATUSES)

    def atlas_observe(self, offline: bool, *, facts: dict | None = None,
                      judgments: dict | None = None) -> dict:
        """Run one deterministic Atlas observe tick against current owner facts.

        Reconciliation runs first: a dispatched workflow may have reached a
        terminal state since the last tick (or while this process was down), and
        its task must be resolved from that state before new work is considered.

        ``facts`` and ``judgments`` are the reasoner's two-phase tick handing
        back what it took. ``facts`` MUST be the same dict the judgment was
        made against — not because recomputing is slow but because
        ``atlas_facts`` consumes a regime flip, so a second assembly in one tick
        reports no flip and the observe would refuse to act on the very change
        the reasoner was just asked about.

        Both default to absent, which is this method exactly as it was: the
        `/api/atlas/observe` route passes neither, deliberately. That route runs
        inside the dispatch lock, where a model call cannot go, so a manual tick
        is deterministic and the desk's own heartbeat is the one loop that
        carries judgment.
        """
        reconciled = self.atlas.reconcile_tasks()
        if facts is None:
            # The tick, and the only other latching caller is the judgment
            # request that hands its facts straight back to this method.
            facts = self.atlas_facts(offline, consume_flip=True)
        observed = self.atlas.observe(facts, trading_date=date.today().isoformat(),
                                      judgments=judgments)
        if reconciled:
            observed = {**observed, "reconciled_tasks": reconciled}
        return observed

    def atlas_judgment_request(self, offline: bool) -> dict:
        """The facts and the triggers whose template the reasoner may choose.

        Registry-only and cheap — no model call — because the caller holds the
        dispatch lock here. Empty when the flag is off, and empty is the whole
        of the off state: nothing else in this path runs, so a desk that never
        turned the reasoner on takes exactly the code it took before.

        The facts come back with the request because the observe phase must use
        these and not its own (see ``atlas_observe``). The context is composed
        only when something is actually pending, since it costs a regime panel.
        """
        if not self.llm_config.reasoner_enabled:
            return {}
        # Latches, because these facts ARE the observe's facts: they are handed
        # back to `atlas_observe`, which then does not assemble its own.
        facts = self.atlas_facts(offline, consume_flip=True)
        # The facts are in the request before anything that can fail, and they
        # stay there on every path out. `atlas_facts` has already CONSUMED this
        # tick's regime flip by the time the composition below runs, so a
        # failure that dropped them would hand `observe` a second assembly
        # reporting `flip: False` — the desk quietly missing the very change it
        # was about to reason about. Losing the judgment is acceptable; losing
        # the flip is not.
        request: dict = {"facts": facts, "triggers": []}
        try:
            pending = self.atlas.pending_judgments(
                facts, trading_date=date.today().isoformat())
            if pending:
                # Composing the context costs a regime panel, a desk read and a
                # news payload — every one of them a surface with its own way
                # of failing, none of them the reasoner's fault, and all of them
                # running before the deterministic observe on this path.
                request["context"] = self.atlas_context(offline, facts=facts)
                request["triggers"] = pending
        except Exception as exc:
            self.note_reasoner_fallback(
                None, f"the desk could not be composed for the reasoner: {exc!r}")
        return request

    def note_reasoner_fallback(self, trigger: str | None, reason: str) -> None:
        """Record one reason the table stood in for the reasoner.

        Takes NO lock, deliberately, because its callers hold different ones.
        ``atlas_judge`` runs outside the dispatch lock and takes ``_LOCK``
        around its whole batch; ``atlas_judgment_request`` and the heartbeat's
        guard run *inside* it, where a second acquire would deadlock — ``_LOCK``
        is a plain ``threading.Lock``. Putting the acquire in here would be the
        kind of convenience that hangs the owner.
        """
        choice = self.llm_config.reasoner
        self.registry.record_event("reasoner.fallback", {
            "trigger": trigger, "backend": choice.backend,
            "model": choice.model, "reason": self._bounded(reason, 500)})

    def atlas_judge(self, request: dict) -> dict:
        """Ask the reasoner which template fits each pending trigger.

        **Must not be called while the dispatch lock is held.** This makes
        model calls — the same rule and the same reason as ``atlas_message``,
        and here it is load-bearing rather than polite: the observe tick holds
        the lock for its whole body, so a completion inside it would freeze the
        snapshot poll, the SSE poll and every approval behind them for up to
        ``REASONER_TIMEOUT_S``. It takes ``_LOCK`` itself, briefly, only to
        record what it could not do.

        Returns the choices that were made; a trigger absent from the map falls
        back to the table in ``observe``. Every failure is one of those
        absences, because the deterministic path has to complete regardless of
        what the reasoner did.
        """
        triggers = request.get("triggers") or []
        if not triggers:
            return {}
        # (trigger kind or None, why) — recorded once, at the end, under a
        # brief lock this method takes for itself.
        notes: list[tuple[str | None, str]] = []
        chosen: dict = {}

        try:
            # Inside the guard with everything else it feeds. Reading the
            # persisted config is not obviously fallible, which is exactly how
            # it sat above the try being called bare from the heartbeat.
            choice = self.llm_config.reasoner
            # Deliberately broad, against this file's usual rule, and it starts
            # at the PROBE rather than at the completion. A reasoner bug must
            # degrade the desk to the lookup, not stop the heartbeat: the
            # observe that follows this line is what keeps the drawdown tiers
            # and the approval expiry moving, and it is the deterministic half.
            # The catalog turns a misbehaving backend into a reason but lets
            # anything that is not an `LlmBackendError` straight through, and
            # on this path — unlike the picker route, where it becomes one
            # failed render — that would skip the whole tick, every tick, for
            # as long as the condition lasted. The reason is recorded, so a
            # desk that has quietly stopped judging is visible on the bus
            # rather than merely quiet.
            #
            # The catalog is the one place availability is asked, so a refusal
            # here carries the picker's own sentence rather than a second
            # opinion.
            entries = {entry["name"]: entry
                       for entry in self.llm_backends_catalog()["backends"]}
            entry = entries.get(choice.backend)
            if entry is None or not entry["available"]:
                # `trigger: null` — this refusal is about the desk, not about
                # one trigger, and a wildcard string in a field that otherwise
                # holds a trigger kind would read as one.
                notes.append((None, entry["reason"] if entry else
                              f"this desk has no {choice.backend!r} backend"))
            else:
                from qlab.operator.llm_backends import build_backend
                from qlab.operator.template_judge import choose_template

                backend = build_backend(choice.backend)
                for trig in triggers[:_REASONER_MAX_PER_TICK]:
                    picked = choose_template(
                        request.get("context") or {}, trig, backend,
                        choice.model,
                        note=lambda why, kind=trig.kind: notes.append((kind, why)))
                    if picked is not None:
                        chosen[trig.kind] = picked
                for trig in triggers[_REASONER_MAX_PER_TICK:]:
                    # The cap was the last exemption that took the table's
                    # answer without saying so. Every other fallback on this
                    # path is on the bus; a budget is not a reason to be the
                    # one that is not.
                    notes.append((trig.kind, "past this tick's reasoner budget "
                                             f"of {_REASONER_MAX_PER_TICK}"))
        except Exception as exc:
            notes.append((None, f"the reasoner could not be asked: {exc!r}"))

        if notes:
            try:
                with _LOCK:
                    for trigger_kind, why in notes:
                        self.note_reasoner_fallback(trigger_kind, why)
            except Exception:
                # A recorder that throws must not do what it guards. This block
                # sat outside the try above, so a failing `record_event` — the
                # write that exists to keep a degraded reasoner VISIBLE — was
                # itself enough to abort the tick, since `atlas_judge` is called
                # bare from the heartbeat. Losing the note is a lost note;
                # losing the tick is a desk that stopped observing.
                pass
        return chosen

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
        with self._news_lock:
            if self._desk_news is not None:
                return self._desk_news
        return {
            "items": [],
            # No providers, not a guessed one: nothing has been read yet, and
            # `outcomes` being empty already says so.
            "outcomes": {},
            "providers": [],
            "provider": "synthetic",
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
        from qlab.news.feed import fetch_news_stacked

        universe = self.mandate.universe_whitelist
        # An instant, not a calendar date. `date.today().isoformat()` is read by
        # the feed as local-midnight-labelled-UTC, and fetch_news drops anything
        # published after as_of — so the desk's window structurally excluded
        # every story filed so far today. Measured: a full 24 hours.
        as_of = datetime.now(timezone.utc)
        # Initialised before the try because `news_provider_for` parses, and a
        # malformed QLAB_NEWS_PROVIDERS must be a loud news window rather than
        # an exception that takes down the whole heartbeat tick around it.
        providers: tuple[str, ...] = ()
        provider_name = ""
        try:
            providers = self.news_provider_for(offline)
            provider_name = ",".join(providers)
            # Passed explicitly rather than letting the feed re-resolve from the
            # environment: the label and the fetch must be the same decision, or
            # the desk can report a provenance it did not use.
            stacked = fetch_news_stacked(
                as_of,
                universe,
                providers,
                lookback_hours=48,
            )
        except Exception as exc:
            # Only a stack with no living member raises. A member that died on
            # its own is an outcome carried beside the records, never a quietly
            # smaller window.
            outcomes = dict(getattr(exc, "outcomes", None) or {})
            window = {
                "items": [],
                # Each member keeps its own sentence when the feed carried them
                # (StackFailed); the aggregate stands in only when the failure
                # happened before any member ran, such as a malformed stack.
                "outcomes": outcomes or {name: str(exc) for name in providers},
                "providers": list(providers),
                "provider": provider_name,
                "provider_name": provider_name,
                "as_of": as_of.isoformat(),
                "error": str(exc),
            }
        else:
            window = {
                "items": stacked.items,
                "outcomes": dict(stacked.outcomes),
                "providers": list(stacked.providers),
                "provider": provider_name,
                "provider_name": provider_name,
                "as_of": as_of.isoformat(),
                "error": None,
            }
        # Publishing under the same lock the heartbeat composes under keeps a
        # manual refresh and a heartbeat tick from interleaving fetch and
        # publish, which could leave the drawer showing a window older than the
        # one just asked for — with no marker that it was stale.
        with self._news_lock:
            self._desk_news = window
        return window

    def archive_desk_news(self, window: dict) -> dict:
        """Persist one fetched window. Callers must already hold the lock.

        The window is a PARAMETER, never ``self._desk_news``. On a threaded
        owner, reading the shared attribute loses a window: the heartbeat
        fetches W1 and publishes it, a refresh handler fetches W2 and publishes
        over it, then the heartbeat archives what it finds — W2 — and W1 is gone
        from the wire forever.

        Deliberately NOT called from ``compose_desk_read``. That has four
        callers and one of them, ``refresh_desk_read``, is documented as the
        entry point for callers that do NOT hold the dispatch lock — burying a
        registry write inside it would create a lock-free writer by
        construction.

        ``stored`` counts rows written by this pass, insert or update, summed
        across the members. It is not a count of distinct stories: two members
        that both carried the same story write it in two batches.
        """
        from qlab.news.archive import build_archive_batch, canonical_timestamp
        from qlab.news.feed import NEWS_OUTCOME_OK, outcome_is_live

        window = window or {}
        items = list(window.get("items") or [])
        outcomes = dict(window.get("outcomes") or {})
        # One batch per member, never one batch for the merge: an ArchiveBatch
        # carries a single provenance, so a merged batch would file an EDGAR
        # filing and a wire story under one attribution and neither could be
        # replayed. Members with no records are still walked — "this source
        # answered with nothing" and "this source was not read" are different
        # facts, and only a per-member event can tell them apart.
        members = [str(name) for name in (window.get("providers") or [])]
        if not members:
            members = [str(window.get("provider_name") or "synthetic")]
        grouped: dict[str, list] = {name: [] for name in members}
        for item in items:
            name = str(getattr(item, "provider", "") or members[0])
            if name not in grouped:
                # An attribution nothing in the window declared. Filing it
                # anyway would archive a provenance the desk cannot account
                # for, which is exactly what the per-member batch exists to
                # prevent.
                raise RuntimeError(
                    f"news window carries provider {name!r}, which is not one "
                    f"of the members it declares ({', '.join(members)})")
            grouped[name].append(item)

        now = canonical_timestamp(datetime.now(timezone.utc))
        window_error = window.get("error")
        totals = {"inserted": 0, "updated": 0, "edges": 0, "total_rows": 0}
        per_provider: dict[str, dict] = {}
        stored = 0
        for provider, records in grouped.items():
            outcome = outcomes.get(provider, NEWS_OUTCOME_OK)
            # A member's own failure sentence is that member's error; the
            # window-wide error only stands in when no member said anything.
            # A partial member is live: its records are real, so its batch is
            # not a failed batch — the missing feeds are on the event instead.
            error = None if outcome_is_live(outcome) else str(outcome)
            if error is None and window_error and not outcomes:
                error = str(window_error)
            batch = build_archive_batch(
                records, provider=provider, offline=bool(self.offline_default),
                as_of=now, lookback_hours=48,
                universe=list(self.mandate.universe_whitelist), first_seen=now,
                error=error)
            result = self.registry.record_news_items(batch)
            # The event carries what distinguishes "the desk was not watching"
            # from "the wire was quiet" — a coverage gap the row count alone
            # cannot show — and now says which source it is a gap in.
            self.registry.record_event("news_archive", {
                "provider": provider, "returned": batch.returned,
                "inserted": result["inserted"], "updated": result["updated"],
                "window_fingerprint": batch.window_fingerprint,
                "outcome": outcome,
                "error": batch.error,
            })
            per_provider[provider] = result
            totals["inserted"] += int(result.get("inserted", 0))
            totals["updated"] += int(result.get("updated", 0))
            totals["edges"] += int(result.get("edges", 0))
            totals["total_rows"] = int(result.get("total_rows", 0))
            stored += int(result.get("inserted", 0)) + int(result.get("updated", 0))
        self._archive_stats_cache = None
        # The aggregate keys stay where callers already read them; `stored` and
        # `per_provider` are what a stack adds.
        return {**totals, "stored": stored, "per_provider": per_provider}

    def archive_summary(self) -> dict:
        """Archive size and span, TTL-cached.

        min/max over unindexed VARCHAR columns, and atlas_context is served
        under the dispatch lock on every poll — uncached this would put a full
        scan on the path every client waits behind.
        """
        with self._archive_lock:
            cached = self._archive_stats_cache
            if cached is not None and (time.monotonic() - cached[0]) < 30.0:
                return dict(cached[1])
            stats = self.registry.archive_stats()
            self._archive_stats_cache = (time.monotonic(), stats)
            return dict(stats)

    def news_search(self, *, question: str, offline: bool, limit: int = 25) -> dict:
        """Point-in-time search over the archive, with its own limits stated.

        Carries no `offer` field. Computing one forces atlas_facts, which runs
        the portfolio, data health and several registry queries under the same
        non-reentrant lock this is already served from.
        """
        from qlab.news.archive import canonical_timestamp, normalise_terms

        as_of = canonical_timestamp(datetime.now(timezone.utc))
        terms = normalise_terms(question)
        universe = list(self.mandate.universe_whitelist)
        tickers = [t for t in universe if t.lower() in {x.lower() for x in terms}]
        rows = self.registry.search_news(
            as_of=as_of, terms=terms, limit=limit)
        total = self.registry.count_news_matches(as_of=as_of, terms=terms)
        return {
            "as_of": as_of,
            "question": question,
            "terms": list(terms),
            "matched_total": total,
            "returned": len(rows),
            "items": rows,
            "universe_terms": tickers,
            # Absence stated: an empty archive and a query that matched nothing
            # are different facts and a bare zero conflates them.
            "archive": self.archive_summary(),
        }

    def atlas_reason(self, *, question: str | None, offline: bool,
                     limit: int = 12) -> dict:
        """The reasoner's ONLY production call site.

        Everything that makes the answer trustworthy is resolved here, in
        deterministic code, before the model is handed anything:

        * the evidence is a point-in-time archive search, so the model cannot
          cite a record the desk did not hold at the as-of it was given;
        * the model is resolved through the catalog's eligibility check, so an
          ineligible or unconfigured model is a loud refusal rather than a
          silent substitution (invariant 4);
        * any template the model proposes is passed to check_startable, which
          is the same authority gate the heartbeat uses. The reasoner proposes;
          the gate disposes, and its refusal reason is carried back verbatim
          rather than being softened into silence.
        """
        from qlab.news.archive import canonical_timestamp, normalise_terms, relevance_report
        from qlab.operator import models as model_catalog
        from qlab.operator.reasoner import ArchiveEvidence, reason

        now = canonical_timestamp(datetime.now(timezone.utc))
        terms = normalise_terms(question or "")
        universe = list(self.mandate.universe_whitelist)

        page = self.registry.search_news(as_of=now, terms=terms, limit=limit)
        matched = self.registry.count_news_matches(as_of=now, terms=terms)
        with_synthetic = self.registry.count_news_matches(
            as_of=now, terms=terms, include_synthetic=True)
        single_secondary = sum(
            1 for r in page if str(r.get("source_tier") or "") != "primary")
        stats = self.archive_summary()

        relevance = relevance_report(
            terms=terms, universe=universe, matched_total=matched, page=page,
            single_secondary_total=single_secondary,
            # Stored but not citable. Reporting the count is what stops an
            # empty result reading as "the wire was quiet".
            synthetic_excluded=max(0, with_synthetic - matched),
            newest_published=stats.get("newest_published"),
            archive_begins=stats.get("begins"),
            providers_in_window=sorted(
                {str(r.get("provider") or "") for r in page}),
            as_of=now, now=now)

        evidence = ArchiveEvidence(
            # .to_dict(), not the dataclass: ArchiveEvidence reads `relevance`
            # as a mapping. The two modules were built in parallel against one
            # spec and met here, which is where that mismatch surfaced.
            items=tuple(page), matched_total=matched,
            relevance=relevance.to_dict(),
            as_of=now, as_of_source="now", archive_begins=stats.get("begins"))

        # resolve_selection validates every slot, so it can raise too. Leaving
        # it outside the guard turned a bad selection into a 500 rather than the
        # named refusal this whole path exists to produce.
        requested = "unresolved"
        try:
            selection = model_catalog.resolve_selection()
            requested = selection.reasoner
            spec = model_catalog.check_eligible(selection.reasoner, slot="reasoner")
            provider = model_catalog.get_provider(spec.provider)
            configured, why = provider.configured()
            if not configured:
                raise model_catalog.ProviderError(why)
        # Both, because they are SIBLINGS under RuntimeError rather than
        # parent and child — catching ModelError alone let an unconfigured
        # provider escape as a 500.
        except (model_catalog.ModelError, model_catalog.ProviderError) as exc:
            # Named, never substituted: an answer served by a model the operator
            # did not choose is worse than no answer.
            self.registry.record_event("atlas_reason_refused", {
                "model_id": requested, "reason": str(exc)[:400]})
            return {"available": False, "model_id": requested,
                    "reason": str(exc), "question": question}

        facts = self.atlas_facts(offline)
        view = reason(
            context=self.atlas_context(offline), evidence=evidence,
            question=question, mode=str(self.atlas.status().get("mode") or ""),
            facts=facts, spec=spec, complete=provider.complete)

        payload = view.to_dict() if hasattr(view, "to_dict") else dict(view.__dict__)
        payload["available"] = True
        self.registry.record_event("atlas_reasoned", {
            "model_id": payload.get("model_id"),
            "served_model": payload.get("served_model"),
            "question": (question or "")[:300],
            "citations": len(payload.get("citations") or []),
            "offer": payload.get("offer"),
            "offer_refused_reason": payload.get("offer_refused_reason"),
            "matched_total": matched,
        })
        return payload

    def news_payload(self, offline: bool) -> dict:
        """The news window as a client renders it: stories, coverage, provenance.

        Coverage is reported per universe member and is the part worth showing.
        A cross-asset desk holding ACWI/BNDW/EMB gets almost no symbol-tagged
        coverage, so "0 stories about your holdings" and "the market was quiet"
        are completely different facts and the desk must not conflate them.
        """
        with self._news_lock:
            window = dict(self._desk_news or {})
        # Deliberately cache-only. `news_payload` is reached from
        # `tui_snapshot`, which runs under the dispatch lock, and
        # `fetch_desk_news` is the seam the network lives behind. Calling it
        # here "only when offline" is not safe either: offline is a runtime
        # value, so the next caller that passes False would block every request
        # on a slow provider. The window is filled by the heartbeat or by an
        # explicit `?refresh=1`, which the HTTP handler runs outside the lock.
        items = list(window.get("items") or [])
        universe = list(self.mandate.universe_whitelist)

        per_ticker = {t: 0 for t in universe}
        rows = []
        for item in items:
            tickers = tuple(getattr(item, "tickers", ()) or ())
            for t in tickers:
                if t in per_ticker:
                    per_ticker[t] += 1
            rows.append({
                "published": str(getattr(item, "published", "")),
                "headline": str(getattr(item, "headline", "")),
                "summary": str(getattr(item, "summary", ""))[:400],
                "source": str(getattr(item, "source", "")),
                "url": str(getattr(item, "url", "")),
                "tickers": list(tickers),
                # Untagged items are macro context. Labelling them keeps a
                # reader from treating a story about an unrelated single name
                # as evidence about a holding.
                "scope": "holding" if tickers else "macro",
            })
        tagged = sum(1 for r in rows if r["scope"] == "holding")
        return {
            # `or`, not a default: a malformed stack leaves the name empty,
            # and a blank where a provider goes reads as a rendering bug.
            "provider": window.get("provider_name") or "—",
            # Which sources were read, and what each of them said about being
            # read. A member that went away is visible on the wire rather than
            # showing up only as a window that quietly got smaller.
            "providers": list(window.get("providers") or []),
            "outcomes": dict(window.get("outcomes") or {}),
            "error": window.get("error"),
            "as_of": self._now_iso(),
            "lookback_hours": 48,
            "items": rows,
            "counts": {
                "total": len(rows),
                "holding": tagged,
                "macro": len(rows) - tagged,
            },
            "coverage": [
                {"ticker": t, "stories": per_ticker[t]}
                for t in sorted(per_ticker, key=lambda k: (-per_ticker[k], k))
            ],
            "uncovered": [t for t in universe if per_ticker[t] == 0],
        }

    def grounded_window(self, window: dict, universe) -> object:
        """The grounded form of one news window, derived once per window.

        Keyed by what grounding actually reads — the provider stamp, the
        window's own as_of, the records' identities, and the universe they are
        mapped against — so a genuinely new window is genuinely re-grounded and
        only an identical one is reused. The cached ``as_of`` is the first
        caller's instant by design: the point-in-time boundary belongs to the
        window, not to whoever looked at it second.
        """
        from qlab.news.grounding import ground

        items = list(window.get("items") or [])
        provider_name = str(window.get("provider_name") or "synthetic")
        key = (
            provider_name,
            str(window.get("as_of") or ""),
            tuple(universe),
            tuple((getattr(item, "url", ""), getattr(item, "published", ""),
                   getattr(item, "headline", "")) for item in items),
        )
        with self._news_lock:
            cached = self._grounded_news
            if cached is not None and cached[0] == key:
                # Handed out shared, not copied: GroundedNews is read-only by
                # convention, the same rule the handler payloads follow.
                return cached[1]
        # Grounding runs outside the lock: it is pure over its arguments, and
        # holding the news lock across it would serialize a fetcher behind it.
        grounded = ground(
            items, as_of=datetime.now(timezone.utc).isoformat(),
            provider=provider_name, universe=universe)
        with self._news_lock:
            self._grounded_news = (key, grounded)
        return grounded

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
        news_error = prefetched_news.get("error")
        # Ground the window before interpreting it: enforce the point-in-time
        # boundary, hash each record so an edited headline is a new record
        # rather than a silent rewrite, and cluster so corroboration is visible.
        # Shared with the matrix, which reads the same window.
        provider_name = str(
            prefetched_news.get("provider_name") or "synthetic")
        grounded = self.grounded_window(prefetched_news, universe)
        # Deterministic properties of the record, computed before anything
        # interprets it. These describe what the window covers and how well
        # supported it is — never a direction, which is the whole reason they
        # can sit next to price signals without becoming a forecast.
        from qlab.core.universe import load_universe
        from qlab.news.qualitative import qualitative_signals

        try:
            asset_classes = load_universe().asset_classes("core")
        except Exception:
            asset_classes = {}
        qualitative = qualitative_signals(
            grounded, universe=universe, asset_classes=asset_classes,
            lookback_hours=48)
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
        payload["qualitative_signals"] = qualitative.to_dict()
        self._desk_read = payload
        return self._desk_read

    def qualitative_matrix(self, offline: bool) -> dict:
        """Per-name counts of what the grounded window says, logged per window.

        Built from the same cached window ``desk_read`` composes from and
        grounded the same way, so the matrix is point-in-time by construction
        and never fetches — nothing reached from ``handle_api`` may block on
        the network. A window that has not been fetched yet, or one that
        arrived with an error, is named through ``news_error`` exactly as the
        read names it: zero coverage on a broken feed is not a quiet tape.

        The row is logged once per window rather than once per call, so the
        registry carries the history of the record rather than a row per page
        refresh.
        """
        from qlab.news.matrix import DESK_MATRIX_SOURCE, build_matrix
        from qlab.news.providers.macro import upcoming

        window = self.desk_news_window()
        universe = list(self.mandate.universe_whitelist)
        as_of = date.today().isoformat()
        provider_name = str(window.get("provider_name") or "synthetic")
        grounded = self.grounded_window(window, universe)
        # The look-ahead is hand-maintained and refuses loudly once it runs
        # out. That refusal must not take the matrix down with it — coverage is
        # a fact about the window, not about the yaml — but it is carried
        # verbatim, because "no releases ahead" and "nobody has extended the
        # calendar" are different claims and only one of them is about markets.
        calendar_error = None
        try:
            events = upcoming(datetime.now(timezone.utc))
        except RuntimeError as exc:
            events, calendar_error = [], str(exc)
        matrix = build_matrix(grounded.claims, universe, as_of, events)
        payload = matrix.to_dict()
        payload["provider"] = provider_name
        news_error = window.get("error")
        if news_error:
            payload["news_error"] = str(news_error)[:400]
        if calendar_error:
            payload["calendar_error"] = calendar_error
        with self._matrix_lock:
            # Scoped to the kind in SQL: a desk that logged a hundred solves
            # since the last matrix would fall off the end of any bounded scan
            # of all runs, find nothing, and re-log — turning one row per
            # window into one per window per day.
            # Scoped to the desk's own stamp as well: the ablation arm logs
            # its research windows here too, and reading one of those as "the
            # last window I logged" makes the hash differ every time, turning
            # the guard below into a row per page refresh.
            found = self.registry.matrix_runs(
                source=DESK_MATRIX_SOURCE, limit=1)
            previous = found[0] if found else None
            spec = previous.get("spec") if isinstance(previous, dict) else None
            logged = spec.get("matrix") if isinstance(spec, dict) else None
            last_hash = (logged or {}).get("window_hash") \
                if isinstance(logged, dict) else None
            if last_hash != matrix.window_hash:
                payload["run_id"] = self.registry.log_run(
                    "qualitative_matrix",
                    {"source": DESK_MATRIX_SOURCE, "matrix": matrix.to_dict()})
            else:
                payload["run_id"] = previous.get("run_id")
        return payload

    def news_provider_for(self, offline: bool) -> tuple[str, ...]:
        """Which news providers this desk should read, in order, and why.

        News follows the data lane the operator already chose. A desk running on
        live prices while its qualitative side is deterministic fixtures is the
        provenance confusion this codebase refuses everywhere else — the read
        would carry a real market and an invented narrative under one heading.

        Precedence: offline is always synthetic (it is the demo, and it must
        never reach the network); then whatever the operator named, because
        naming providers is an operator instruction — `QLAB_NEWS_PROVIDERS`
        first and the singular `QLAB_NEWS_PROVIDER` after it, parsed by the feed
        so the desk and a bare `fetch_news` cannot disagree about what a stack
        is; then Alpaca when a credential actually resolves; then synthetic,
        which `compose_desk_read` already labels as fixtures.
        """
        from qlab.news.feed import parse_provider_stack

        if offline:
            return ("synthetic",)
        named = (os.environ.get("QLAB_NEWS_PROVIDERS", "").strip()
                 or os.environ.get("QLAB_NEWS_PROVIDER", "").strip())
        if named:
            return parse_provider_stack(None)
        try:
            from qlab.trader.alpaca_auth import resolve_alpaca_credentials

            if resolve_alpaca_credentials() is not None:
                return ("alpaca",)
        except Exception:
            # A broken credential source is not a provider; the resolver reports
            # the detail where the operator can act on it.
            pass
        return ("synthetic",)

    def news_settings(self, offline: bool) -> dict:
        """What the desk reads, what it could read, and how the last fetch went.

        Composed from the wizard's own catalog and from the window cache
        ``fetch_desk_news`` publishes, so nothing here reaches the network and
        the route is safe under the dispatch lock.

        The EDGAR contact is reported as a bool and never as a value: it goes
        to the SEC in a User-Agent and nowhere else, and a settings pane that
        echoed it would put it on the wire in every snapshot.
        """
        from qlab.news import setup

        stack = self.news_provider_for(offline)
        window = self.desk_news_window()
        named = (os.environ.get("QLAB_NEWS_PROVIDERS", "").strip()
                 or os.environ.get("QLAB_NEWS_PROVIDER", "").strip())
        return {
            "lane": "synthetic" if offline else "live",
            "stack": list(stack),
            "configured": bool(named),
            "edgar_contact_set": bool(
                os.environ.get("QLAB_EDGAR_CONTACT", "").strip()),
            "catalog": [
                {"name": choice.name, "tier": choice.tier,
                 "needs": choice.needs, "cost": choice.cost,
                 "available": choice.available, "default": choice.default,
                 # What this desk reads right now, which is not the same claim
                 # as what a fresh desk would default to.
                 "chosen": choice.name in stack}
                for choice in setup.catalog(os.environ)
            ],
            # Empty before the first fetch: the window stand-in says so through
            # its own error channel, and a guessed outcome would be a lie about
            # a source nobody has called yet.
            "outcomes": {str(name): str(outcome) for name, outcome
                         in (window.get("outcomes") or {}).items()},
        }

    def _checked_news_stack(self, providers: object,
                            contact: str | None) -> tuple[str, ...]:
        """The stack to write, or a ValueError naming what cannot be honoured.

        Fail loud: a source that cannot answer is refused here with the fix,
        never written to be discovered dead on the first heartbeat — the same
        rule ``run_wizard`` applies when it refuses alpaca without a credential.
        """
        from qlab.news import setup

        if not isinstance(providers, list):
            raise ValueError("providers must be a list of source names")
        # A stack is an order, not a bag: `parse_provider_stack` would read a
        # repeat back as two members and the feed would fetch the source twice.
        # Deduped in place so the operator's order survives.
        names = tuple(dict.fromkeys(
            str(name).strip().lower() for name in providers
            if str(name).strip()))
        if not names:
            raise ValueError(
                "no news source was named; an explicit 'no real sources' is "
                '["synthetic"], exactly as the wizard writes it')
        known = _known_news_providers()
        unknown = [name for name in names if name not in known]
        if unknown:
            raise ValueError(
                f"unknown news provider(s) {', '.join(unknown)}; available: "
                f"{', '.join(known)}")
        catalog = {choice.name: choice for choice in setup.catalog(os.environ)}
        for name in names:
            choice = catalog.get(name)
            if choice is None or choice.available or not choice.needs:
                continue
            if name == "alpaca":
                raise ValueError(
                    "alpaca is chosen but no Alpaca credential resolves: run "
                    "`alpaca profile login` for a paper-only browser session, "
                    "or put ALPACA_API_KEY and ALPACA_API_SECRET in .env at "
                    "the workspace root")
            if name == "edgar" and not contact:
                raise ValueError(
                    f"edgar needs a contact, as {setup.CONTACT_SHAPE}: the SEC "
                    "requires a descriptive User-Agent with a contact, and "
                    "this desk does not send an invented one")
        return names

    def apply_news_settings(self, providers: object, edgar_contact: object,
                            verify: bool, *, offline: bool) -> dict:
        """Apply a news choice the way ``qlab news-setup`` applies one.

        This changes what the desk READS. It touches no posture, no approval
        and no plan: choosing a source is a provenance decision, never an
        authority one, and widening what Atlas reads must never widen what it
        can execute.

        ``verify`` does real network requests, one per member, so this method
        must not be called under the dispatch lock — ``do_POST`` routes the
        path around it. A failing member is reported and the change is still
        applied: the caller chose the stack, and refusing the write would make
        an unreachable source unconfigurable from the pane.
        """
        from qlab.news import setup

        # An empty box is "leave the contact alone", the wizard's own answer to
        # "keep the one already on file?" — not a contact to validate. Absent
        # and empty mean the same thing here; a non-empty one must be usable.
        typed = "" if edgar_contact is None else str(edgar_contact).strip()
        contact = setup.validate_contact(typed) if typed else None
        # A contact already on file makes edgar `available` in the catalog, so
        # only the typed one has to be passed down.
        names = self._checked_news_stack(providers, contact)
        plan = setup.SetupPlan(
            read_news=names != ("synthetic",), providers=names,
            edgar_contact=contact, verify=bool(verify))
        # `verify_plan` exports the contact and `write_env_values` sets each
        # name before the file lands, so a raise anywhere in between would
        # leave the process holding a stack that .env does not — and this
        # route's "nothing was written" refusal would be untrue. The env
        # mutation survives only a completed apply_plan.
        guarded = ("QLAB_NEWS_PROVIDERS", "QLAB_EDGAR_CONTACT")
        before = {name: os.environ.get(name) for name in guarded}
        try:
            report = setup.verify_plan(
                plan, self.mandate.universe_whitelist,
                environ=os.environ) if plan.verify else None
            setup.apply_plan(plan, root=workspace_root(), environ=os.environ)
        except BaseException:
            for name, value in before.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            raise
        with self._news_lock:
            # The next heartbeat must fetch through the new stack rather than
            # publish a window the old one produced.
            self._desk_news = None
        payload = self.news_settings(offline)
        if report is not None:
            payload["verify"] = {
                # ANY-member semantics, `check_news`'s own: one living member
                # is still a record. This is not whole-stack health, and a
                # member can be ok while short a feed — hence quality_flags.
                "ok": bool(report.get("ok")),
                # Names, one sentence and the flags: a member report also
                # carries the credential description, which is not the pane's
                # business.
                "members": {
                    str(name): {
                        "ok": bool(member.get("ok")),
                        "detail": str(member.get("error") or ""),
                        "quality_flags": [
                            str(flag) for flag
                            in (member.get("quality_flags") or [])],
                    }
                    for name, member in (report.get("members") or {}).items()},
            }
        return payload

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
        # The grounding was just zeroed, so carrying the previous window's
        # signals forward would report coverage the desk can no longer stand
        # behind. Recompute them over nothing: every value becomes None with a
        # no_window state, which is the honest reading of a failed recompose.
        from qlab.news.grounding import ground
        from qlab.news.qualitative import qualitative_signals

        payload["qualitative_signals"] = qualitative_signals(
            ground([], as_of=self._now_iso(), provider="synthetic",
                   universe=list(self.mandate.universe_whitelist)),
            universe=self.mandate.universe_whitelist,
            asset_classes={},
        ).to_dict()
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

    def set_fast_mode(self, enabled: bool) -> dict:
        """Trade depth for latency on the judgment roles, at runtime.

        Bounded in one place that matters: REQUIRED_DEEP_ROLES keep their tier,
        so the referee is never cheapened. A PASS must mean the same thing on a
        fast desk as on a slow one, or the gate is decorative.
        """
        from qlab.operator.model_routing import REQUIRED_DEEP_ROLES

        self.fast_mode = bool(enabled)
        self.registry.record_event("workforce_fast_mode",
                                   {"enabled": self.fast_mode})
        return {
            "fast": self.fast_mode,
            "exempt_roles": sorted(REQUIRED_DEEP_ROLES),
            "effect": (
                "judgment roles run on the quick model; "
                f"{', '.join(sorted(REQUIRED_DEEP_ROLES))} keeps its tier"
                if self.fast_mode
                else "every role runs on its configured tier"),
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

    def adjudicate_debate(self, debate_id: str, body: dict) -> dict:
        """Close a debate with a human adjudication.

        Until this existed, `adjudicate()` had no caller anywhere: a debate
        could be opened but never closed, and `update_workflow_phase` refuses to
        start the reporter while any debate on its workflow is open. An opened
        debate was therefore a permanent deadlock with no exit — the run could
        neither finish nor be finished.

        Adjudication is a human act, exposed here and not to any agent. A role
        that could close its own debate would make the bounded-debate gate
        decorative.
        """
        from qlab.governance.debate import DebateViolation, adjudicate

        resolution = str(body.get("resolution") or "").strip()
        if not resolution:
            raise ValueError("adjudication requires a resolution")
        positions = body.get("winning_claim_positions")
        if not isinstance(positions, dict) or not positions:
            raise ValueError(
                "adjudication requires winning_claim_positions: "
                "{claim: position} for every material claim")
        try:
            return adjudicate(
                self.registry, debate_id,
                decided_by=str(body.get("decided_by") or "operator"),
                resolution=resolution,
                winning_claim_positions=positions,
                evidence_refs=list(body.get("evidence_refs") or []),
                amended_decision_id=body.get("amended_decision_id"))
        except DebateViolation as exc:
            raise ValueError(str(exc)) from exc

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
        from qlab.state.registry import agent_for_phase

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
        # Registering the workflow is not running it. Its phases advance only
        # when a coordinator walks them, so dispatch alone left the run parked at
        # phase one forever. Drive it here.
        # The graph's roles, not its phases: which provider serves a dispatch
        # is a per-role route, and `agent_for_phase` is the one place a phase
        # becomes a role.
        driven = self.drive_workflow(
            str(workflow_id),
            f"[{template_id}] {template.purpose}",
            roles=tuple(agent_for_phase(phase) for phase in template.phases))
        # A workflow row is not a finding. Report the dispatch and let
        # AtlasSupervisor.reconcile_tasks resolve the task from the workflow's
        # own terminal state.
        return Dispatched(
            workflow_id=str(workflow_id),
            detail={"template_id": template_id, "action_taken": True,
                    "driving": driven.get("driving", False),
                    # Carried even on success: an undriven dispatch is still a
                    # valid dispatch, and the operator has to be able to see
                    # that it is waiting on them rather than on Claude. It is no
                    # longer waiting FOREVER — `drive_pending_tasks` picks this
                    # workflow up on a later beat once the slot frees — but the
                    # reason is what says whether the wait is on a busy slot or
                    # on something only they can fix.
                    "drive_reason": driven.get("reason", "")},
        )

    def start_template_workflow(self, template_id: str, goal: str,
                                offline: bool) -> dict:
        """Start one registered template's own graph, through the mode gate.

        This is what the desk manager reaches when it starts its own research,
        and it is the *same* gate the unattended beat passes: ``check_startable``
        answers first, so a research template starts at once and a
        plan-creating one starts only in Propose mode — and even then it ends
        at a checked plan that still needs a human approval to book.

        The network names a template, never a phase graph. ``start_workflow``
        still refuses ``phases`` from a body; the graph that runs here is the
        one the template itself declares, resolved in-process.

        The workflow is driven, because registering one is not running it: a
        row nobody walks is the parked-at-phase-one state this desk already
        learned to avoid. A refusal to drive is reported, not raised — the
        workflow exists and a later beat picks it up.
        """
        from qlab.operator.templates import check_startable
        from qlab.state.registry import agent_for_phase

        template_id = str(template_id or "").strip()
        # Raises TemplateNotAllowed (a PermissionError) naming the template and
        # the reason. The caller turns that into the refusal the operator reads.
        template = check_startable(template_id, self.atlas.mode,
                                   self.atlas_facts(offline))
        if not template.needs_coordinator or not template.phases:
            raise ValueError(
                f"{template_id!r} is deterministic and starts no workflow; it "
                "is assembled from owner facts — read the desk brief instead")
        text = f"[{template_id}] {goal.strip() or template.purpose}"
        started = self.start_workflow(
            {"kind": "portfolio_review", "goal": text, "started_by": "atlas"},
            phases=template.phases)
        workflow_id = str((started or {}).get("workflow_id") or "")
        if not workflow_id:
            raise RuntimeError(
                f"no workflow could be started for template {template_id!r}")
        driven = self.drive_workflow(
            workflow_id, text,
            roles=tuple(agent_for_phase(phase) for phase in template.phases))
        return {**started, "template_id": template_id,
                "driving": bool(driven.get("driving")),
                "drive_reason": str(driven.get("reason") or "")}

    def atlas_create_task(self, kind: str, reason: str) -> dict:
        """Write one Atlas task down, from a trigger kind or a template name.

        Creating is not starting. The row lands ``queued`` and ``start_task``
        remains the only thing that starts it, so this grants nothing the mode
        gate does not still answer for. One per template per trading day — the
        same dedupe shape every other proposal carries, so ``task_age`` can
        read the day out of it.

        A reason is required. A task nobody explained is one nobody can judge
        later, and the desk's whole record is built on being able to.
        """
        from qlab.operator.templates import TEMPLATES, TRIGGER_TEMPLATE

        kind = str(kind or "").strip()
        reason = str(reason or "").strip()
        if not kind:
            raise ValueError(
                "a task needs a kind: a trigger kind "
                f"({sorted(TRIGGER_TEMPLATE)}) or a registered template "
                f"({sorted(TEMPLATES)})")
        if not reason:
            raise ValueError(
                f"a task for {kind!r} needs a reason; an unexplained task "
                "cannot be judged later")
        template_id = TRIGGER_TEMPLATE.get(kind) or (
            kind if kind in TEMPLATES else "")
        if not template_id:
            raise ValueError(
                f"unknown task kind {kind!r}; name a trigger kind "
                f"({sorted(TRIGGER_TEMPLATE)}) or a registered template "
                f"({sorted(TEMPLATES)})")
        trading_date = date.today().isoformat()
        # Sweep before minting, exactly as `atlas_actionables` does. This is the
        # desk's second proposal minter; a minter that does not clean up is how
        # one set per template per day accumulates in the window the gate scans.
        self._expire_stale_proposals(trading_date)
        universe = ",".join(sorted(self.mandate.universe_whitelist))
        task_kind = f"proposal:{template_id}"
        dedupe = f"{task_kind}|{trading_date}|{universe}|{template_id}"
        existing = self.registry.get_atlas_task_by_dedupe(dedupe)
        if existing is not None:
            return {"task_id": str(existing["task_id"]),
                    "template_id": template_id,
                    "status": str(existing.get("status") or ""),
                    "created": False,
                    "reason": (f"today's task for {template_id} already "
                               "exists; one per template per day")}
        task_id = uuid.uuid4().hex[:16]
        if not self.registry.create_atlas_task(
                task_id, dedupe, task_kind,
                {"template_id": template_id, "reason": reason,
                 "asked_by": "atlas"},
                template_id, origin="proposal"):
            raise RuntimeError(
                f"task {dedupe!r} was created between its lookup and its "
                "insert; the id minted here was never stored")
        self.registry.record_event(
            "atlas_task_created",
            {"task_id": task_id, "template_id": template_id,
             "reason": self._bounded(reason, 500), "origin": "proposal"})
        return {"task_id": task_id, "template_id": template_id,
                "status": "queued", "created": True, "reason": reason}

    # -- owner-driven coordination -------------------------------------------
    @property
    def coordinator_driver(self):
        """The owner's single coordinator slot, built on first use."""
        # Double-checked: the fast path stays lock-free once built, because
        # coordinator_status() is on the snapshot path and runs every tick.
        if self._driver is None:
            with self._driver_lock:
                if self._driver is None:
                    self._driver = self._build_driver()
        self._driver.fast = self.fast_mode
        self._driver.workforce = self.llm_config.workforce
        return self._driver

    def _build_driver(self):
        """Construct the driver. Called once, under `_driver_lock`.

        `fast` and `workforce` are deliberately not passed here: the property
        re-reads them on each access so a Settings change lands on the next
        dispatch rather than the next owner restart.
        """
        from qlab.operator.coordinator import CoordinatorDriver
        from qlab.paths import workspace_root

        return CoordinatorDriver(
            runtime_url=f"http://127.0.0.1:{self.port}",
            cwd=workspace_root(),
            record_event=self.registry.record_event,
            offline=self.offline_default,
            # The owner IS the single writer, and the driver runs inside it, so
            # this is the same handle rather than a second one.
            registry=self.registry,
            backend_status=self._last_backend_reading,
        )

    def _last_backend_reading(self, name: str) -> tuple[bool | None, str]:
        """The last availability reading for one backend — never a probe.

        `coordinator_status()` runs on the snapshot path, under the dispatch
        lock, every tick. `llm_payload` reports the same way and for the same
        reason: probing here would block every other request behind a daemon
        that may be a network hop away. `None` means nothing has been probed
        yet, which is not a refusal — the run itself fails with the daemon's
        own sentence, which beats a pre-flight guess.
        """
        with self._llm_catalog_lock:
            cached = self._llm_catalog
        if cached is None:
            return None, ""
        _, catalog = cached
        for entry in catalog["backends"]:
            if entry["name"] == name:
                return bool(entry["available"]), str(entry["reason"])
        return None, ""

    def drive_workflow(self, workflow_id: str, goal: str,
                       roles: tuple[str, ...] = ()) -> dict:
        """Spawn a coordinator for a workflow this owner just registered."""
        return self.coordinator_driver.drive(workflow_id, goal, roles=roles)

    def running_research_workflow(self) -> dict | None:
        """The workflow this owner's one coordinator is walking, named.

        ``coordinator_status()`` answers "is something driving"; a refusal has
        to answer "driving *what*", or the operator is told no with nothing to
        look at and no way to decide whether to wait or interrupt.

        The template comes from the goal's own ``[template_id]`` stamp — the
        one ``atlas_workflow_runner`` writes — and falls back to the workflow
        kind, because a run a human started from the workforce view carries no
        template at all and "a workflow (id)" is still a name.
        """
        status = self.coordinator_status()
        if not status.get("driving"):
            return None
        workflow_id = str(status.get("workflow_id") or "")
        workflow = (self.registry.get_workflow(workflow_id)
                    if workflow_id else None) or {}
        goal = str((workflow.get("request") or {}).get("goal") or "")
        template = self._stamped_template(workflow)
        return {
            "workflow_id": workflow_id,
            "template": template or str(workflow.get("kind") or "") or "a workflow",
            "current_phase": str(workflow.get("current_phase") or ""),
            "goal": goal,
        }

    def coordinator_status(self) -> dict:
        """What the desk should say about unattended coordination."""
        driver = self.coordinator_driver
        ok, reason = driver.available()
        status = {
            "driving": driver.busy,
            "workflow_id": driver.current_workflow_id,
            "can_drive": ok,
            "reason": reason or driver.last_reason,
        }
        # A configured workforce the desk cannot honour is not a refusal any
        # more, so it would otherwise be visible only on invocation rows. Every
        # client renders `reason` only when `can_drive` is False, and this desk
        # CAN drive — so the fact needs its own field or it is silent. Carried
        # only when there is one: an absent key is what old clients already
        # tolerate, and an empty string would render as a blank line.
        note = driver.workforce_note()
        if note:
            status["note"] = note
        return status

    def drive_pending_tasks(self) -> list[dict]:
        """Walk a running task's workflow that nothing is currently walking.

        Registering a workflow is not running it. The owner drives one
        coordinator at a time, so an approval that lands while the slot is busy
        leaves a real workflow parked at phase one — and ``reconcile_tasks``
        never resolves it, because a run nobody walks never reaches a terminal
        state. This is what picks it up when the slot frees.

        It grants nothing. Only ``start_task`` binds a workflow to a task, and
        only a task the gate started is ``running``, so every workflow reachable
        here is one that already passed the mode check and (for a proposal) a
        human approval. Driving is resuming, never starting.

        One SPAWN per call, because the owner has one coordinator. The check
        that makes that safe is not the early return below — the beat runs on
        its own thread beside HTTP handlers that can also dispatch. It is
        ``CoordinatorDriver.drive``, which re-tests ``busy``, spawns and stores
        the session all under the driver's own lock, and refuses the second
        caller. The early return only spares the audit bus a skip row per beat
        while a run is in flight.

        And that idle slot is the OWNER's. A coordinator a human started from
        the classic TUI is a client-side process this flag cannot see, so a
        manual resume landing inside one beat window can put two coordinators on
        one graph. The window is one beat, nothing is granted that the human did
        not already have, and closing it properly needs a lease in the registry
        rather than a flag in a process.

        A refusal is not the end of the sweep. ``available()`` answers per
        graph — a one-role read served by a daemon that is down is refused while
        a claude review would start — so stopping at the first refusal would
        park every later candidate behind it, which is the state this method
        exists to remove. Refusals are attempted past, up to
        ``_DRIVE_ATTEMPTS_PER_SWEEP``; the return value carries every attempt.

        And a candidate the driver would refuse costs no attempt at all. That
        cap exists to bound audit rows, and ``available()`` is deterministic per
        graph in a stable environment: three parked one-role tasks whose daemon
        is down would otherwise consume the whole budget on the same three
        refusals every beat, and a fourth task that WOULD drive is never
        reached on any beat — the parked-forever state this method removes,
        rebuilt in bounded form. So the screen is asked first, without
        emitting, and the cap bounds real ``drive()`` calls.
        """
        from qlab.operator.atlas import (
            TASK_SCAN_WINDOW,
            WORKFLOW_RESOLVED_STATUSES,
        )

        if self.coordinator_status().get("driving"):
            return []
        attempted: list[dict] = []
        # Filtered in SQL, because no task row is ever deleted and finished
        # history must not decide whether live work is visible. Sorted here, and
        # the direction is the opposite of the registry's: the newest parked
        # approval first would retry a doomed young workflow ahead of an older
        # healthy one on every single beat. Oldest first is the queue order the
        # operator would expect anyway. The window is the one `reconcile_tasks`
        # uses, so the sweep never drives a task reconciliation cannot see.
        for task in sorted(
                self.registry.list_atlas_tasks(TASK_SCAN_WINDOW,
                                               status="running"),
                key=lambda row: str(row.get("created_at") or "")):
            workflow = self.registry.get_workflow(
                str(task.get("workflow_id") or ""))
            if workflow is None:
                # Two cases, one answer. A task that bound nothing belongs to a
                # deterministic template that concluded inline; a task whose
                # bound workflow has vanished is reconciliation's to fail.
                # Neither has a graph to walk. (`or ""` because `str(None)` is
                # the id "None", which would be looked up and not found — the
                # same answer by accident rather than on purpose.)
                continue
            if str(workflow.get("status") or "") in WORKFLOW_RESOLVED_STATUSES:
                continue
            # The row's own id, not the task's copy of it: they agree, and the
            # one that was just read is the one being driven.
            workflow_id = str(workflow["workflow_id"])
            # The roles are read from the step rows rather than re-derived from
            # their phases: `start_workflow` resolved them once, and the column
            # is what this graph was created with. Read here rather than at the
            # call below because the screen needs them first.
            roles = tuple(str(step["agent"])
                          for step in workflow.get("steps") or ())
            # The driver's own answer, asked without emitting anything. A
            # refusal here is not an attempt: `drive` would record an
            # `atlas_coordinator_skipped` row and spend one of the sweep's
            # three, which is what starved the fourth parked approval behind
            # three that could never move.
            ok, _ = self.coordinator_driver.available(roles)
            if not ok:
                continue
            driven = self.drive_workflow(
                workflow_id,
                # The workflow's own goal and its own graph. A blank goal and an
                # unnamed graph are both meaningful to the driver — the first
                # tells the coordinator nothing, the second routes every
                # one-role read to the claude coordinator — so neither may be a
                # default standing in for a row that has the answer.
                str((workflow.get("request") or {}).get("goal") or ""),
                roles=roles)
            attempted.append({"task_id": str(task["task_id"]),
                              "workflow_id": workflow_id,
                              # Reported rather than assumed: a refusal is not a
                              # drive, and a later beat has to try this again.
                              "driving": bool(driven.get("driving"))})
            if driven.get("driving"):
                # The slot is taken now. Anything else waits for a later beat.
                break
            if len(attempted) >= _DRIVE_ATTEMPTS_PER_SWEEP:
                break
        return attempted

    def atlas_run_startable(self, offline: bool, *, limit: int = 1) -> list[dict]:
        """Start the queued work Atlas's current mode already permits.

        Autonomy is a *convenience*, never an authority widening: every task
        still goes through ``start_task``, so mode checks, the retry budget,
        and the plan-creation boundary all apply unchanged. In Research mode
        this launches research and still cannot create a paper plan.

        Unattended work only: a proposal is a queued task the operator has yet
        to approve, so the beat passes over it.

        One research workflow at a time. While the owner's coordinator is
        walking a graph, a trigger is left exactly where it is — ``queued`` —
        and named in the chat, rather than spawning a second coordinator or
        registering a workflow row nothing will walk. Nothing is lost: the
        task keeps its place and a later beat starts it.

        Oldest first, which is why the candidate list is reversed:
        ``startable_tasks`` reads the registry's newest-first order, and
        starting the newest of a queue every beat is how the oldest waiting
        trigger never runs at all.
        """
        facts = self.atlas_facts(offline)
        running = self.running_research_workflow()
        started: list[dict] = []
        for candidate in reversed(self.atlas.startable_tasks(facts)):
            if len(started) >= limit:
                break
            if not candidate.get("startable"):
                continue
            if candidate.get("origin") != "trigger":
                # A proposal is started by the operator approving it, never by
                # the beat. This line IS the envelope.
                continue
            if running is not None:
                started.append({
                    "task_id": candidate["task_id"],
                    "template_id": candidate.get("template_id"),
                    "started": False, "state": "queued",
                    "blocked_by": "coordinator",
                    "reason": (f"a research workflow is already running: "
                               f"{running['template']} "
                               f"({running['workflow_id']})"),
                })
                self._announce_queued_task(started[-1], running)
                continue
            result = self.atlas.start_task(
                candidate["task_id"], facts, runner=self.atlas_workflow_runner)
            started.append({"task_id": candidate["task_id"],
                            "template_id": candidate.get("template_id"),
                            **{k: v for k, v in result.items()
                               if k in ("started", "completed", "blocked_by")}})
        return started

    def _announce_queued_task(self, entry: dict, running: dict) -> None:
        """Say once, in the chat, that a trigger is waiting on the slot.

        Once per (task, running workflow) pair, because the beat re-reaches
        the same queued task every thirty seconds and a line per beat is not
        an announcement, it is the noise this whole task exists to remove.
        The memo is process-local and deliberately so: it bounds chatter, it
        is not a record — the record is the task row, which never moved.

        The owner is threaded (invariant 9): the beat and an HTTP handler can
        both be in here, so the memo takes its own lock.
        """
        key = (str(entry.get("task_id") or ""), str(running.get("workflow_id") or ""))
        with self._queued_notice_lock:
            if key in self._queued_notice:
                return
            # Bounded: one entry per queued task per running workflow, and the
            # set is dropped when this process ends.
            self._queued_notice.add(key)
        self._record_atlas_reply(
            f"⚑ {entry.get('template_id') or 'work'} stays queued "
            f"({key[0][:8]}): {entry.get('reason')}. It starts when the slot "
            f"frees; nothing was lost.")

    def atlas_start_task(self, task_id: str, offline: bool) -> dict:
        """Start one queued Atlas task through the governed workflow runner.

        **This route is the approval.** The beat passes over proposal-origin
        tasks (`atlas_run_startable`), so nothing but a human reaches one — and
        that made the envelope positional: which route was hit was the only
        evidence anybody approved anything. The row below is what makes it
        structural, and it is written HERE rather than in the dispatcher so
        every caller of this method leaves it, not only the one path someone
        remembered to instrument.

        Written *before* the start, for `execute_plan`'s reason: an approval
        that follows the work it authorises reads as permission granted
        retroactively, and a start that raises leaves no trace of the asking at
        all. It records the approval and nothing more — whether the gate then
        started the task is `atlas_task_started`'s to say.

        Only a proposal. A trigger is work the desk raised for itself and may
        start unattended, so a row saying a human approved one would put a
        decision on the record that nobody made.
        """
        task = self.registry.get_atlas_task(task_id)
        if task is not None and str(task.get("origin") or "") == "proposal":
            self.registry.record_event("atlas_proposal_approved", {
                "task_id": task_id,
                "template_id": str(task.get("template_id") or ""),
                # What was approved, in the state it was approved in: an
                # approval of a task the gate then refuses for being spent is
                # a different record from one that started work.
                "task_status": str(task.get("status") or ""),
            })
        facts = self.atlas_facts(offline)
        return self.atlas.start_task(task_id, facts,
                                   runner=self.atlas_workflow_runner)

    def atlas_actionables(self, offline: bool) -> dict:
        """What Atlas would do next, ranked, with the gate's verdict on each.

        Every item is checked here AND again by ``start_task`` when it is
        approved. That is not redundant: the mode can change and a plan can
        appear between proposing and approving, and a proposal made in Research
        must not execute on a permit it no longer holds.

        Only the offered half is persisted **as a task**. A refusal has nothing
        to approve, so writing a task row for it would put work in the queue
        that the gate has already said may not run. The refusals are recorded
        on the audit row instead, which is what lets
        ``atlas_actionables_snapshot`` show them: a desk that silently omits
        what it will not do teaches the operator nothing about why.

        Called under the dispatch lock (every route but two is), so it takes no
        lock of its own — ``_LOCK`` is not reentrant.
        """
        from qlab.operator.atlas import STARTABLE_TASK_STATES
        from qlab.operator.templates import template_menu

        facts = self.atlas_facts(offline)
        mode = self.atlas.mode
        # The same clock `atlas_observe` hands the supervisor, so a proposal and
        # a trigger minted in one tick carry the same trading date. It is LOCAL
        # midnight while `startable_tasks` ages against UTC (`_utc_today`), so
        # for a few hours a day the two clocks disagree by up to one day — older
        # or younger depending on which side of UTC the zone sits — and one day
        # against `max_task_age_days = 5` cannot flip a fresh proposal to stale.
        # `_expire_stale_proposals` compares against this same local date so
        # the two ends of the proposal's life agree with each other.
        trading_date = date.today().isoformat()
        universe = ",".join(sorted(facts.get("universe", [])))
        self._expire_stale_proposals(trading_date)
        items: list[dict] = []
        # The gate's own refusals, kept whole. They mint no task, so the
        # snapshot has no row to compose them from — persisting them here is
        # the only way the panel can show what this desk would NOT do and why.
        # The spent-proposal refusal below is deliberately not one of these: it
        # HAS a row, and the snapshot reads that row's status for itself.
        refusals: list[dict] = []
        for entry in template_menu(mode, facts):
            item = dict(entry)
            item["task_id"] = None
            item["task_status"] = None
            if not entry["startable"]:
                refusals.append(dict(entry))
            else:
                task_id, status = self._proposal_task(
                    entry["template_id"], trading_date, universe)
                item["task_id"] = task_id
                item["task_status"] = status
                if status not in STARTABLE_TASK_STATES:
                    # The gate permits the template; today's proposal for it is
                    # spent. `start_task` refuses anything but queued or failed,
                    # so leaving `startable` True here would offer an approve
                    # button whose only possible answer is a 400.
                    item["startable"] = False
                    item["reason"] = (
                        f"today's proposal for {entry['template_id']} is "
                        f"already {status}; one task per template per day, so "
                        "the next one is tomorrow's")
            items.append(item)
        # Startable first, then the refusals; the gate's own order within each
        # half, which is the registry's declaration order.
        items.sort(key=lambda item: not item["startable"])
        self.registry.record_event(
            "atlas_actionables",
            {"mode": mode, "trading_date": trading_date,
             "offered": sum(1 for i in items if i["startable"]),
             # The count keeps its name and its type — a field that changes
             # JSON shape mid-history poisons every reader of the older rows —
             # and the refusals themselves travel beside it.
             "refused": sum(1 for i in items if not i["startable"]),
             "refusals": refusals})
        return {"trading_date": trading_date, "items": items}

    def _proposal_task(self, template_id: str, trading_date: str,
                       universe: str) -> tuple[str, str]:
        """Today's proposal for this template — its id AND its status.

        The status travels with the id because the caller's verdict depends on
        it. The dedupe key survives the whole trading day, so a proposal that
        has already been approved and started is still what this returns; an
        offer built from the template gate alone would call that startable and
        the approve it invites answers 400.

        The dedupe key keeps ``AtlasSupervisor``'s shape —
        ``kind|trading_date|universe|state_hash`` — because ``task_age`` reads
        the trading date out of it, and a key it cannot parse reads as
        age-unknown, which ``startable_tasks`` refuses: the very gate this
        proposal needs to pass.

        The kind is ``proposal:<template_id>``, deliberately outside
        ``_WORKFLOW_TRIGGERS``, so a proposal sitting unapproved never consumes
        the unattended desk's daily workflow budget.
        """
        kind = f"proposal:{template_id}"
        dedupe = f"{kind}|{trading_date}|{universe}|{template_id}"
        # By key, in SQL. A bounded scan answered "no such task" for a key that
        # exists as soon as the table was deeper than the window, and the
        # UNIQUE constraint then refused the insert that answer implied.
        existing = self.registry.get_atlas_task_by_dedupe(dedupe)
        if existing is not None:
            return str(existing["task_id"]), str(existing.get("status") or "")
        task_id = uuid.uuid4().hex[:16]
        if not self.registry.create_atlas_task(
                task_id, dedupe, kind, {"template_id": template_id},
                template_id, origin="proposal"):
            # Written between the lookup and the insert. The id just minted is
            # not the one that is stored, and returning it would hand the
            # operator an approve button for a task that does not exist.
            raise RuntimeError(
                f"proposal {dedupe!r} was created between its lookup and its "
                "insert; the id minted here was never stored")
        return task_id, "queued"

    def _expire_stale_proposals(self, trading_date: str) -> list[str]:
        """Retire proposals offered on an earlier trading day.

        A proposal answers "what should this desk do today", so yesterday's
        unapproved answer is not an answer to today's question. Nothing else
        expires one: `reconcile_tasks` resolves dispatched work and the age
        check in `startable_tasks` only *reports* staleness. Left queued, one
        set per template per day accumulates in the bounded window the gate
        scans, and a genuine trigger — still inside `max_task_age_days` —
        drops off the end of it.

        Bounded on purpose: one pass over the same window everything else
        reads, writing only to the rows that need it.

        Two call sites, and they cover different failures. Every mint sweeps
        first, so the pile-up can never outpace the cleanup — that includes
        `atlas_create_task`, the chat's own minter, which would otherwise be a
        second producer with no consumer. And the beat sweeps
        (`expire_stale_atlas_work`), because a desk that is asked nothing for a
        week mints nothing, and yesterday's unapproved answer must not still be
        sitting in today's queue because nobody happened to ask again.
        """
        # The supervisor's own parser and window, deliberately: one reader of
        # the dedupe key's shape, so a change to it cannot silently disagree
        # with `task_age` about which day a task belongs to.
        from qlab.operator.atlas import TASK_SCAN_WINDOW, _dedupe_trading_date

        today = trading_date[:10]
        expired: list[str] = []
        # Queued proposals only, in SQL. Filtering after the read left this
        # bounded by the very window it exists to protect: past 200 rows of
        # history the cleanup stopped seeing the rows it had to expire.
        # A trigger is never touched — it is a claim about a trading day and
        # keeps for `max_task_age_days`; expiring one here would disarm the
        # desk's own autonomy without saying so.
        for task in self.registry.list_atlas_tasks(
                TASK_SCAN_WINDOW, status="queued", origin="proposal"):
            day = _dedupe_trading_date(task.get("dedupe_key"))
            if day == today:
                continue
            self.registry.update_atlas_task(
                str(task["task_id"]), status="expired",
                error=f"offered on {day or 'an unreadable date'} and not "
                      f"approved before {today}; ask again to reopen it")
            expired.append(str(task["task_id"]))
        if expired:
            self.registry.record_event(
                "atlas_proposals_expired",
                {"task_ids": expired, "trading_date": today})
        return expired

    def expire_stale_atlas_work(self, today: str | None = None) -> dict:
        """Retire work that has outlived the question it answered.

        Two kinds, both marked and neither deleted — the record of what the
        desk once wanted is worth keeping; offering it as live work is not.

        * A queued *trigger* is a claim about one trading day. Past
          ``max_task_age_days`` it describes a portfolio that has moved, and
          ``startable_tasks`` already refuses it for exactly that reason — but
          it refused it again every beat, forever, and fifty of them buried
          whatever else was queued. Marking them ``expired`` is the refusal
          made once. If the condition still holds the observe tick fires it
          again under today's date.
        * A workflow with no phase progress in a week is not in flight. It is
          marked ``stale`` (a resolved state, so reconciliation frees the task
          bound to it and the drive sweep stops re-walking it) and kept.

        Idempotent by construction: both passes read only rows in a live state,
        and both write a state that is not live.
        """
        from qlab.operator.atlas import TASK_SCAN_WINDOW, _utc_today

        day = (today or _utc_today())[:10]
        cutoff_days = self.atlas.config.max_task_age_days
        expired: list[str] = []
        for task in self.registry.list_atlas_tasks(
                TASK_SCAN_WINDOW, status="queued", origin="trigger"):
            age = self.atlas.task_age(task, day)
            # Strictly True. An unreadable trading date is age-*unknown*, which
            # `startable_tasks` already refuses; expiring it would retire a row
            # on a guess about how old it is.
            if age.get("stale") is not True:
                continue
            task_id = str(task["task_id"])
            self.registry.update_atlas_task(
                task_id, status="expired",
                error=(f"older than the {cutoff_days}-day cutoff: this trigger "
                       f"fired on {age['trading_date']}, {age['age_days']} days "
                       f"before {day}, and no longer describes this book"))
            expired.append(task_id)
        if expired:
            self.registry.record_event(
                "atlas_tasks_expired",
                {"task_ids": expired, "cutoff_days": cutoff_days, "as_of": day})
        # Proposals age out on a different clock — a proposal answers "what
        # should this desk do TODAY", so yesterday's is not an answer at all.
        # Their own sweep already knows that rule; this is what runs it on a
        # desk nobody asks, where nothing mints and so nothing swept.
        expired_proposals = self._expire_stale_proposals(date.today().isoformat())
        stale = self.registry.mark_idle_workflows_stale(
            f"no phase progress in {_WORKFLOW_IDLE_STALE_DAYS} days",
            updated_before=(datetime.now(timezone.utc)
                            - timedelta(days=_WORKFLOW_IDLE_STALE_DAYS)
                            ).isoformat())
        return {"expired_tasks": expired,
                "expired_proposals": expired_proposals,
                "stale_workflows": [str(row["workflow_id"]) for row in stale]}

    def atlas_task_rows(self, limit: int = 10) -> list[dict]:
        """Trigger tasks the desk should still show, newest first.

        Expired rows are dropped rather than drawn. This window is what the
        classic TUI renders as OPEN TASKS and RECENT TASKS, so a retired
        trigger appearing in it reads as work still waiting — and fifty of them
        would take every row of a ten-row list. They stay in the registry and
        the system card counts them; they just do not stand in for live work.
        """
        from qlab.operator.atlas import TASK_SCAN_WINDOW

        rows = self.registry.list_atlas_tasks(TASK_SCAN_WINDOW, origin="trigger")
        return [row for row in rows
                if str(row.get("status") or "") != "expired"][:limit]

    def _asked_refusals(self) -> tuple[str, list[dict]]:
        """The newest ask's own refusals, and the trading day it was asked on.

        Read from the audit row ``atlas_actionables`` writes, because a refused
        candidate mints no task and there is no other record of it. Replayed
        rather than recomposed: this surface may not call ``atlas_facts``, so
        the only refusal it can show is the one the gate already made — with
        the day it was made on beside it, which is what says how old the answer
        is.

        Absence is not an error at any step. A desk nobody has asked, and an
        ask recorded before this payload carried its refusals, both answer
        "nothing to add" rather than raising or inventing a verdict.
        """
        rows = self.registry.read_events_of_kind("atlas_actionables", limit=1)
        if not rows:
            return "", []
        payload = rows[-1].get("payload")
        if not isinstance(payload, dict):
            return "", []
        day = str(payload.get("trading_date") or "")[:10]
        refusals = payload.get("refusals")
        if not isinstance(refusals, list):
            return day, []
        return day, [entry for entry in refusals if isinstance(entry, dict)]

    def atlas_actionables_snapshot(self) -> dict:
        """The newest proposal set, read from the task table — never composed.

        A snapshot must not mint proposals as a side effect of being drawn.
        `/api/tui` is polled every two seconds, so composing the menu here
        would write a task row per startable template per poll and turn the
        desk's queue into a record of nobody having asked anything.

        **``startable`` is never True here.** This surface must not call
        ``atlas_facts`` — ``_atlas_regime_facts`` latches ``_last_robust_state``,
        so a two-second poll would consume every regime flip before the observe
        tick saw it — and without facts the data preconditions cannot be
        checked. What it CAN establish without them it does check, and reports
        as an outright refusal: the mode (``check_authority``), the age
        (``task_age``), the task's own status, and whether the template still
        exists. Everything else comes back ``startable: None`` with a reason
        saying where the verdict lives. Unknown must never read the same as
        permitted — the same reason ``task_age`` reports an unreadable date as
        ``None`` rather than False, and the same reason ``news_search`` carries
        no ``offer`` field rather than assert one it did not compute.

        **The ask's own refusals are merged in, with no task.** A candidate the
        gate refused mints nothing, so composing this from proposal rows alone
        showed the operator only the half the desk agreed to — in Research that
        silently drops every plan-creating template from the panel. They arrive
        as ``startable: False`` with ``task_id: None``: the ask's verdict,
        replayed. It is the answer as of the ask rather than as of now, which
        is why the block carries the day it was asked on; a refusal cannot
        become an approve affordance in the meantime, so a stale one errs on
        the side the gate already chose.
        """
        # The supervisor's own clock and parser: this surface's age answer must
        # be the one `startable_tasks` would give, not a second opinion.
        from qlab.operator.atlas import (
            STARTABLE_TASK_STATES, TASK_SCAN_WINDOW, _dedupe_trading_date,
            _utc_today)
        from qlab.operator.templates import (
            TEMPLATES, TemplateNotAllowed, check_authority)

        rows = self.registry.list_atlas_tasks(
            TASK_SCAN_WINDOW, origin="proposal")
        asked_day, asked_refusals = self._asked_refusals()
        if not rows and not asked_refusals:
            # Absence, not an error: nobody has asked yet.
            return {"trading_date": None, "items": []}
        newest = max([_dedupe_trading_date(row.get("dedupe_key"))
                      for row in rows] or [""])
        # An ask that offered nothing mints no row at all, so the newest ask can
        # be newer than the newest proposal. Its refusals are still the answer
        # to "what would this desk do today", and yesterday's rows are not.
        newest = max(newest, asked_day)
        mode = self.atlas.mode
        today = _utc_today()[:10]
        items: list[dict] = []
        for row in rows:
            if _dedupe_trading_date(row.get("dedupe_key")) != newest:
                continue
            template_id = str(row.get("template_id") or "")
            status = str(row.get("status") or "")
            template = TEMPLATES.get(template_id)
            item = {
                "template_id": template_id,
                "purpose": template.purpose if template else "",
                "creates_plan": bool(template.creates_plan) if template else False,
                "needs_coordinator": (
                    bool(template.needs_coordinator) if template else False),
                "task_id": str(row["task_id"]), "task_status": status,
                "startable": None, "reason": None,
            }
            if template is None:
                # A release can unregister a template while a proposal for it
                # is queued. Dropping the row would remove an item from the
                # client with nothing said; raising would take the whole
                # snapshot — the entire desk — down for one dead row.
                item["startable"] = False
                item["reason"] = (f"{template_id!r} is no longer a registered "
                                  "template; this proposal cannot start")
            elif status not in STARTABLE_TASK_STATES:
                item["startable"] = False
                item["reason"] = (
                    f"today's proposal for {template_id} is already {status}; "
                    "one task per template per day, so the next one is "
                    "tomorrow's")
            else:
                age = self.atlas.task_age(row, today)
                if age["stale"] is not False:
                    item["startable"] = False
                    item["reason"] = age["age_reason"]
            if item["startable"] is None:
                try:
                    check_authority(template_id, mode)
                except TemplateNotAllowed as exc:
                    item["startable"] = False
                    item["reason"] = str(exc)
                else:
                    item["reason"] = (
                        "the data preconditions were not checked here; POST "
                        "/api/atlas/actionables asks the gate for today's "
                        "verdict")
            items.append(item)
        if asked_day and asked_day == newest:
            # Only the templates no row already speaks for. A day can hold two
            # asks in two modes — the first mints a proposal, the second
            # refuses the same template — and the row is the better account of
            # the two: it carries the task an approval would bind to, and the
            # loop above re-checks the mode against it.
            spoken = {item["template_id"] for item in items}
            for entry in asked_refusals:
                template_id = str(entry.get("template_id") or "")
                if not template_id or template_id in spoken:
                    continue
                items.append({
                    "template_id": template_id,
                    "purpose": str(entry.get("purpose") or ""),
                    "creates_plan": bool(entry.get("creates_plan")),
                    "needs_coordinator": bool(entry.get("needs_coordinator")),
                    # No task, and that is the fact rather than a gap: the gate
                    # refused it, so there is nothing queued to approve.
                    "task_id": None, "task_status": None,
                    "startable": False,
                    # The gate's own sentence. A refusal a client cannot read
                    # is not one an operator can act on, so a refusal that
                    # arrived without one says which silence it met.
                    "reason": str(entry.get("reason") or "").strip() or (
                        f"the desk refused {template_id} when it was asked and "
                        "did not say why"),
                })
        # Known-refused last, exactly as the menu orders its own refusals.
        items.sort(key=lambda item: item["startable"] is False)
        return {"trading_date": newest or None, "items": items}

    @staticmethod
    def _bounded(text: str, limit: int) -> str:
        """`text` capped at `limit`, saying so when the cap bites.

        An audit row that silently ends mid-sentence reads as what was said.
        The model receives the operator's question whole, so a row that cut it
        without a mark would be a record disagreeing with the prompt it
        produced, with nothing to show which one was short.
        """
        if len(text) <= limit:
            return text
        return f"{text[:limit]} …[truncated from {len(text)} chars]"

    def announce_desk_work(self, offline: bool,
                           created_tasks: list[dict] | None = None) -> dict:
        """Say in the chat what the desk now wants from the operator, or just did.

        Two announcements, each bounded to once per fact, both on the same
        `atlas_message` kind the console already draws:

        * A checked plan with no approval request gets one opened here, and
          the chat names the two words that answer it. The reporter's checked
          preview IS the desk asking — but nothing opened the request, so a
          plan sat "awaiting approval" with no way to give one, and BOOK's
          `x` returned in silence for want of a covering approval. Any prior
          request (pending, approved, consumed, expired) means the desk has
          already asked; it asks once.
        * The desk holds ONE open question. Once the newest checked plan has
          its request, every older live one — pending, and approved but never
          booked — is invalidated with a reason naming its successor, and the
          chat says so, naming the state it withdrew. Once per superseded
          plan, because a second pass finds those rows already terminal and
          has nothing left to name.
        * A live request whose plan is no longer `checked` is withdrawn with
          its own reason, once. Supersession cannot reach it: that runs only
          when there IS a current proposal, and a request on a plan refused
          after it opened leaves the desk with none.
        * A trigger task minted this tick is named with its reason and the
          template the lookup maps it to. `created_tasks` is per-tick and
          deduped upstream, so a strong indicator is announced once.
        """
        from qlab.governance.proposal import (
            ORPHAN_REASON,
            current_proposal,
            live_requests,
            supersede,
            withdraw_orphans,
        )
        from qlab.operator.templates import TRIGGER_TEMPLATE

        announced: dict = {"approvals_opened": [], "superseded": [],
                           "supersede_failures": [], "orphans_withdrawn": [],
                           "orphan_failures": [], "triggers": []}
        asked = {str(a.get("plan_id") or "")
                 for a in self.registry.list_approval_requests(200)}
        for plan in self.registry.list_plans(20):
            pid = str(plan.get("plan_id") or "")
            if not pid or plan.get("state") != "checked" or pid in asked:
                continue
            try:
                opened = self.create_approval({"plan_id": pid}, offline)
            except (KeyError, ValueError, RuntimeError) as exc:
                self._record_atlas_reply(
                    f"⚑ Plan {pid[:8]} is checked but no approval could be "
                    f"opened: {exc}")
                continue
            aid = str(opened.get("approval_id") or "")
            pre = plan.get("pre_trade") or {}
            turnover = pre.get("turnover")
            shape = (f"{pre.get('n_legs', '?')} legs, turnover {turnover:.1%}"
                     if isinstance(turnover, (int, float)) else "checked")
            self._record_atlas_reply(
                f"⚑ Plan {pid[:8]} is checked ({shape}). Approval {aid[:8]} "
                f"is open — in chat: `/approve {aid[:8]}` to approve it, then "
                f"`/execute {pid[:8]}` to book it; each opens the confirm box.")
            announced["approvals_opened"].append(aid)
        # Orphans first, and unconditionally: a live request whose plan is no
        # longer checked has no keeper, so the supersession below never reaches
        # it — that runs only when there IS a current proposal, and an orphan
        # is exactly the case where there is none. It carries its own reason
        # rather than `superseded by …`, because nothing replaced it.
        orphans, orphan_failures = withdraw_orphans(self.registry)
        for row in orphans:
            self._record_atlas_reply(
                f"\u2691 Plan {row['plan_id'][:8]} is {row['plan_state']}, not "
                f"checked; approval {row['approval_id'][:8]} "
                f"({row['status'] or 'live'}) is invalidated as "
                f"{ORPHAN_REASON} \u2014 it can no longer book anything.")
        for failed in orphan_failures:
            self._record_atlas_reply(
                f"\u2691 Approval {failed['approval_id'][:8]} for plan "
                f"{failed['plan_id'][:8]} ({failed['plan_state']}, not checked) "
                f"could not be withdrawn: {failed['error']}. It is still "
                f"{failed['status'] or 'live'} \u2014 reject it by hand.")
        announced["orphans_withdrawn"].extend(row["plan_id"] for row in orphans)
        announced["orphan_failures"].extend(orphan_failures)
        # One proposal. Run every tick, not only when a request was opened
        # here: a desk that came up holding two live requests has two open
        # questions and nothing else would ever close the older one.
        proposal = current_proposal(self.registry)
        if proposal is not None:
            keep = str(proposal["plan_id"])
            # The state each is in BEFORE the withdrawal, because that is what
            # the chat line has to name: an approved-but-unbooked allocation
            # going away is a bigger fact than a pending one going away, and
            # after the transition every row reads `invalidated`.
            was = {plan_id: str(row.get("status") or "")
                   for plan_id, row in live_requests(self.registry).items()}
            gone, failures = supersede(self.registry, keep)
            for old in gone:
                state = ("approved, unbooked" if was.get(old) == "approved"
                         else was.get(old) or "live")
                self._record_atlas_reply(
                    f"⚑ Plan {keep[:8]} supersedes {old[:8]} ({state}): one "
                    f"proposal at a time. The older approval is invalidated "
                    f"as superseded — it can no longer book anything.")
            # Separately, and never merged into the line above: a request the
            # desk failed to withdraw is still bookable, which is the opposite
            # fact and the operator has to be able to act on it.
            for failed in failures:
                self._record_atlas_reply(
                    f"⚑ Plan {keep[:8]} is the current proposal but approval "
                    f"{str(failed['approval_id'])[:8]} for plan "
                    f"{str(failed['plan_id'])[:8]} could not be withdrawn: "
                    f"{failed['error']}. It is still "
                    f"{failed['status'] or 'live'} — reject it by hand.")
            announced["superseded"].extend(gone)
            announced["supersede_failures"].extend(failures)
        for task in created_tasks or []:
            kind = str(task.get("trigger") or "trigger")
            action = str(task.get("action") or "")
            tid = str(task.get("task_id") or "")[:8]
            template = TRIGGER_TEMPLATE.get(kind)
            what = (f"queued `{template}`" if template and action != "block"
                    else action or "noted")
            self._record_atlas_reply(f"⚑ {kind} fired — Atlas {what} ({tid}).")
            announced["triggers"].append(tid)
        return announced

    def _record_atlas_reply(self, text: str, error: str | None = None) -> None:
        """Put the desk's own words on the bus as a second `atlas_message` row.

        The existing kind, deliberately. The workstation already renders it —
        the Rust console keys `atlas_message` in `CONSOLE_KINDS` and reads
        `text` as the row's subject — so a reply arrives in front of the
        operator with no client change at all. A new kind would have been an
        answer nothing displays.
        """
        payload = {"actor": "atlas",
                   "text": self._bounded(text, _ATLAS_REPLY_CHARS)}
        if error is not None:
            payload["error"] = self._bounded(error, 500)
        self.registry.record_event("atlas_message", payload)

    def atlas_message(self, body: dict, offline: bool) -> dict:
        """Answer the operator through the configured reasoner.

        This never grants authority. The reply is words on a bus: no tool, no
        plan, no approval, and no path to one. What makes that safe is not
        instruction but absence — the model is handed a context and a question
        and its answer is recorded, exactly as `atlas_context`'s own docstring
        splits the reasoning surface from the gate's nine booleans.

        Which model answers is `llm_config.reasoner`, and it answers whenever
        its backend can serve. `reasoner_enabled` is NOT consulted: that flag
        gates Atlas's template judgment (what the desk starts unattended), and
        gating a question the operator typed behind it would withhold an answer
        to protect an authority the answer does not carry.

        **Must not be called while the dispatch lock is held.** The model call
        is the longest network wait the owner makes on a request — up to
        `_ATLAS_REPLY_TIMEOUT_S` — and it runs outside the lock, which is taken
        twice and briefly around the registry work instead. `_Handler.do_POST`
        routes this path accordingly; `_LOCK` is not reentrant.
        """
        text = str(body.get("text") or "").strip()
        if not text:
            raise ValueError("message text is required")
        choice = self.llm_config.reasoner

        # The question goes on the record before anything can fail. The model
        # gets it whole; the row is bounded and says when it was cut, because a
        # record that quietly disagrees with the prompt it produced is worse
        # than a long one.
        with _LOCK:
            self.registry.record_event(
                "atlas_message",
                {"actor": "operator", "text": self._bounded(text, 500)})

        # Outside the lock: the catalog probes the network, and it is the one
        # place availability is asked, so the refusal carries the same sentence
        # the picker shows rather than a second opinion composed here.
        entries = {entry["name"]: entry
                   for entry in self.llm_backends_catalog()["backends"]}
        entry = entries.get(choice.backend)
        if entry is None or not entry["available"]:
            reason = (entry["reason"] if entry else
                      f"this desk has no {choice.backend!r} backend")
            return self._atlas_refusal(choice, reason)

        from qlab.operator.llm_backends import LlmBackendError, build_backend

        # Composed only once a model exists to read it. `atlas_context` costs a
        # regime panel under the dispatch lock, and paying that for a desk whose
        # reasoner is down would make the one unanswerable question the most
        # expensive request the owner serves.
        with _LOCK:
            context = self.atlas_context(offline)
        user = (
            "The desk right now, as JSON:\n\n"
            # Compact: this is ~12KB of context and every separator is a token.
            f"{json.dumps(context, default=str, separators=(',', ':'))}\n\n"
            f"The operator asks:\n\n{text}")
        try:
            reply = build_backend(choice.backend).complete(
                system=_ATLAS_DESK_MANAGER_PROMPT, user=user,
                model=choice.model, max_tokens=_ATLAS_REPLY_TOKENS,
                timeout=_ATLAS_REPLY_TIMEOUT_S)
        except LlmBackendError as exc:
            # A model that was asked and could not answer is a desk event, not
            # a 500: the operator asked a question, and "I could not answer,
            # here is why" is an answer. Only LlmBackendError — a bug in this
            # method must still surface as one.
            return self._atlas_refusal(choice, str(exc), asked=True)

        with _LOCK:
            self._record_atlas_reply(reply)
        return {
            "received": True,
            "answered": True,
            "backend": choice.backend,
            "model": choice.model,
            # The same bound and the same marker as the row this mirrors: an
            # HTTP caller and the bus must not be shown two different cuts of
            # one answer, one of them silent.
            "reply": self._bounded(reply, _ATLAS_REPLY_CHARS),
            "note": f"{choice.backend} {choice.model} answered on the console",
        }

    def _atlas_refusal(self, choice: SurfaceModel, reason: str, *,
                       asked: bool = False) -> dict:
        """Record and report that the desk could not answer, and why.

        Never a fabricated reply: what lands on the bus is the desk's own
        sentence about its own failure, carrying the backend's reason verbatim.

        Both notes say "unavailable" because the Rust client reads that word to
        raise its toast from Info to Warn — a degraded desk must not render as
        a receipt (`toast.rs`), and a backend that was asked and failed is as
        unable to answer as one that was never reachable.
        """
        note = (f"the reasoner is unavailable: {choice.backend} "
                f"{choice.model} was asked and failed — {reason}"
                if asked else
                f"the reasoner is unavailable; Atlas is degraded and cannot "
                f"answer, but the owner, data, and book remain usable — "
                f"{reason}")
        with _LOCK:
            self._record_atlas_reply(note, error=reason)
        return {
            "received": True,
            "answered": False,
            "backend": choice.backend,
            "model": choice.model,
            "note": note,
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

    def actionable_approvals(self, limit: int = _SNAPSHOT_APPROVALS) -> list[dict]:
        """The approvals a client can still act on: pending, and approved-unspent.

        Both statuses, because they answer different keys. A *pending* request
        is what approve/reject bind to; an *approved, unconsumed* one is what
        the execute gate consumes (``execute_plan_with_approval``). A snapshot
        carrying only the pending queue could never show a client the record a
        legal execution binds to — the approval would be invisible from the
        moment it became usable.

        The cap is per status rather than shared. ``list_approval_requests``
        filters one status at a time, and merging the two under a single limit
        would let a busy pending queue crowd out the one approval that can
        actually be executed.

        ``consumed_at`` is filtered rather than inferred. The transition table
        already implies it — consumption moves the row to ``consumed`` — but
        this list is read as "what the gate would accept", and it should state
        that precondition rather than rely on a second table to imply it.
        """
        rows = self.registry.list_approval_requests(limit, "pending")
        rows += [row for row
                 in self.registry.list_approval_requests(limit, "approved")
                 if not row.get("consumed_at")]
        # Newest first across both, as every other list in the payload is.
        rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        return rows

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

        policy = self.data_policy(offline)
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

    def book_current_proposal(self, body: dict, offline: bool) -> dict:
        """Approve the desk's own open question and execute it, once.

        Composed from the two existing seams — ``decide_approval`` then
        ``execute_plan_with_approval`` — and adds no execution primitive. What
        it removes is the gap between them: approving and booking used to be
        two calls, so a desk could be left holding an approved-but-unbooked
        plan that nothing on screen was still asking about.

        Every refusal lands *before* any transition. The order is deliberate:
        the confirmation, then identity (is this still the desk's question),
        then the hash the confirm box bound to, then the referee. A caller
        that fails any of them has changed nothing.

        The approval this grants is the one the execute gate then consumes, so
        the two steps must not be separated by a lock release — they are not.
        The whole route runs under the owner dispatch lock (`_LOCK`) exactly as
        ``/api/plans/execute`` does, and this method takes no lock of its own
        because `_LOCK` is not reentrant.

        On the *simulated* book the broker is in-process, so holding `_LOCK`
        across the fill costs a dispatch turn and nothing else. On an Alpaca
        book it is venue I/O under the lock — inherited, not introduced here:
        ``/api/plans/execute`` has always held `_LOCK` across the same call,
        and this route reaches the same ``execute_plan_with_approval``. Adding
        the approve step in front of it lengthens that hold by one registry
        write. If the lock is ever narrowed for the Alpaca book, both routes
        move together, and whatever replaces it must still guarantee that the
        approval granted here is the one the execute gate reads.
        """
        from qlab.governance.proposal import current_proposal

        # Exactly True. "yes", 1 and [] are a client bug or a smuggled
        # confirmation; neither is a human at a confirm box (invariant 3).
        if body.get("human_confirmed") is not True:
            raise ValueError("human_confirmed=true is required")
        plan_id = str(body.get("plan_id") or "")
        supplied_hash = str(body.get("targets_hash") or "")

        # Sweep first: a request past its expiry is not live, and reading the
        # stored status without sweeping would let a lapsed `pending` row look
        # like an open question for as long as nothing else asked.
        self.registry.expire_due_approvals(self._now_iso())
        proposal = current_proposal(self.registry)
        if proposal is None or str(proposal.get("plan_id") or "") != plan_id:
            # Covers a superseded plan, a consumed one, and a plan that never
            # had a request: in each case the desk is not asking about it.
            raise ValueError("not the current proposal")
        if supplied_hash != str(proposal.get("targets_hash") or ""):
            # Not a near-miss to repair: a different hash means the operator
            # confirmed a different allocation from the one on the record.
            raise ValueError("targets_hash does not match the plan")
        referee = proposal.get("referee") or {}
        if referee.get("verdict") != "PASS":
            raise ValueError(
                f"no referee PASS covers targets_hash {supplied_hash}")

        approval_id = str(proposal.get("approval_id") or "")
        status = str(proposal.get("approval_state") or "")
        if status == "pending":
            # The same decide_approval the two-call path takes — one
            # `approval_approved` row, same digest and book-revision binding.
            self.decide_approval(approval_id, "approve")
        elif status != "approved":
            raise ValueError(f"the approval request is {status!r}, not live")

        # No `human_confirmed` in this body: the gate consumes the approval
        # record, never a boolean, and passing one would suggest the callee
        # reads it. The confirmation was checked at the top of this method.
        try:
            result = self.execute_plan_with_approval(
                plan_id, {"approval_id": approval_id}, offline)
        except Exception as exc:
            # An approval granted a moment ago and never consumed is live
            # authority to book. An exception on the way to the broker — the
            # incomplete-legs RuntimeError, say — would otherwise leave it
            # spendable against a plan that just proved it cannot execute.
            # Withdraw it naming the fault, then re-raise: the caller still
            # gets the failure, and a transition that fails here chains onto
            # it rather than replacing it.
            self.registry.transition_approval(
                approval_id, "invalidated",
                invalidated_reason=f"execution raised: {exc}"[:200])
            raise
        booked = result.get("executed") is True
        if booked:
            # Only a fill is a booking. Recording `proposal_booked` for a
            # refused execution would forge the one audit line that says the
            # desk acted.
            self.registry.record_event("proposal_booked", {
                "plan_id": plan_id, "targets_hash": supplied_hash,
                "approval_id": approval_id})
        return {"booked": booked, "execution": result,
                "approval_id": approval_id}

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

    def _run_summary_cached(self, key: str, build):
        """TTL-cache one summary of the runs table, exact against new runs.

        Built under the lock, exactly as ``archive_summary`` is: two handler
        threads arriving cold should wait for one scan, not run two. The value
        is deep-copied out, because these payloads reach handlers and the
        convention there is replace-never-mutate.
        """
        import copy

        revision = self.registry.run_revision
        with self._research_lock:
            hit = self._research_cache.get(key)
            if (hit is not None and hit[1] == revision
                    and (time.monotonic() - hit[0]) < _RESEARCH_TTL_SECONDS):
                return copy.deepcopy(hit[2])
            value = build()
            self._research_cache[key] = (time.monotonic(), revision, value)
            return copy.deepcopy(value)

    def latest_equilibrium_returns(self) -> dict | None:
        """Compact summary of the newest persisted equilibrium research run.

        TTL-cached against the registry's run revision: it scans a thousand run
        rows and every /api/tui poll asks for it under the dispatch lock.
        """
        return self._run_summary_cached(
            "equilibrium_returns", self._latest_equilibrium_returns)

    def _latest_equilibrium_returns(self) -> dict | None:
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
        TTL-cached against the run revision: the poll path used to pay for this
        twice, once here and once through ``leaderboard``.
        """
        return self._run_summary_cached(
            "ablation_metrics", self._latest_ablation_metrics)

    def _latest_ablation_metrics(self) -> dict[str, dict]:
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
            "posture": self.posture_payload(),
            "llm": self.llm_payload(),
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
                # Whether a coordinator is walking a workflow's phases right
                # now. Every client needs this to answer "is Claude working?",
                # which the presence of a workflow row does not answer.
                "coordinator": self.coordinator_status(),
                "fast": self.fast_mode,
            },
            "news": self.news_payload(offline),
            # Trigger work only. This key is what the classic TUI draws as
            # OPEN TASKS and RECENT TASKS, under a heading whose empty state
            # reads "no autonomous tasks recorded" and beside an AUTHORITY
            # panel about what Atlas starts unattended — so a proposal here
            # reads as open autonomous work that nobody authorised, and one ask
            # in Research fills the ten-row window and pushes real trigger work
            # off it. Proposals have their own block below.
            "atlas_tasks": self.atlas_task_rows(10),
            # The newest proposal set, read from the task table so the client
            # renders it without a second fetch — and so a poll never mints
            # one. Asking is what proposes; drawing is what reports. `startable`
            # here is False (known refused) or None (not checked on this
            # surface), never True: see `atlas_actionables_snapshot`.
            "actionables": self.atlas_actionables_snapshot(),
            "approvals": self.actionable_approvals(),
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
            # The same summary the reasoner is handed (atlas_context), so the
            # operator can see the evidence base the desk reasons from — the
            # quantum-augmented lane against its ridge control. Reads only
            # persisted run rows; never triggers a board run.
            "predictors": self.predictor_board_summary(),
            # The conversation, selected by kind at the store. The general
            # `events` window above cannot carry it: a news-archive poll writes
            # a row every 30s, so an hour of idling floods any fixed window and
            # the chat a client renders would silently end an hour back.
            "atlas_chat": self.registry.read_events_of_kind(
                "atlas_message", limit=60),
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
        """Read a bounded audit page in the merged stream's stable ordering.

        `news_archive` is selected out at the store, the way `atlas_chat` is
        selected in: a stack writes one row per member per heartbeat tick, and
        on a fixed-size page that pushes everything an operator is actually
        auditing off the end within minutes. The rows are untouched —
        `read_events_of_kind("news_archive", …)` still returns every one.
        """
        limit = max(1, min(int(limit), _STREAM_PAGE_CEILING))
        # Registry.read_events caps ordinary observers at 500 and orders only
        # by timestamp. The owner needs the full tuple order to page one dense
        # timestamp without adding another registry connection or writer.
        if after:
            return self.registry._rows(
                "SELECT * FROM events WHERE ts > ? AND kind <> 'news_archive' "
                "ORDER BY ts ASC, event_id ASC LIMIT ?",
                [after, limit],
            )
        return self.registry._rows(
            "SELECT * FROM ("
            "SELECT * FROM events WHERE kind <> 'news_archive' "
            "ORDER BY ts DESC, event_id DESC LIMIT ?"
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
    # This hook runs after every book-moving route, so it is also the one place
    # that has to drop a cached valuation — otherwise a fill could take up to
    # the TTL to appear, which is exactly the wrong thing to be slow about.
    session.invalidate_valuation()
    try:
        session.record_equity_mark(source, offline)
    except Exception as exc:  # the mutation's own result must still reach the client
        session.registry.record_event(
            "equity_mark_failed", {"source": source, "error": repr(exc)})


# ---------------------------------------------------------------------------
# API dispatch (pure functions of the session; easy to unit-test)
# ---------------------------------------------------------------------------
def _known_news_providers() -> list[str]:
    """Every provider name that can be written, first-party and plugin."""
    from qlab.news.feed import PROVIDERS, load_plugin_providers

    try:
        load_plugin_providers()
    except Exception:
        # A broken plugin entry point is not this route's error to raise: the
        # first-party names are still nameable, and the feed refuses the rest
        # with its own sentence.
        pass
    return sorted(PROVIDERS)


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

    if (method == "POST" and path.startswith("/api/debates/")
            and path.endswith("/adjudicate")):
        debate_id = path.removeprefix("/api/debates/").removesuffix("/adjudicate")
        try:
            return 200, session.adjudicate_debate(debate_id, body)
        except ValueError as exc:
            return 400, {"error": str(exc)}

    if method == "GET" and path == "/api/debates":
        # Open debates across every workflow: without this an operator has no
        # way to find the one blocking a reporter.
        return 200, {"debates": [
            d for d in session.registry.list_debates()
            if d.get("status") == "open"]}

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

    if method == "GET" and path == "/api/desk/proposal":
        # The one question the desk is asking, or None. Read-only: what makes
        # the older ones go away is the tick's supersede, so a client polling
        # this route never changes what it is looking at.
        from qlab.governance.proposal import current_proposal

        return 200, {"proposal": current_proposal(session.registry)}

    if method == "POST" and path == "/api/desk/proposal/book":
        # Client-only, deliberately: this route is NOT in OWNER_LAB_TOOLS, the
        # `qlab-operator` MCP proxy, or the combined MCP server, so no agent
        # surface can reach it. Booking is a human act at a confirm box bound
        # to the plan's own targets_hash; an agent-reachable one-call book is
        # exactly the execution path invariant 3 forbids.
        clamped_off = _offline_for_book(session, off)
        try:
            result = session.book_current_proposal(body, clamped_off)
        except KeyError as exc:
            return 404, {"error": str(exc)}
        except (ValueError, PermissionError) as exc:
            return 400, {"error": str(exc)}
        # `booked: false` is 200, and it is three different facts. Only one
        # of them means re-propose:
        #   execution.blocked_by == "approval"      -> the request was
        #       invalidated (book drift, expiry, a plan/targets mismatch). The
        #       authority is gone; the plan must be re-proposed.
        #   execution.blocked_by == "data_revalidation"  -> refused before the
        #       approval was touched. It is still approved and unspent: fix the
        #       data and book the same proposal again.
        #   execution.mandate_violation present     -> same, refused at
        #       submission with the approval intact; retrying is valid.
        # A client that reads every `booked: false` as "re-propose" throws away
        # a live approval in two cases out of three.
        #
        # Only a fill moved the book; a refused execution must not forge an
        # "execution"-sourced mark.
        if result.get("booked") is True:
            _mark_after_mutation(session, "execution", clamped_off)
        return 200, result

    if method == "GET" and path == "/api/desk/method":
        return 200, session.method_payload()

    if method == "POST" and path == "/api/desk/method":
        # A refusal here is the whole point of the route: the cap and the
        # method are mandate limits, so an invalid one must never reach disk
        # and leave the next start unable to load its own mandate.
        try:
            return 200, session.set_method(body)
        except ValueError as exc:
            return 400, {"error": str(exc)}

    if method == "POST" and path == "/api/desk/posture":
        # Arming a desk takes an explicit true: "yes", 1 and [] are refused
        # rather than read as consent (the ``replace`` precedent above).
        armed = body.get("armed")
        if not isinstance(armed, bool):
            return 400, {"error": "armed must be true or false"}
        return 200, session.set_posture(armed)

    if method == "POST" and path == "/api/alpaca/credentials":
        from qlab.trader.alpaca_auth import AlpacaAuthError, AlpacaConsentRequired

        # Destroying an existing login takes an explicit true, so a client that
        # sends "yes" or 1 is refused rather than read as consent.
        replace = body.get("replace", False)
        if not isinstance(replace, bool):
            return 400, {"error": "replace must be true or false"}
        try:
            return 200, session.set_alpaca_credentials(
                body.get("api_key"), body.get("api_secret"), replace=replace)
        except AlpacaConsentRequired as exc:
            # The one refusal a client can act on: show the sentence, and
            # re-POST with replace:true if the operator agrees. `confirm` is
            # what makes that a check rather than a substring sniff — the
            # message above says what would be lost and nothing about how,
            # and the validation refusal below deliberately has no such key.
            return 400, {"error": str(exc), "confirm": "replace"}
        except AlpacaAuthError as exc:
            # The module's own sentence, which never quotes what was typed.
            return 400, {"error": str(exc)}

    if method == "POST" and path == "/api/alpaca/test":
        # Network I/O. do_POST runs this one outside the dispatch lock; it
        # touches no registry state, so it does not need it.
        return 200, session.probe_alpaca_credentials()

    if method == "GET" and path == "/api/llm/backends":
        # Network I/O. do_GET runs this one outside the dispatch lock; nothing
        # here touches the registry, so it does not need it.
        return 200, session.llm_backends_catalog(
            refresh=_qbool(query, "refresh", False))

    if method == "POST" and path == "/api/llm":
        enabled = body.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            return 400, {"error": "enabled must be true or false"}
        # An absent backend/model means "leave the pair alone", which is what
        # makes {surface, enabled} a switch. Absent is not the same as empty:
        # an empty string is a choice of nothing and is refused below.
        backend, model = body.get("backend"), body.get("model")
        try:
            return 200, session.set_llm_config(
                str(body.get("surface") or ""),
                None if backend is None else str(backend),
                None if model is None else str(model),
                enabled)
        except ValueError as exc:
            return 400, {"error": str(exc)}

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
        status["autonomous"] = bool(session.autonomous)
        # Whether a coordinator is actually walking a workflow's phases. Without
        # this the desk could only show that a workflow existed, which is why an
        # unattended run read as "Claude is a black box doing nothing".
        status["coordinator"] = session.coordinator_status()
        return 200, status

    if method == "POST" and path == "/api/atlas/ask":
        question = str(body.get("question") or "").strip()
        if not question:
            return 400, {"error": "question is required"}
        offline = _flagbool(body.get("offline"), session.offline_default)
        return 200, session.atlas_reason(question=question, offline=offline)

    if method == "GET" and path == "/api/news/search":
        question = str((query.get("q") or [""])[0])
        if not question.strip():
            return 400, {"error": "q is required; an empty query is not a search"}
        offline = _qbool(query, "offline", session.offline_default)
        return 200, session.news_search(question=question, offline=offline)

    if method == "GET" and path == "/api/news/upcoming":
        # Scheduled releases, not news: future-dated by definition, so they
        # never travel through the point-in-time window.
        from qlab.news.providers import macro

        # The calendar is hand-maintained and refuses loudly once it runs out.
        # That is an expected state of the file, not a fault of the owner, so
        # it is named the way the matrix names it -- never a 500 with a repr,
        # which a client cannot tell from the desk being broken.
        try:
            events = macro.upcoming(datetime.now(timezone.utc))
        except RuntimeError as exc:
            return 200, {"upcoming": [], "error": str(exc)}
        return 200, {"upcoming": events}

    if method == "GET" and path == "/api/news/settings":
        # Cache-only and network-free, like /api/news itself.
        offline = _qbool(query, "offline", session.offline_default)
        return 200, session.news_settings(offline)

    if method == "POST" and path == "/api/news/settings":
        # Network I/O when verify is asked for. do_POST runs this one outside
        # the dispatch lock; it writes .env and the process environment, never
        # the registry, so it does not need it.
        # `verify.ok` is ANY-member, like `qlab news-check`: it is not a claim
        # that every chosen source answered.
        offline = _flagbool(body.get("offline"), session.offline_default)
        contact = body.get("edgar_contact")
        try:
            return 200, session.apply_news_settings(
                body.get("providers"), contact,
                _flagbool(body.get("verify"), False), offline=offline)
        except ValueError as exc:
            # The refusal names the source and the fix; nothing was written.
            return 400, {"error": str(exc)}

    if method == "GET" and path == "/api/news":
        offline = _qbool(query, "offline", session.offline_default)
        return 200, session.news_payload(offline)

    if method == "GET" and path == "/api/atlas/context":
        offline = _qbool(query, "offline", session.offline_default)
        return 200, session.atlas_context(offline)

    if method == "GET" and path == "/api/research/predictors":
        # Read-only over the newest persisted board. Running one is a POST to
        # /api/lab/research.predictor_board, which is owner-gated: reading the
        # evidence and producing it are different authorities.
        return 200, session.predictor_board_detail()

    if method == "GET" and path == "/api/research/qualitative":
        # Read-only over the window already fetched: composing the matrix never
        # touches the network, so it is safe under the dispatch lock.
        offline = _qbool(query, "offline", session.offline_default)
        return 200, session.qualitative_matrix(offline)

    if method == "GET" and path == "/api/workforce":
        # The same summary Atlas reasons from, so the operator and the manager
        # are looking at one picture of the desk rather than two.
        return 200, session.workforce_summary()

    if method == "GET" and path == "/api/workforce/stream":
        # What the agents themselves said, off the same bus the TUI renders.
        return 200, session.agent_stream()

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

    if method == "POST" and path == "/api/atlas/actionables":
        return 200, session.atlas_actionables(off)

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

    if method == "POST" and path == "/api/atlas/tasks":
        try:
            return 200, session.atlas_create_task(
                str(body.get("kind") or ""), str(body.get("reason") or ""))
        except ValueError as exc:
            return 400, {"error": str(exc)}

    if method == "POST" and path == "/api/atlas/autonomy":
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            return 400, {"error": "enabled must be true or false"}
        return 200, session.set_autonomy(enabled)

    if method == "POST" and path == "/api/workforce/fast":
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            return 400, {"error": "enabled must be true or false"}
        return 200, session.set_fast_mode(enabled)

    if method == "POST" and path == "/api/atlas/escalate":
        return 200, session.atlas_escalate_debate(off)

    if method == "POST" and path == "/api/atlas/message":
        try:
            return 200, session.atlas_message(body, off)
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
        # One research workflow at a time, refused BY NAME. The owner drives one
        # coordinator; a second start would register a graph nothing walks, and
        # a bare 409 leaves the operator no way to decide between waiting and
        # interrupting. Only this route is guarded: the unattended beat reaches
        # `atlas_run_startable`, which queues instead of refusing.
        running = session.running_research_workflow()
        if running is not None:
            return 409, {
                "error": (f"a research workflow is already running: "
                          f"{running['template']} ({running['workflow_id']})"),
                "running": running,
            }
        template_id = str(body.get("template_id") or "").strip()
        if template_id:
            # A registered template, resolved in-process to its own declared
            # graph. `start_workflow` still takes no phases from a body.
            try:
                return 200, session.start_template_workflow(
                    template_id, str(body.get("goal") or ""), off)
            except PermissionError as exc:
                # TemplateNotAllowed: the mode gate, naming the template and
                # what would have to change.
                return 400, {"error": str(exc)}
            except ValueError as exc:
                return 400, {"error": str(exc)}
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
            except PermissionError as exc:
                # TemplateNotAllowed: the mode gate refusing a resume that
                # would walk a plan-creating graph to its plan. Named, and 400
                # for the same reason `/api/atlas/tasks/<id>/start` is — it is
                # a refusal about authority the operator can change, not a
                # conflict with something else in flight.
                return 400, {"error": str(exc)}
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
        session.invalidate_valuation()
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

    def _drain_request_body(self) -> bool:
        """Consume any body sent with a bodyless method. False = unframable.

        A GET carrying a body left those bytes in `rfile` under HTTP/1.1
        keep-alive, and the next request on that connection was parsed out of
        them — so a client could append a second, unrelated request (a reset,
        say) to a harmless read and have the owner run it.
        """
        header = self.headers.get("Content-Length")
        if header is None:
            return True
        try:
            length = int(header)
        except ValueError:
            self.close_connection = True
            self._json(400, {"error": "Content-Length must be an integer"})
            return False
        if length < 0:
            self.close_connection = True
            self._json(400, {"error": "Content-Length must not be negative"})
            return False
        if length:
            self.rfile.read(length)
        return True

    def do_GET(self):
        if not self._drain_request_body():
            return
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            # The web client is retired. The root answers with where the desk
            # actually is rather than 404ing, because a browser pointed here is
            # an operator following an old habit, not a broken client.
            self._json(200, {
                "qlab": "owner runtime",
                "client": "the Atlas workstation — run `qlab`",
                "api": "/api/tui",
            })
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
                    parsed.path == "/api/news"
                    and _qbool(query, "refresh", False)
                ):
                    # Same rule as the Atlas read: the network fetch happens
                    # here, outside the dispatch lock, because nothing reached
                    # from handle_api may block every other request on a slow
                    # news provider.
                    offline = _qbool(
                        query, "offline", self.session.offline_default)
                    self.session.fetch_desk_news(offline)
                    with _LOCK:
                        obj = self.session.news_payload(offline)
                    status = 200
                elif (
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
                elif parsed.path == "/api/llm/backends":
                    # Probing a backend is network I/O, and this route reads no
                    # registry state at all, so it takes no dispatch lock —
                    # otherwise a settings panel opened against an unreachable
                    # daemon would freeze the whole desk for the probe timeout.
                    status, obj = handle_api(
                        self.session, "GET", parsed.path, query, {})
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
        if length < 0:
            # A negative length parses as an integer but describes no body:
            # `length > 0` was False, so the bytes were neither consumed nor
            # refused and keep-alive framed the next request against them.
            self.close_connection = True
            self._json(400, {"error": "Content-Length must not be negative"})
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
            if parsed.path == "/api/llm" and (
                    body.get("enabled") is True
                    or (body.get("backend") is not None
                        and body.get("enabled") is not False)):
                # Validation probes a backend, and both a new pair and an
                # enable are validated. The GET catalog route avoids the
                # dispatch lock entirely; this one needs it for the registry
                # write, so the probe is warmed here instead — a cold cache
                # plus an unreachable daemon would otherwise freeze every other
                # request for the probe timeout. A disable is skipped because
                # set_llm_config validates nothing for it, and an off-switch
                # must not wait on the daemon that is probably the reason it
                # was sent.
                #
                # This is a hint, not the check: set_llm_config re-reads the
                # catalog and remains the authority. If the TTL lapses in
                # between it probes again under the lock — slower, never a
                # different answer, and the accepted trade for the same reason
                # the GET route accepts serving a cached catalog at all.
                self.session.llm_backends_catalog()
            if parsed.path in ("/api/atlas/message", "/api/alpaca/test",
                               "/api/news/settings"):
                # The whole route runs outside the lock, not just a warm-up: an
                # answer is a model call, up to _ATLAS_REPLY_TIMEOUT_S of it,
                # and one operator question must not freeze the snapshot poll,
                # the SSE poll and every approval behind it. `atlas_message`
                # takes the dispatch lock itself, twice and briefly, around the
                # registry work at either end — which is why it must not be
                # reached from inside it (`_LOCK` is not reentrant).
                #
                # The credential probe is here for the same reason and a
                # simpler one: it is a socket to a venue with a PROBE_TIMEOUT_S
                # deadline, and it touches no registry state at all, so it has
                # no business holding the book while an operator waits on
                # Alpaca. Storing a login does take the lock — that one writes
                # an event row.
                #
                # A news-settings write is here for the credential probe's
                # reason: with verify:true it fetches one live window per
                # chosen source, which is minutes when gdelt is among them, and
                # it writes .env and the process environment rather than the
                # registry.
                status, obj = handle_api(self.session, "POST", parsed.path,
                                         parse_qs(parsed.query), body)
            else:
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


def _alpaca_stream_runner(*, supervisor, key, secret, stop_event) -> None:
    """The real transport: blocks on Alpaca's websocket until stopped."""
    from qlab.data.stream import run_alpaca_market_stream

    run_alpaca_market_stream(
        supervisor, key=key, secret=secret, stop_event=stop_event)


def serve(port: int = 8765, *, offline: bool = True,
          desk_mode: DeskMode | None = None) -> None:
    """Start the owner runtime (blocking). Ctrl-C to stop.

    ``desk_mode=None`` means no launcher flag chose one, so the session loads
    the operator's persisted choice rather than being handed a guess — and
    ``offline`` only seeds a desk that has never been chosen. The mode the
    session settles on is what the banner reports.
    """
    try:
        # The bind IS the one-writer guard, so it must be exclusive on every
        # platform. ThreadingHTTPServer sets allow_reuse_address (SO_REUSEADDR),
        # which on Windows lets a SECOND process bind the same live port — a
        # second registry writer admitted by the exact mechanism that exists to
        # refuse it. Windows' own default is already exclusive; keep REUSEADDR
        # only where it means "skip TIME_WAIT" (POSIX), never where it means
        # "share the port" (nt). Set the class before construction rather than
        # wrapping it, because tests monkeypatch this constructor to prove the
        # refusal happens before any registry is opened.
        setattr(ThreadingHTTPServer, "allow_reuse_address", os.name != "nt")
        httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    except Exception:
        # Resolve ownership before opening DuckDB or recovering workflows. A
        # second process that cannot bind this port must remain a pure refusal,
        # never a transient second writer that interrupts the real owner's run.
        raise
    try:
        session = UISession(offline_default=offline, desk_mode=desk_mode)
        # The bound port, not the env default: a driven coordinator addresses
        # this owner by URL and must not be pointed at a different one.
        session.port = int(port)
        market_stop, market_thread = _start_market_topics(session)
        _start_atlas_heartbeat(session, offline=offline)
        # The transport, injected here and nowhere else: only the serving
        # runtime may open a websocket, and only a live desk mode will.
        session.attach_market_stream_runner(_alpaca_stream_runner)
    except Exception:
        httpd.server_close()
        raise
    httpd.daemon_threads = True
    _Handler.session = session
    url = f"http://127.0.0.1:{port}/"
    print(_startup_banner(session.desk_mode, url))
    print("[qlab] press Ctrl-C to stop.")
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
                # Before the socket closes. A coordinator that outlives its
                # owner is an orphaned Claude tree still billing tokens against
                # a runtime URL that no longer answers.
                if session._driver is not None:
                    session._driver.stop("owner stopped")
            finally:
                try:
                    _stop_market_topics(market_stop, market_thread)
                finally:
                    try:
                        session.stop_market_stream()
                    finally:
                        httpd.server_close()
