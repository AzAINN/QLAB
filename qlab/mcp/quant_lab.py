"""quant-lab MCP server — the research lab.

Namespaced, ref-passing tools the orchestrator's subagents call:

    data.*       fetch the universe, summarize a point-in-time snapshot
    moments.*    estimate co-moments (returns a moment_set_id + summary)
    objective.*  build the one-true objective (returns an objective_id)
    algorithms.*  list, describe, and run staged algorithms
    solve.*       compatibility alias for staged prepared-objective solvers
    backtest.*   walk-forward an arm and return metrics
    registry.*   list runs, fetch a report, log a decision (reflection loop),
                  log_verdict (referee gate)
    report.*     compile a full recommendation

The executable module delegates to the guarded combined server.
Tools return **ids + diagnostics only** — never raw tensors (invariant 1).
"""

from __future__ import annotations

import os

from qlab.arms import Arm, MomentsConfig, build_policy
from qlab.algorithms.catalog import (
    get_algorithm,
    list_algorithms,
    operational_algorithm_for_solver,
    operational_solver_names,
    solve_prepared_objective,
)
from qlab.core import data as market
from qlab.core.backtest import run_backtest
from qlab.core.moments import detect_regime
from qlab.core.objective import build_objective
from qlab.core.types import DataSnapshot, Decision
from qlab.core.universe import load_universe
from qlab.experiment import recommend
from qlab.mcp.guardrails import LabState, check_as_of, require_fastmcp
from qlab.solvers.base import Constraints


