"""Stateless MCP proxy for Claude sessions launched by the Textual console.

Unlike the original quant-lab / quant-trader stdio servers, this process never
opens DuckDB. Every tool delegates to the already-running owner API. Its
authority is intentionally capped at observation, research, durable workforce
coordination, daily operations, and dry rebalance previews; paper execution
remains a human-confirmed TUI action.
"""

from __future__ import annotations

import os
from typing import Any

import httpx


class OwnerRefused(RuntimeError):
    """The owner runtime refused a call, carrying its own explanation.

    A bare ``HTTPStatusError`` renders as "Server error '500 …' for url …",
    which tells a worker nothing about *why* it was refused — so the worker
    repeats the identical call until its turn budget is gone. Every refusal
    the owner can explain (a missing artifact, a phase whose dependency is
    not done, a budget, a look-ahead date) is re-raised with that text so one
    corrected retry is possible instead of an unbounded blind one.
    """


def _refusal(path: str, exc: httpx.HTTPStatusError) -> OwnerRefused:
    detail = ""
    try:
        payload = exc.response.json()
        detail = str(payload.get("error") or "").strip()
    except ValueError:
        detail = exc.response.text.strip()[:400]
    return OwnerRefused(
        f"the qlab owner refused {path}: {detail or exc.response.reason_phrase}"
    )


class RuntimeClient:
    def __init__(self, base_url: str | None = None, *, offline: bool | None = None):
        self.base_url = (base_url or os.environ.get(
            "QLAB_RUNTIME_URL", "http://127.0.0.1:8765")).rstrip("/")
        if offline is None:
            offline = os.environ.get("QLAB_OFFLINE", "1") == "1"
        self.offline = bool(offline)

    def get(self, path: str, **params: Any) -> dict:
        try:
            response = httpx.get(
                self.base_url + path, params=params, timeout=httpx.Timeout(30.0))
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _refusal(path, exc) from exc
        except httpx.HTTPError as exc:
            raise OwnerRefused(
                f"the qlab owner runtime at {self.base_url} is unreachable "
                f"({exc!r}); stop and report this — retrying will not help"
            ) from exc
        return response.json()

    def post(self, path: str, body: dict | None = None) -> dict:
        try:
            response = httpx.post(
                self.base_url + path, json=body or {},
                timeout=httpx.Timeout(900.0, connect=10.0))
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _refusal(path, exc) from exc
        except httpx.HTTPError as exc:
            raise OwnerRefused(
                f"the qlab owner runtime at {self.base_url} is unreachable "
                f"({exc!r}); stop and report this — retrying will not help"
            ) from exc
        return response.json()


