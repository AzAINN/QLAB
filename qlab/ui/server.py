"""A dependency-free HTTP server exposing every qlab operation as a JSON API.

Single-threaded on purpose: requests execute on the main thread, so the Qiskit
QAOA arm runs where Aer's BLAS internals expect it. One shared DuckDB registry is
the paper book, so the UI, the CLI, and the autopilot all see the same portfolio.

Routes
------
GET  /                         the single-page app
GET  /api/bootstrap            universe, mandate, agents, portfolio, defaults
GET  /api/portfolio            broker-truth positions + risk report
POST /api/recommend            an allocation + classical-vs-quantum compare
POST /api/run_once             one autopilot iteration (analyze -> solve -> trade)
POST /api/daily_ops            heartbeat (reconcile/risk/triggers; never trades)
POST /api/batch                the reproducible ablation
POST /api/compare              classical vs quantum on the same covariance
GET  /api/resource_count       the MVSK->QUBO 434-vs-7 count
GET  /api/runs                 recent registry runs
GET  /api/decisions            recent decisions (the reflection loop)
POST /api/reset                reset the paper book to starting capital
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import threading
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from qlab.core.types import _jsonable

_HERE = Path(__file__).resolve().parent
_INDEX = _HERE / "index.html"
_REPO_ROOT = _HERE.parents[1]

# A ThreadingHTTPServer keeps the browser's parallel/keep-alive connections
# responsive, but the shared DuckDB connection is not thread-safe and Qiskit's
# Aer internals want serial execution — so every dispatch runs under this lock.
# Effectively one request computes at a time (fine for a local single user),
# while the socket layer never stalls.
_LOCK = threading.Lock()


class UISession:
    """Process-wide state: one registry (the paper book) + the mandate."""

    def __init__(self, offline_default: bool = True, seed: int = 7, registry=None):
        from qlab.trader.mandate import load_mandate
        from qlab.state.registry import Registry

        self.registry = registry or Registry()
        self.mandate = load_mandate()
        self.offline_default = offline_default
        self.seed = seed
        self.registry.init_account(self.mandate.paper_capital)

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

    def market(self, offline: bool) -> dict:
        """Compact, provenance-first daily-bar snapshot for terminal clients."""
        import numpy as np

        from qlab.core import data as market
        from qlab.core.moments import detect_regime

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
        return {
            "source": snap.source,
            "as_of": last_dt.isoformat(),
            "bar_age_days": max(0, (date.today() - last_dt).days),
            "frequency": "daily",
            "regime": detect_regime(snap),
            "assets": assets,
        }

    def agents(self) -> list[dict]:
        """Agent definitions shaped for the persistent work rail."""
        from qlab.agents.loader import load_agents

        rows = []
        for agent in load_agents():
            tools = agent.tools
            if any("execute_plan" in tool for tool in tools):
                authority = "PAPER"
            elif agent.name == "referee":
                authority = "VETO"
            elif any("solve." in tool for tool in tools):
                authority = "SOLVE"
            elif agent.name == "challenger":
                authority = "CHALLENGE"
            else:
                authority = "RESEARCH"
            rows.append({
                "name": agent.name,
                "description": agent.description,
                "authority": authority,
                "state": "idle",
                "tools": tools,
            })
        return rows

    def system_status(self, offline: bool) -> dict:
        """Health and authority facts shown quietly at the bottom edge."""
        from qlab.core import data

        config_path = _REPO_ROOT / ".mcp.json"
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
        return {
            "mode": "paper",
            "offline": offline,
            "claude_available": bool(shutil.which("claude")),
            "mcp_configured": bool(servers),
            "mcp_servers": servers,
            "mcp_proxy_available": proxy_available,
            "governed_available": proxy_available and bool(shutil.which("claude")),
            "governed_authority": "propose_only",
            "governed_lock_reason": (
                "agent authority is intentionally propose-only; paper execution "
                "requires explicit human confirmation"
            ),
            "data_source": provenance[0] if provenance else "none",
            "data_age_days": provenance[1] if provenance else None,
        }

    def tui_snapshot(self, offline: bool, event_limit: int = 100) -> dict:
        """One consistent payload for a complete TUI refresh."""
        from qlab.core.objective import mvsk_qubo_resource_count

        plans = self.registry.list_plans(20)
        decisions = self.registry.recent_decisions(limit=30)
        verdicts = self.registry.verdicts_for(
            [decision["decision_id"] for decision in decisions])
        for decision in decisions:
            decision["verdict"] = verdicts.get(decision["decision_id"])
        return {
            "portfolio": self.portfolio(offline),
            "market": self.market(offline),
            "agents": self.agents(),
            "decisions": decisions,
            "runs": self.registry.list_runs(30),
            "plans": plans,
            "orders": self.registry.list_orders(50),
            "events": self.registry.read_events(event_limit),
            "system": self.system_status(offline),
            "quantum": mvsk_qubo_resource_count(7, 4),
        }


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

    if method == "GET" and path == "/api/market":
        return 200, session.market(_qbool(query, "offline", session.offline_default))

    if method == "GET" and path == "/api/agents":
        return 200, {"agents": session.agents()}

    if method == "GET" and path == "/api/system":
        offline = _qbool(query, "offline", session.offline_default)
        return 200, session.system_status(offline)

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

    if method == "GET" and path == "/api/resource_count":
        from qlab.core.objective import mvsk_qubo_resource_count

        n = int(query.get("n", ["7"])[0])
        r = int(query.get("r", ["4"])[0])
        return 200, mvsk_qubo_resource_count(max(2, min(n, 12)), max(1, min(r, 6)))

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
            offline=off, run_qaoa=bool(body.get("qaoa", False)), seed=session.seed)
        return 200, rec

    if method == "POST" and path == "/api/run_once":
        from qlab.autopilot.loop import run_once

        summary = run_once(
            registry=session.registry, mandate=session.mandate, offline=off,
            execute=bool(body.get("execute", True)),
            skew_lambda=float(body.get("skew", 0.5)),
            kurt_lambda=float(body.get("kurt", 0.5)),
            run_qaoa=bool(body.get("qaoa", False)),
            as_of=body.get("as_of") or None, seed=session.seed)
        return 200, summary

    if method == "POST" and path == "/api/daily_ops":
        from qlab.autopilot.loop import daily_ops

        return 200, daily_ops(registry=session.registry, mandate=session.mandate,
                              offline=off, seed=session.seed)

    if method == "POST" and path == "/api/batch":
        from qlab.experiment import run_ablation

        spec = body.get("spec") or _default_ui_spec()
        report = run_ablation(spec, registry=session.registry, offline=off,
                             run_qaoa=bool(body.get("qaoa", False)))
        return 200, report

    if method == "POST" and path == "/api/compare":
        from qlab.arms import MomentsConfig
        from qlab.core import data as market
        from qlab.core.universe import load_universe
        from qlab.experiment import compare_classical_quantum
        from qlab.solvers.base import Constraints

        tickers = load_universe().tickers(body.get("universe", "core"))
        snap = market.snapshot(tickers, body.get("as_of") or date.today().isoformat(),
                               offline=off, seed=session.seed)
        return 200, compare_classical_quantum(
            snap, MomentsConfig(), Constraints(),
            run_qaoa=bool(body.get("qaoa", True)))

    if method == "POST" and path == "/api/reset":
        session.registry.reset_book(session.mandate.paper_capital)
        return 200, {"reset": True, "cash": session.mandate.paper_capital}

    return 404, {"error": f"no route for {method} {path}"}


def _bootstrap(session: UISession) -> dict:
    from qlab.agents.loader import load_agents
    from qlab.core.universe import load_universe
    from qlab.solvers.base import available_solvers

    uni = load_universe()
    agents = [{"name": a.name, "description": a.description,
               "servers": sorted(a.server_scopes), "n_tools": len(a.tools),
               "tools": a.tools} for a in load_agents()]
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
            "trailing_drawdown_pct": m.trailing_drawdown_pct,
            "cadence": m.cadence, "order_type": m.order_type,
        },
        "agents": agents,
        "solvers": available_solvers(),
        "portfolio": session.portfolio(session.offline_default),
    }


def _default_ui_spec() -> dict:
    """A compact, fast ablation for the UI (short window, key arms + QC)."""
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
        "quantum_arms": [
            {"id": "QC", "objective": "mvsk", "solver": "qubo_resource_count",
             "params": {"resolution_bits": 4}},
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
    _Handler.session = UISession(offline_default=offline)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    httpd.daemon_threads = True
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
        httpd.server_close()