def register_lab_tools(app, st: LabState) -> None:
    """Mount every research-lab tool on ``app``, bound to session state ``st``.

    Split out of ``build_server`` so the combined single-process server
    (``qlab.mcp.server``) can mount the lab and trader namespaces on one
    FastMCP app over one shared Registry (one DuckDB writer).
    """

    # -- data ---------------------------------------------------------------
    @app.tool(name="data.fetch_universe")
    def data_fetch_universe(which: str = "core") -> dict:
        """List the investable universe (``core`` ~7 ETFs or ~19 ``candidates``)."""
        st.budget.charge("data.fetch_universe")
        uni = load_universe()
        return {"which": which, "tickers": uni.tickers(which),
                "asset_classes": uni.asset_classes()}

    @app.tool(name="data.snapshot_summary")
    def data_snapshot_summary(as_of: str, universe: str = "core",
                              lookback_days: int = 756) -> dict:
        """Summarize a point-in-time snapshot (rows, source, date span). No prices."""
        st.budget.charge("data.snapshot_summary")
        d = check_as_of(as_of)
        tickers = load_universe().tickers(universe)
        snap = market.snapshot(tickers, d, lookback_days=lookback_days,
                               offline=st.offline, seed=st.seed)
        return {"as_of": str(d), "tickers": tickers, "source": snap.source,
                "rows": int(len(snap.prices)),
                "regime": detect_regime(snap)["regime"]}

    # -- moments ------------------------------------------------------------
    @app.tool(name="moments.estimate")
    def moments_estimate(as_of: str, universe: str = "core", lookback_days: int = 756,
                         shrinkage: str = "ledoit_wolf",
                         denoise: str = "marchenko_pastur",
                         comoment_shrinkage: float | str = 0.5,
                         comoment_target: str = "isserlis",
                         higher_moments: bool = False) -> dict:
        """Estimate co-moments. Returns a moment_set_id + a safe summary."""
        st.budget.charge("moments.estimate")
        from qlab.core.moments import estimate_moments

        d = check_as_of(as_of)
        tickers = load_universe().tickers(universe)
        snap = market.snapshot(tickers, d, offline=st.offline, seed=st.seed)
        ms = estimate_moments(snap, lookback_days=lookback_days, shrinkage=shrinkage,
                              denoise=denoise, comoment_shrinkage=comoment_shrinkage,
                              comoment_target=comoment_target,
                              higher_moments=higher_moments)
        mid = st.put_moment_set(ms)
        return {"moment_set_id": mid, "summary": ms.summary()}

    # -- objective ----------------------------------------------------------
    @app.tool(name="objective.build")
    def objective_build(moment_set_id: str, form: str = "min_variance",
                        skew_lambda: float = 0.5, kurt_lambda: float = 0.5) -> dict:
        """Build the one-true objective from a moment set. Returns an objective_id."""
        st.budget.charge("objective.build")
        ms = st.get_moment_set(moment_set_id)
        obj = build_objective(form, ms, skew_lambda=skew_lambda, kurt_lambda=kurt_lambda)
        oid = st.put_objective(obj)
        return {"objective_id": oid, "form": form, "n": obj.n}

    # -- algorithm catalog + staged solve ----------------------------------
    @app.tool(name="algorithms.list")
    def algorithms_list(category: str = "", stage: str = "operational") -> dict:
        """List categorized methods and their deployment stage."""
        st.budget.charge("algorithms.list")
        if stage not in ("operational", "research", "offline", "all"):
            raise ValueError("stage must be operational, research, offline, or all")
        rows = list_algorithms(
            category=category or None,
            stage=None if stage == "all" else stage,
        )
        return {"algorithms": rows, "count": len(rows)}

    @app.tool(name="algorithms.describe")
    def algorithms_describe(algorithm_id: str) -> dict:
        """Describe one method, including whether agents may execute it."""
        st.budget.charge("algorithms.describe")
        return get_algorithm(algorithm_id).to_dict()

    @app.tool(name="policy.current")
    def policy_current() -> dict:
        """Return the mandate-configured operational allocation policy."""
        from qlab.algorithms import get_operational_policy
        from qlab.trader.mandate import load_mandate

        st.budget.charge("policy.current")
        mandate = load_mandate()
        policy = get_operational_policy(mandate.operational_policy).to_dict()
        policy["constraints"] = {
            "long_only": mandate.long_only,
            "budget": 1.0,
            "min_weight": mandate.min_weight_per_asset,
            "max_weight": mandate.max_weight_per_asset,
        }
        return policy

    @app.tool(name="algorithms.solve")
    def algorithms_solve(objective_id: str, algorithm_id: str,
                         max_weight: float = 1.0, run_id: str = "") -> dict:
        """Run an operational catalog entry against a prepared objective."""
        st.budget.charge("algorithms.solve")
        obj = st.get_objective(objective_id)
        res = solve_prepared_objective(
            algorithm_id, obj, Constraints(max_weight=max_weight)
        )
        st.registry.log_solution(
            run_id or "adhoc", algorithm_id, res,
            objective_hash=objective_id, objective_form=obj.form,
        )
        return res.to_dict()

    # Compatibility alias for older agents. It now routes through the same
    # catalog boundary, so free-form solver names cannot reach offline code.
    @app.tool(name="solve.classical")
    def solve_classical(objective_id: str, solver: str = "classical",
                        max_weight: float = 1.0, run_id: str = "") -> dict:
        """Solve an objective with a classical arm. Persists the solution."""
        st.budget.charge("solve.classical")
        obj = st.get_objective(objective_id)
        spec = operational_algorithm_for_solver(solver, obj.form)
        res = solve_prepared_objective(
            spec.id, obj, Constraints(max_weight=max_weight)
        )
        st.registry.log_solution(run_id or "adhoc", solver, res,
                                 objective_hash=objective_id, objective_form=obj.form)
        return res.to_dict()

    # -- backtest -----------------------------------------------------------
    @app.tool(name="backtest.run")
    def backtest_run(objective: str, solver: str, universe: str = "core",
                    start: str = "2010-01-01", end: str = "", cadence: str = "quarterly",
                    lookback_days: int = 756, cost_bps: float = 5.0,
                    skew_lambda: float = 0.5, kurt_lambda: float = 0.5) -> dict:
        """Walk-forward backtest one arm. Returns the metric bundle + logs it."""
        st.budget.charge("backtest.run")
        if solver != "none" and solver not in operational_solver_names():
            raise PermissionError(
                f"solver {solver!r} is not operational; inspect algorithms.list"
            )
        tickers = load_universe().tickers(universe)
        prices = market.get_prices(tickers, start, end or None, offline=st.offline,
                                   seed=st.seed)
        arm = Arm(f"{objective}:{solver}", objective, solver,
                  {"skew_lambda": skew_lambda, "kurt_lambda": kurt_lambda})
        res = run_backtest(prices, build_policy(arm, moments=MomentsConfig(
            lookback_days=lookback_days)), arm_id=arm.id, cadence=cadence,
            lookback_days=lookback_days, cost_bps=cost_bps)
        rid = st.registry.log_run("backtest", {"arm": arm.id})
        st.registry.log_backtest(rid, arm.id, res.metrics)
        return {"arm": arm.id, "metrics": res.metrics,
                "total_turnover": res.total_turnover}

    # -- registry + reflection loop ----------------------------------------
    @app.tool(name="registry.list_runs")
    def registry_list_runs(limit: int = 20) -> list:
        st.budget.charge("registry.list_runs")
        return st.registry.list_runs(limit)

    @app.tool(name="registry.report")
    def registry_report(run_id: str) -> dict:
        st.budget.charge("registry.report")
        return st.registry.report(run_id)

    @app.tool(name="registry.log_decision")
    def registry_log_decision(as_of: str, kind: str, choice: dict, rationale: str,
                             challenger_view: str = "") -> dict:
        """Log a judgment (the reflection-loop artifact). Schema-enforced."""
        st.budget.charge("registry.log_decision")
        dec = Decision(as_of=check_as_of(as_of), kind=kind, choice=choice,
                       rationale=rationale, challenger_view=challenger_view or None)
        did = st.registry.log_decision(dec)
        return {"decision_id": did}

    @app.tool(name="registry.recent_decisions")
    def registry_recent_decisions(kind: str = "", limit: int = 10) -> list:
        """Recent decisions to inject into the next rebalance (reflection loop)."""
        st.budget.charge("registry.recent_decisions")
        return st.registry.recent_decisions(kind or None, limit)

    @app.tool(name="registry.attach_challenge")
    def registry_attach_challenge(decision_id: str, challenger_view: str) -> dict:
        """Challenger-only: attach the opposing case to an existing judgment."""
        st.budget.charge("registry.attach_challenge")
        st.registry.attach_challenger_view(decision_id, challenger_view)
        return {"decision_id": decision_id, "challenger_view": challenger_view}

    @app.tool(name="registry.log_verdict")
    def registry_log_verdict(decision_id: str, verdict: str, targets: dict,
                             reasons: list | None = None) -> dict:
        """Referee-only: record PASS/FAIL for a decision. Trading requires PASS.

        ``targets`` must be the exact targets the referee reviewed — the
        verdict is bound to their content hash, so it never transfers to a
        different (even superficially similar) target set.
        """
        st.budget.charge("registry.log_verdict")
        if verdict not in ("PASS", "FAIL"):
            raise ValueError("verdict must be PASS or FAIL")
        if st.registry.get_decision(decision_id) is None:
            raise KeyError(f"unknown decision_id {decision_id!r}")
        vid = st.registry.log_verdict(decision_id, verdict, list(reasons or []),
                                      source="referee-agent", targets=targets)
        return {"verdict_id": vid, "decision_id": decision_id, "verdict": verdict}

    # -- report -------------------------------------------------------------
    @app.tool(name="report.recommendation")
    def report_recommendation(as_of: str, universe: str = "core",
                             skew_lambda: float = 0.5,
                             kurt_lambda: float = 0.5) -> dict:
        """Compile a full staged allocation recommendation."""
        from qlab.trader.mandate import load_mandate

        st.budget.charge("report.recommendation")
        check_as_of(as_of)
        return recommend(as_of=as_of, universe=universe, skew_lambda=skew_lambda,
                         kurt_lambda=kurt_lambda, offline=st.offline, seed=st.seed,
                         policy_id=load_mandate().operational_policy)


def build_server(state: LabState | None = None):
    """Build an isolated lab app for embedding and tests.

    The executable module path delegates to :mod:`qlab.mcp.server` so normal
    users always receive the owner-runtime guard and one-writer topology.
    """
    FastMCP = require_fastmcp()
    st = state or LabState(offline=os.environ.get("QLAB_OFFLINE") == "1")
    app = FastMCP("quant-lab")
    register_lab_tools(app, st)
    return app


def main() -> None:  # pragma: no cover
    from qlab.mcp.server import main as combined_main

    combined_main()


if __name__ == "__main__":  # pragma: no cover
    main()