def register_proxy_tools(app, client: RuntimeClient) -> None:
    """Register the bounded owner-API surface on a FastMCP-like app."""

    def lab(name: str, args: dict) -> Any:
        payload = dict(args)
        payload["offline"] = client.offline
        return client.post(f"/api/lab/{name}", payload).get("result")

    @app.tool(name="portfolio_state")
    def portfolio_state() -> dict:
        """Current paper portfolio, exposure, drawdown, and target weights."""
        return client.get("/api/portfolio", offline=int(client.offline))

    @app.tool(name="market_snapshot")
    def market_snapshot() -> dict:
        """Daily-bar market snapshot with source, age, volatility, and regime."""
        return client.get("/api/market", offline=int(client.offline))

    @app.tool(name="policy_current")
    def policy_current() -> dict:
        """Configured operational allocation policy and evidence rationale."""
        return client.get("/api/policy")

    @app.tool(name="audit_events")
    def audit_events(limit: int = 50) -> list[dict]:
        """Recent ordered workflow, mandate, proposal, and execution events."""
        return client.get("/api/events", limit=max(1, min(limit, 200))).get("events", [])

    @app.tool(name="research_runs")
    def research_runs() -> list[dict]:
        """Recent reproducible research and ablation runs."""
        return client.get("/api/runs").get("runs", [])

    @app.tool(name="research_decisions")
    def research_decisions() -> list[dict]:
        """Recent agent-authored judgment records and reflections."""
        return client.get("/api/decisions").get("decisions", [])

    @app.tool(name="workflow_rebalance_preview")
    def rebalance_preview(targets: dict, decision_id: str) -> dict:
        """Build a dry plan from the optimizer's exact referee-reviewed targets."""
        return client.post("/api/rebalance_preview", {
            "offline": client.offline, "targets": targets,
            "decision_id": decision_id,
        })

    @app.tool(name="workflow_daily_ops")
    def daily_ops() -> dict:
        """Run the non-trading reconcile, risk, drift, and regime heartbeat."""
        return client.post("/api/daily_ops", {"offline": client.offline})

    @app.tool(name="research_batch")
    def research_batch() -> dict:
        """Run the compact offline ablation through the owner runtime."""
        return client.post("/api/batch", {
            "offline": client.offline,
        })

    # -- owner-backed research lab ----------------------------------------
    @app.tool(name="data_fetch_universe")
    def data_fetch_universe(which: str = "core") -> dict:
        """List the investable universe without opening a second registry."""
        return lab("data.fetch_universe", {"which": which})

    @app.tool(name="data_snapshot_summary")
    def data_snapshot_summary(as_of: str, universe: str = "core",
                              lookback_days: int = 756) -> dict:
        """Point-in-time data provenance, span, and regime summary."""
        return lab("data.snapshot_summary", {
            "as_of": as_of, "universe": universe, "lookback_days": lookback_days,
        })

    @app.tool(name="qa_data_integrity")
    def qa_data_integrity(as_of: str, universe: str = "core",
                          lookback_days: int = 756) -> dict:
        """Deterministic per-ticker freshness, gap, jump, and span findings."""
        return lab("qa.data_integrity", {
            "as_of": as_of, "universe": universe, "lookback_days": lookback_days,
        })

    @app.tool(name="moments_estimate")
    def moments_estimate(as_of: str, universe: str = "core",
                         lookback_days: int = 756,
                         shrinkage: str = "ledoit_wolf",
                         denoise: str = "marchenko_pastur",
                         comoment_shrinkage: float | str = 0.5,
                         comoment_target: str = "isserlis",
                         higher_moments: bool = False) -> dict:
        """Estimate owner-held moments and return only an id plus diagnostics."""
        return lab("moments.estimate", {
            "as_of": as_of, "universe": universe, "lookback_days": lookback_days,
            "shrinkage": shrinkage, "denoise": denoise,
            "comoment_shrinkage": comoment_shrinkage,
            "comoment_target": comoment_target,
            "higher_moments": higher_moments,
        })

    @app.tool(name="research_equilibrium_returns")
    def research_equilibrium_returns(
        as_of: str,
        universe: str = "core",
        lookback_days: int = 756,
    ) -> dict:
        """Reverse-optimize equilibrium returns through the owner runtime."""
        return lab("research.equilibrium_returns", {
            "as_of": as_of,
            "universe": universe,
            "lookback_days": lookback_days,
        })

    @app.tool(name="research_window_evidence")
    def research_window_evidence(
        as_of: str,
        universe: str = "core",
        cadence: str = "quarterly",
    ) -> dict:
        """Rank cited estimation-window evidence through the owner runtime."""
        return lab("research.window_evidence", {
            "as_of": as_of,
            "universe": universe,
            "cadence": cadence,
        })

    # -- regime indicators (options for the analyst's regime call) --------
    def regime(name: str, as_of: str, universe: str, lookback_days: int) -> dict:
        return lab(name, {"as_of": as_of, "universe": universe,
                          "lookback_days": lookback_days})

    @app.tool(name="regime_turbulence")
    def regime_turbulence(as_of: str, universe: str = "core",
                          lookback_days: int = 756) -> dict:
        """Turbulence regime: how unusual is the latest cross-asset move."""
        return regime("regime.turbulence", as_of, universe, lookback_days)

    @app.tool(name="regime_absorption")
    def regime_absorption(as_of: str, universe: str = "core",
                          lookback_days: int = 756) -> dict:
        """Absorption-ratio regime: how tightly coupled (fragile) the market is."""
        return regime("regime.absorption", as_of, universe, lookback_days)

    @app.tool(name="regime_volatility_term_structure")
    def regime_volatility_term_structure(as_of: str, universe: str = "core",
                                         lookback_days: int = 756) -> dict:
        """Vol term-structure regime: is variance accelerating or mean-reverting."""
        return regime("regime.volatility_term_structure", as_of, universe,
                      lookback_days)

    @app.tool(name="regime_drawdown")
    def regime_drawdown(as_of: str, universe: str = "core",
                        lookback_days: int = 756) -> dict:
        """Drawdown regime: directional depth below the trailing peak, plus trend."""
        return regime("regime.drawdown", as_of, universe, lookback_days)

    @app.tool(name="regime_tail_risk")
    def regime_tail_risk(as_of: str, universe: str = "core",
                         lookback_days: int = 756) -> dict:
        """Tail-risk regime: downside/upside asymmetry and recent realised skew."""
        return regime("regime.tail_risk", as_of, universe, lookback_days)

    @app.tool(name="objective_build")
    def objective_build(moment_set_id: str, form: str = "min_variance",
                        skew_lambda: float = 0.5,
                        kurt_lambda: float = 0.5) -> dict:
        """Build an owner-held objective from a prior moment-set id."""
        return lab("objective.build", {
            "moment_set_id": moment_set_id, "form": form,
            "skew_lambda": skew_lambda, "kurt_lambda": kurt_lambda,
        })

    @app.tool(name="algorithms_list")
    def algorithms_list(category: str = "", stage: str = "operational") -> dict:
        """List algorithm catalog entries by category and deployment stage."""
        return lab("algorithms.list", {"category": category, "stage": stage})

    @app.tool(name="algorithms_describe")
    def algorithms_describe(algorithm_id: str) -> dict:
        """Describe one cataloged algorithm and its permitted deployment stage."""
        return lab("algorithms.describe", {"algorithm_id": algorithm_id})

    @app.tool(name="algorithms_solve")
    def algorithms_solve(objective_id: str, algorithm_id: str,
                         max_weight: float = 1.0, run_id: str = "") -> dict:
        """Run one operational algorithm against an owner-held objective."""
        return lab("algorithms.solve", {
            "objective_id": objective_id, "algorithm_id": algorithm_id,
            "max_weight": max_weight, "run_id": run_id,
        })

    @app.tool(name="solve_classical")
    def solve_classical(objective_id: str, solver: str = "classical",
                        max_weight: float = 1.0, run_id: str = "") -> dict:
        """Compatibility route through the staged operational catalog."""
        return lab("solve.classical", {
            "objective_id": objective_id, "solver": solver,
            "max_weight": max_weight, "run_id": run_id,
        })

    @app.tool(name="backtest_run")
    def backtest_run(objective: str, solver: str, universe: str = "core",
                     start: str = "2010-01-01", end: str = "",
                     cadence: str = "quarterly", lookback_days: int = 756,
                     cost_bps: float = 5.0, skew_lambda: float = 0.5,
                     kurt_lambda: float = 0.5) -> dict:
        """Run and record a walk-forward backtest through the owner."""
        return lab("backtest.run", {
            "objective": objective, "solver": solver, "universe": universe,
            "start": start, "end": end, "cadence": cadence,
            "lookback_days": lookback_days, "cost_bps": cost_bps,
            "skew_lambda": skew_lambda, "kurt_lambda": kurt_lambda,
        })

    @app.tool(name="registry_list_runs")
    def registry_list_runs(limit: int = 20) -> list:
        """List recent research runs from the owner registry."""
        return lab("registry.list_runs", {"limit": limit})

    @app.tool(name="registry_report")
    def registry_report(run_id: str) -> dict:
        """Fetch a persisted research report by run id."""
        return lab("registry.report", {"run_id": run_id})

    @app.tool(name="registry_log_decision")
    def registry_log_decision(as_of: str, kind: str, choice: dict,
                              rationale: str, challenger_view: str = "") -> dict:
        """Record one governed judgment and its rationale."""
        return lab("registry.log_decision", {
            "as_of": as_of, "kind": kind, "choice": choice,
            "rationale": rationale, "challenger_view": challenger_view,
        })

    @app.tool(name="registry_recent_decisions")
    def registry_recent_decisions(kind: str = "", limit: int = 10) -> list:
        """Read prior judgments and realized reflections."""
        return lab("registry.recent_decisions", {"kind": kind, "limit": limit})

    @app.tool(name="registry_attach_challenge")
    def registry_attach_challenge(decision_id: str, challenger_view: str) -> dict:
        """Attach the challenger evidence to the analyst's persisted judgment."""
        return lab("registry.attach_challenge", {
            "decision_id": decision_id, "challenger_view": challenger_view,
        })

    @app.tool(name="registry_log_verdict")
    def registry_log_verdict(decision_id: str, verdict: str, targets: dict,
                             reasons: list | None = None) -> dict:
        """Record the referee PASS or FAIL bound to exact target weights."""
        return lab("registry.log_verdict", {
            "decision_id": decision_id, "verdict": verdict, "targets": targets,
            "reasons": reasons or [],
        })

    @app.tool(name="report_recommendation")
    def report_recommendation(as_of: str, universe: str = "core",
                              skew_lambda: float = 0.5,
                              kurt_lambda: float = 0.5) -> dict:
        """Compile a human-facing staged recommendation through the owner."""
        return lab("report.recommendation", {
            "as_of": as_of, "universe": universe,
            "skew_lambda": skew_lambda, "kurt_lambda": kurt_lambda,
        })

    # -- durable workforce control ----------------------------------------
    @app.tool(name="workflow_start")
    def workflow_start(
        goal: str,
        as_of: str = "",
        universe: str = "core",
        kind: str = "portfolio_review",
        variants: list[dict] | None = None,
    ) -> dict:
        """Create a durable standard or panel workforce run and return its id."""
        payload = {
            "goal": goal, "as_of": as_of, "universe": universe,
            "kind": kind, "offline": client.offline,
        }
        if variants is not None:
            payload["variants"] = variants
        return client.post("/api/workflows/start", payload)

    @app.tool(name="workflow_status")
    def workflow_status(workflow_id: str = "", limit: int = 10) -> dict:
        """Inspect one workforce run, or list recent runs when id is omitted."""
        if workflow_id:
            if not workflow_id.replace("-", "").isalnum():
                raise ValueError("invalid workflow_id")
            return client.get(f"/api/workflows/{workflow_id}")
        return client.get("/api/workflows", limit=max(1, min(limit, 50)))

    def phase_update(phase: str, workflow_id: str, status: str,
                     summary: str, artifacts: dict | None) -> dict:
        return client.post(f"/api/workflows/{phase}", {
            "workflow_id": workflow_id, "status": status,
            "summary": summary, "artifacts": artifacts or {},
        })

    @app.tool(name="workflow_phase")
    def workflow_phase(phase: str, workflow_id: str, status: str,
                       summary: str = "",
                       artifacts: dict | None = None) -> dict:
        """Persist one workflow instance phase; the owner validates its name."""
        return phase_update(phase, workflow_id, status, summary, artifacts)

    @app.tool(name="workflow_analyst")
    def workflow_analyst(workflow_id: str, status: str,
                         summary: str = "", artifacts: dict | None = None) -> dict:
        """Moments analyst only: persist analyst phase progress or completion."""
        return phase_update("analyst", workflow_id, status, summary, artifacts)

    @app.tool(name="workflow_challenger")
    def workflow_challenger(workflow_id: str, status: str,
                            summary: str = "", artifacts: dict | None = None) -> dict:
        """Challenger only: persist challenger phase progress or completion."""
        return phase_update("challenger", workflow_id, status, summary, artifacts)

    @app.tool(name="workflow_optimizer")
    def workflow_optimizer(workflow_id: str, status: str,
                           summary: str = "", artifacts: dict | None = None) -> dict:
        """Optimization runner only: persist optimizer phase progress."""
        return phase_update("optimizer", workflow_id, status, summary, artifacts)

    @app.tool(name="workflow_referee")
    def workflow_referee(workflow_id: str, status: str,
                         summary: str = "", artifacts: dict | None = None) -> dict:
        """Referee only: persist gate progress, PASS, or FAIL evidence."""
        return phase_update("referee", workflow_id, status, summary, artifacts)

    @app.tool(name="workflow_reporter")
    def workflow_reporter(workflow_id: str, status: str,
                          summary: str = "", artifacts: dict | None = None) -> dict:
        """Reporter only: persist the final human-facing proposal phase."""
        return phase_update("reporter", workflow_id, status, summary, artifacts)


def build_server(client: RuntimeClient | None = None):
    from fastmcp import FastMCP

    app = FastMCP("qlab-operator")
    register_proxy_tools(app, client or RuntimeClient())
    return app


def main() -> None:  # pragma: no cover - stdio transport
    build_server().run()


if __name__ == "__main__":  # pragma: no cover
    main()
