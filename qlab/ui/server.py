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
GET  /api/data/health          data-policy provenance, freshness, eligibility
GET  /api/data/permit/current  the latest recorded data permit for a purpose
GET  /api/quotes               latest cached quotes + live market-stream health
GET  /api/events               event stream with cursor and limit
GET  /api/plans                recent order plans
GET  /api/orders               recent orders
GET  /api/agents               deployed agent definitions
GET  /api/algorithms           categorized algorithm deployment catalog
GET  /api/policy               configured operational allocation policy
GET  /api/workflows            durable Claude-workforce runs and phase state
GET  /api/workflows/<id>       one durable workflow and its ordered steps
GET  /api/stream               durable audit + transient market events (live)
GET  /api/tui                  one consistent terminal snapshot
POST /api/lab/<tool>           bounded research tool executed by this owner
POST /api/workflows/start      begin a standard or panel workforce run
POST /api/workflows/<phase>    update one role-bound workflow phase
POST /api/rebalance_preview    build an exact, referee-bound checked plan
POST /api/plans/execute        human-confirm one existing checked paper plan
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
import shutil
import threading
import uuid
import webbrowser
from collections import deque
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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

_MARKET_EVENT_LIMIT = 500
_STREAM_PAGE_CEILING = 5000
_QUOTE_REFRESH_TTL_SECONDS = 30.0
_QUOTE_MIN_INTERVAL_SECONDS = 5.0
_MARKET_THREAD_NAME = "qlab-market-topics"
_MARKET_THREAD_JOIN_TIMEOUT_SECONDS = 5.0
_MARKET_THREAD_LOCK = threading.Lock()
_ACTIVE_MARKET_THREAD: threading.Thread | None = None

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

    def __init__(self, offline_default: bool = True, seed: int = 7, registry=None):
        from qlab.trader.mandate import load_mandate
        from qlab.mcp.guardrails import LabState
        from qlab.mcp.quant_lab import register_lab_tools
        from qlab.state.registry import Registry

        self.registry = registry or Registry()
        self.mandate = load_mandate()
        self.offline_default = offline_default
        self.seed = seed
        self._market_events: deque[dict] = deque(maxlen=_MARKET_EVENT_LIMIT)
        self._market_lock = threading.Lock()
        self._last_quote_signature: tuple[tuple[str, float, float], ...] | None = None
        self._regime_hmm_cache: dict[str, dict[str, object]] = {}
        # The live market stream is attached only under an operational policy
        # (a real Alpaca feed); it stays None for demo/offline runtimes.
        self.market_stream = None
        self.registry.init_account(self.mandate.paper_capital)
        self.lab_state = LabState(
            registry=self.registry, max_calls=200,
            offline=offline_default, seed=seed,
        )
        owner_tools = _OwnerToolApp()
        register_lab_tools(owner_tools, self.lab_state, owner_only=True)
        self._lab_tools = owner_tools.tools

    # -- Claude workforce --------------------------------------------------
    def call_lab_tool(self, name: str, body: dict, offline: bool) -> object:
        """Run a safe research tool in-process so this owner keeps DuckDB."""
        if name not in OWNER_LAB_TOOLS or name not in self._lab_tools:
            raise PermissionError(f"lab tool {name!r} is not exposed by the owner")
        self.lab_state.offline = offline
        args = {key: value for key, value in body.items() if key != "offline"}
        return self._lab_tools[name](**args)  # type: ignore[operator]

    def start_workflow(self, body: dict) -> dict:
        """Start a durable portfolio/research workforce run."""
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
        return self.registry.start_workflow(kind, request)

    def update_workflow(self, phase: str, body: dict) -> dict:
        return self.registry.update_workflow_phase(
            str(body.get("workflow_id") or ""),
            phase,
            str(body.get("status") or "working"),
            str(body.get("summary") or ""),
            body.get("artifacts") if isinstance(body.get("artifacts"), dict) else {},
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

    def execute_checked_plan(self, body: dict, offline: bool) -> dict:
        """Execute one persisted checked plan after an explicit TUI confirmation."""
        from qlab.trader.broker import get_broker
        from qlab.trader.mandate import MandateViolation
        from qlab.trader.plan import OrderLeg, OrderPlan, execute_plan

        if body.get("human_confirmed") is not True:
            raise PermissionError("human_confirmed=true is required")
        plan_id = str(body.get("plan_id") or "")
        stored = self.registry.get_plan(plan_id)
        if stored is None:
            raise KeyError(f"unknown plan_id {plan_id!r}")
        legs = [OrderLeg(**leg) for leg in (stored.get("legs") or [])]
        expected_legs = int((stored.get("pre_trade") or {}).get("n_legs", 0))
        if len(legs) != expected_legs:
            raise RuntimeError(
                f"plan {plan_id!r} has incomplete persisted legs; re-propose"
            )
        plan = OrderPlan(
            plan_id=stored["plan_id"], decision_id=stored["decision_id"],
            targets=stored["targets"], legs=legs,
            pre_trade=stored["pre_trade"], state=stored["state"],
        )
        # Execution-time data revalidation (P3): under an operational policy the
        # data backing this plan must STILL be execution-grade at submission —
        # fresh daily bar and, if a live stream is attached, fresh quotes. Demo
        # /offline runtimes are never execution-grade, so this gate applies only
        # to the real operational path and never blocks the simulated demo.
        from qlab.core import data as market

        policy = market.policy_for(offline, seed=self.seed)
        if policy.execution_eligible:
            health = self.data_health(offline, purpose="execution")
            if health.get("blocked") or not health.get("eligible_for_execution"):
                return {"executed": False, "blocked_by": "data_revalidation",
                        "data_health": health}

        broker = get_broker(
            self.registry, offline=offline,
            starting_cash=self.mandate.paper_capital, seed=self.seed,
            universe=self.mandate.universe_whitelist,
        )
        try:
            result = execute_plan(self.registry, broker, plan)
        except MandateViolation as exc:
            return {"executed": False, "mandate_violation": str(exc)}
        return {"executed": True, **result}

    # -- portfolio view -----------------------------------------------------
    def portfolio(self, offline: bool) -> dict:
        from qlab.trader.broker import get_broker

        broker = get_broker(self.registry, offline=offline,
                            starting_cash=self.mandate.paper_capital,
                            seed=self.seed, universe=self.mandate.universe_whitelist)
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
                seed=self.seed, universe=universe)
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

    def tui_snapshot(self, offline: bool, event_limit: int = 100) -> dict:
        """One consistent payload for a complete TUI refresh."""
        from qlab.algorithms import list_algorithms

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
        return {
            "portfolio": portfolio,
            "live_portfolio": self.live_portfolio(offline),
            "market": market_snapshot,
            "stress": stress,
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


# ---------------------------------------------------------------------------
# API dispatch (pure functions of the session; easy to unit-test)
# ---------------------------------------------------------------------------
def handle_api(session: UISession, method: str, path: str,
               query: dict, body: dict) -> tuple[int, dict]:
    off = bool(body.get("offline", query.get("offline", [session.offline_default])[0]
                        if isinstance(query.get("offline"), list) else session.offline_default))

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

    if method == "GET" and path == "/api/workflows":
        limit = max(1, min(int(query.get("limit", ["10"])[0]), 50))
        return 200, {"workflows": session.registry.list_workflows(limit)}

    if method == "GET" and path.startswith("/api/workflows/"):
        workflow_id = path.removeprefix("/api/workflows/")
        workflow = session.registry.get_workflow(workflow_id)
        if workflow is None:
            return 404, {"error": f"unknown workflow_id {workflow_id!r}"}
        return 200, workflow

    if method == "GET" and path == "/api/system":
        offline = _qbool(query, "offline", session.offline_default)
        return 200, session.system_status(offline)

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
        return 200, session.execute_checked_plan(body, off)

    if method == "POST" and path.startswith("/api/workflows/"):
        phase = path.removeprefix("/api/workflows/")
        try:
            return 200, session.update_workflow(phase, body)
        except ValueError as exc:
            return 400, {"error": str(exc)}

    if method == "POST" and path == "/api/run_once":
        from qlab.autopilot.loop import run_once

        summary = run_once(
            registry=session.registry, mandate=session.mandate, offline=off,
            execute=bool(body.get("execute", True)),
            skew_lambda=float(body.get("skew", 0.5)),
            kurt_lambda=float(body.get("kurt", 0.5)),
            as_of=body.get("as_of") or None, seed=session.seed)
        return 200, summary

    if method == "POST" and path == "/api/daily_ops":
        from qlab.autopilot.loop import daily_ops

        return 200, daily_ops(registry=session.registry, mandate=session.mandate,
                              offline=off, seed=session.seed)

    if method == "POST" and path == "/api/batch":
        from qlab.experiment import run_ablation

        spec = body.get("spec") or _default_ui_spec()
        report = run_ablation(spec, registry=session.registry, offline=off)
        return 200, report

    if method == "POST" and path == "/api/reset":
        session.registry.reset_book(session.mandate.paper_capital)
        return 200, {"reset": True, "cash": session.mandate.paper_capital}

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


def _qbool(query: dict, key: str, default: bool) -> bool:
    v = query.get(key)
    if not v:
        return default
    return str(v[0]).lower() in ("1", "true", "yes", "on")


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
        if parsed.path == "/api/stream":
            self._stream(parse_qs(parsed.query))
            return
        if parsed.path.startswith("/api/"):
            try:
                with _LOCK:
                    status, obj = handle_api(self.session, "GET", parsed.path,
                                             parse_qs(parsed.query), {})
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
                    with _LOCK:
                        audit_events = self.session.read_audit_stream_events(
                            read_limit, read_after)
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
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {}
        try:
            with _LOCK:
                status, obj = handle_api(self.session, "POST", parsed.path,
                                         parse_qs(parsed.query), body)
        except Exception as exc:
            status, obj = 500, {"error": repr(exc)}
        self._json(status, obj)


def serve(port: int = 8765, *, offline: bool = True, open_browser: bool = True) -> None:
    """Start the UI server (blocking). Ctrl-C to stop."""
    session = UISession(offline_default=offline)
    market_stop, market_thread = _start_market_topics(session)
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    except Exception:
        _stop_market_topics(market_stop, market_thread)
        raise
    httpd.daemon_threads = True
    _Handler.session = session
    url = f"http://127.0.0.1:{port}/"
    print(f"[qlab] UI at {url}  (offline={'on' if offline else 'off'}; paper capital only)")
    print("[qlab] press Ctrl-C to stop.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[qlab] UI stopped.")
    finally:
        try:
            _stop_market_topics(market_stop, market_thread)
        finally:
            httpd.server_close()
