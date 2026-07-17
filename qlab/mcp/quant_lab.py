"""quant-lab MCP server — the research lab.

Namespaced, ref-passing tools the orchestrator's subagents call:

    data.*       fetch the universe, summarize a point-in-time snapshot
    moments.*    estimate co-moments (returns a moment_set_id + summary)
    objective.*  build the one-true objective (returns an objective_id)
    solve.*       classical / quantum / compare / qubo_resource_count
    backtest.*   walk-forward an arm and return metrics
    registry.*   list runs, fetch a report, log a decision (reflection loop)
    report.*     compile a full recommendation

Run standalone:  ``python -m qlab.mcp.quant_lab``  (needs ``pip install qlab[mcp]``)
Tools return **ids + diagnostics only** — never raw tensors (invariant 1).
"""

from __future__ import annotations

import os

from qlab.arms import Arm, MomentsConfig, build_policy
from qlab.core import data as market
from qlab.core.backtest import run_backtest
from qlab.core.moments import detect_regime
from qlab.core.objective import build_objective, mvsk_qubo_resource_count
from qlab.core.types import DataSnapshot, Decision
from qlab.core.universe import load_universe
from qlab.experiment import compare_classical_quantum, recommend
from qlab.mcp.guardrails import LabState, check_as_of, require_fastmcp
from qlab.solvers.base import Constraints, get_solver


def build_server(state: LabState | None = None):
    FastMCP = require_fastmcp()
    st = state or LabState(offline=os.environ.get("QLAB_OFFLINE") == "1")
    app = FastMCP("quant-lab")

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
                         higher_moments: bool = True) -> dict:
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
    def objective_build(moment_set_id: str, form: str = "mvsk",
                        skew_lambda: float = 0.5, kurt_lambda: float = 0.5) -> dict:
        """Build the one-true objective from a moment set. Returns an objective_id."""
        st.budget.charge("objective.build")
        ms = st.get_moment_set(moment_set_id)
        obj = build_objective(form, ms, skew_lambda=skew_lambda, kurt_lambda=kurt_lambda)
        oid = st.put_objective(obj)
        return {"objective_id": oid, "form": form, "n": obj.n}

    # -- solve --------------------------------------------------------------
    @app.tool(name="solve.classical")
    def solve_classical(objective_id: str, solver: str = "classical_multistart",
                        max_weight: float = 1.0, run_id: str = "") -> dict:
        """Solve an objective with a classical arm. Persists the solution."""
        st.budget.charge("solve.classical")
        obj = st.get_objective(objective_id)
        c = Constraints(max_weight=max_weight)
        res = get_solver(solver).solve(obj, c)
        st.registry.log_solution(run_id or "adhoc", solver, res,
                                 objective_hash=objective_id, objective_form=obj.form)
        return res.to_dict()

    @app.tool(name="solve.quantum")
    def solve_quantum(objective_id: str, k: int = 0, resolution_bits: int = 3,
                     reps: int = 2) -> dict:
        """Solve a binary quantum arm (selection QUBO or discretized MV) on Aer."""
        st.budget.charge("solve.quantum")
        obj = st.get_objective(objective_id)
        ctx = {"k": k} if k else {}
        try:
            res = get_solver("qaoa", reps=reps).solve(obj, Constraints(), **ctx)
            return res.to_dict()
        except Exception as exc:
            return {"unavailable": repr(exc),
                    "note": "install qlab[quantum] for the Aer QAOA arm"}

    @app.tool(name="solve.compare")
    def solve_compare(as_of: str, universe: str = "core",
                     lookback_days: int = 756, run_qaoa: bool = True) -> dict:
        """Run classical vs quantum on the SAME covariance (the headline compare)."""
        st.budget.charge("solve.compare")
        d = check_as_of(as_of)
        tickers = load_universe().tickers(universe)
        snap = market.snapshot(tickers, d, offline=st.offline, seed=st.seed)
        return compare_classical_quantum(snap, MomentsConfig(lookback_days=lookback_days),
                                        Constraints(), run_qaoa=run_qaoa)

    @app.tool(name="solve.qubo_resource_count")
    def solve_qubo_resource_count(n: int = 7, resolution_bits: int = 4) -> dict:
        """The 434-vs-7 argument, as a count: MVSK->QUBO->Ising resources."""
        st.budget.charge("solve.qubo_resource_count")
        out = mvsk_qubo_resource_count(n, resolution_bits)
        out["note"] = ("worst-case closed form; run objective.build + "
                       "solve.constructed_resource_count for measured counts")
        return out

    @app.tool(name="solve.constructed_resource_count")
    def solve_constructed_resource_count(objective_id: str, resolution_bits: int = 4) -> dict:
        """Measured MVSK->QUBO->Ising resources from an actual construction."""
        st.budget.charge("solve.constructed_resource_count")
        from qlab.solvers.ising_encoder import resource_report
        return resource_report(st.get_objective(objective_id), resolution_bits)

    # -- backtest -----------------------------------------------------------
    @app.tool(name="backtest.run")
    def backtest_run(objective: str, solver: str, universe: str = "core",
                    start: str = "2010-01-01", end: str = "", cadence: str = "quarterly",
                    lookback_days: int = 756, cost_bps: float = 5.0,
                    skew_lambda: float = 0.5, kurt_lambda: float = 0.5) -> dict:
        """Walk-forward backtest one arm. Returns the metric bundle + logs it."""
        st.budget.charge("backtest.run")
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
        vid = st.registry.log_verdict(decision_id, verdict, list(reasons or []),
                                      source="referee-agent", targets=targets)
        return {"verdict_id": vid, "decision_id": decision_id, "verdict": verdict}

    # -- report -------------------------------------------------------------
    @app.tool(name="report.recommendation")
    def report_recommendation(as_of: str, universe: str = "core",
                             skew_lambda: float = 0.5, kurt_lambda: float = 0.5,
                             run_qaoa: bool = True) -> dict:
        """Compile a full allocation recommendation with a classical/quantum compare."""
        st.budget.charge("report.recommendation")
        check_as_of(as_of)
        return recommend(as_of=as_of, universe=universe, skew_lambda=skew_lambda,
                         kurt_lambda=kurt_lambda, offline=st.offline,
                         run_qaoa=run_qaoa, seed=st.seed)

    return app


def main() -> None:  # pragma: no cover
    build_server().run()


if __name__ == "__main__":  # pragma: no cover
    main()
