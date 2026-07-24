"""quant-lab MCP server — the research lab.

Namespaced, ref-passing tools the orchestrator's subagents call:

    data.*       fetch the universe, summarize a point-in-time snapshot
    moments.*    estimate co-moments (returns a moment_set_id + summary)
    selection.*  run the exact research-stage candidate-universe selector
    objective.*  build the one-true objective (returns an objective_id)
    algorithms.*  list, describe, and run staged algorithms
    solve.*       compatibility alias for staged prepared-objective solvers
    backtest.*   walk-forward an arm and return metrics
    research.*   produce cited sensitivity evidence for agent judgments
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
    solve_prepared_objective,
)
from qlab.core import data as market
from qlab.core.backtest import run_backtest
from qlab.core.equilibrium import equilibrium_returns as compute_equilibrium_returns
from qlab.core.moments import detect_regime
from qlab.core.objective import build_objective
from qlab.core.selection import MAX_EXACT_ASSETS, select_k_of_n
from qlab.core.types import DataSnapshot, Decision
from qlab.core.universe import load_universe
from qlab.core.window_evidence import window_evidence
from qlab.experiment import recommend
from qlab.mcp.guardrails import LabState, check_as_of, require_fastmcp
from qlab.solvers.base import Constraints


def _require_operational_backtest_pair(objective: str, solver: str) -> None:
    """Refuse arm labels that do not describe an operational catalog pairing."""
    catalog_solver = None if solver == "none" else solver
    rows = list_algorithms(stage="operational")
    matches = [
        spec for spec in rows
        if spec["solver"] == catalog_solver
        and (
            objective == spec["id"]
            or objective in spec["objective_forms"]
        )
    ]
    if matches:
        return

    # Named policies use their catalog id (``hrp`` / ``risk_parity``), while
    # generic prepared objectives use an entry's declared objective form.
    permitted = sorted({
        candidate
        for spec in rows
        if spec["solver"] == catalog_solver
        for candidate in (spec["id"], *spec["objective_forms"])
    })
    raise PermissionError(
        "objective/solver mismatch: "
        f"solver {solver!r} is not operational for objective {objective!r}; "
        f"cataloged objectives for this solver: {permitted or ['none']}"
    )


def _current_portfolio_weights(
    registry,
    snapshot: DataSnapshot,
) -> dict[str, float] | None:
    """Mark the registry's paper positions on the equilibrium snapshot.

    The helper only reads account/position rows.  It avoids constructing a
    broker (whose ``portfolio_state`` updates the high-water mark) so
    ``research.equilibrium_returns`` remains a read-only computation apart from
    its one auditable research run.
    """
    account = registry.get_account()
    positions = registry.get_positions()
    if not account and not positions:
        return None

    latest = snapshot.prices.iloc[-1]
    cash = float(account.get("cash", 0.0))
    equity = cash
    marked: dict[str, float] = {}
    for ticker, position in positions.items():
        mark = (
            float(latest[ticker])
            if ticker in latest.index
            else float(position["avg_price"])
        )
        value = float(position["qty"]) * mark
        if value < -1e-9:
            raise ValueError(
                "equilibrium aggregation requires a long-only current portfolio"
            )
        equity += value
        if ticker in snapshot.tickers:
            marked[ticker] = value
    if equity <= 0.0:
        raise ValueError(
            "equilibrium aggregation requires positive current portfolio equity"
        )
    return {
        ticker: marked.get(ticker, 0.0) / equity
        for ticker in snapshot.tickers
    }


def register_lab_tools(app, st: LabState, *, owner_only: bool = False) -> None:
    """Mount every research-lab tool on ``app``, bound to session state ``st``.

    Split out of ``build_server`` so the combined single-process server
    (``qlab.mcp.server``) can mount the lab and trader namespaces on one
    FastMCP app over one shared Registry (one DuckDB writer).

    ``owner_only=True`` additionally mounts tools reserved for human-driven
    surfaces (owner API → TUI/CLI). Research-stage executables such as
    ``selection.run`` are owner-only: the catalog's stage boundary keeps
    research algorithms off every agent-facing MCP surface, headless included.
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

    # -- exact candidate selection (owner-only: research stage) --------------
    def selection_run(as_of: str, k: int, universe: str = "candidates",
                      tickers: list[str] | None = None,
                      lookback_days: int = 756) -> dict:
        """Run and persist exact research-stage k-of-N selection (N<=25).

        Supply either a configured universe tier or an explicit ticker list.
        This is a research run, not an ``algorithms.solve`` or paper-allocation
        path.
        """
        st.budget.charge("selection.run")
        from qlab.core.moments import estimate_moments

        d = check_as_of(as_of)
        selected_universe = list(tickers) if tickers is not None else (
            load_universe().tickers(universe)
        )
        if not selected_universe:
            raise ValueError("selection requires at least one ticker")
        if len(set(selected_universe)) != len(selected_universe):
            raise ValueError("selection tickers must be unique")
        if len(selected_universe) > MAX_EXACT_ASSETS:
            raise ValueError(
                f"exact k-of-N selection requires N <= {MAX_EXACT_ASSETS}; "
                f"got N={len(selected_universe)}"
            )
        if isinstance(k, bool) or not isinstance(k, int):
            raise TypeError("k must be an integer")
        if not 1 <= k <= len(selected_universe):
            raise ValueError(
                f"k must satisfy 1 <= k <= N; got k={k}, "
                f"N={len(selected_universe)}"
            )
        if isinstance(lookback_days, bool) or not isinstance(lookback_days, int):
            raise TypeError("lookback_days must be an integer")
        if lookback_days < 2:
            raise ValueError("lookback_days must be at least 2")

        snap = market.snapshot(
            selected_universe, d, offline=st.offline, seed=st.seed
        )
        moments = estimate_moments(
            snap, lookback_days=lookback_days, higher_moments=False
        )
        moment_set_id = st.put_moment_set(moments)
        selection = select_k_of_n(
            moments.tickers,
            k,
            covariance=moments.cov,
        ).to_dict()
        run_id = st.registry.log_run("selection", {
            "algorithm_id": "selection_k_of_n",
            "as_of": str(d),
            "universe": "explicit" if tickers is not None else universe,
            "tickers": selected_universe,
            "k": k,
            "lookback_days": lookback_days,
            "source": snap.source,
            "moment_set_id": moment_set_id,
            "result": selection,
        })
        return {
            "selected": selection["selected"],
            "score": selection["score"],
            "run_id": run_id,
            "contributions": selection["contributions"],
        }

    if owner_only:
        # Research-stage executable: humans (TUI/CLI via the owner API) may run
        # it; agent-facing MCP surfaces — headless included — never see it.
        app.tool(name="selection.run")(selection_run)

    # -- regime indicators (options for the analyst's regime call) ----------
    # Five deterministic, price-only reads on different faces of market
    # variability. Each returns one regime reading in a shared schema so the
    # analyst can weigh several and defend a single call; none forecasts returns.
    def _regime_snapshot(as_of: str, universe: str, lookback_days: int):
        d = check_as_of(as_of)
        tickers = load_universe().tickers(universe)
        return market.snapshot(tickers, d, lookback_days=lookback_days,
                               offline=st.offline, seed=st.seed)

    @app.tool(name="regime.turbulence")
    def regime_turbulence(as_of: str, universe: str = "core",
                          lookback_days: int = 756, quantile: float = 0.80) -> dict:
        """Turbulence regime: is the latest cross-asset move statistically unusual?"""
        st.budget.charge("regime.turbulence")
        from qlab.signals.indicators import turbulence_regime
        return turbulence_regime(
            _regime_snapshot(as_of, universe, lookback_days), quantile=quantile)

    @app.tool(name="regime.absorption")
    def regime_absorption(as_of: str, universe: str = "core",
                          lookback_days: int = 756, quantile: float = 0.80) -> dict:
        """Absorption-ratio regime: how tightly coupled (fragile) is the market?"""
        st.budget.charge("regime.absorption")
        from qlab.signals.indicators import absorption_regime
        return absorption_regime(
            _regime_snapshot(as_of, universe, lookback_days), quantile=quantile)

    @app.tool(name="regime.volatility_term_structure")
    def regime_vol_term(as_of: str, universe: str = "core",
                        lookback_days: int = 756, quantile: float = 0.80) -> dict:
        """Vol term-structure regime: is variance accelerating or mean-reverting?"""
        st.budget.charge("regime.volatility_term_structure")
        from qlab.signals.indicators import volatility_term_structure
        return volatility_term_structure(
            _regime_snapshot(as_of, universe, lookback_days), quantile=quantile)

    @app.tool(name="regime.drawdown")
    def regime_drawdown(as_of: str, universe: str = "core",
                        lookback_days: int = 756, quantile: float = 0.80) -> dict:
        """Drawdown regime: directional depth below the trailing peak, plus trend."""
        st.budget.charge("regime.drawdown")
        from qlab.signals.indicators import drawdown_regime
        return drawdown_regime(
            _regime_snapshot(as_of, universe, lookback_days), quantile=quantile)

    @app.tool(name="regime.tail_risk")
    def regime_tail_risk(as_of: str, universe: str = "core",
                         lookback_days: int = 756, quantile: float = 0.80) -> dict:
        """Tail-risk regime: downside/upside asymmetry and recent realised skew."""
        st.budget.charge("regime.tail_risk")
        from qlab.signals.indicators import tail_risk_regime
        return tail_risk_regime(
            _regime_snapshot(as_of, universe, lookback_days), quantile=quantile)

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

    # -- equilibrium expected returns --------------------------------------
    @app.tool(name="research.equilibrium_returns")
    def research_equilibrium_returns(
        as_of: str,
        universe: str = "core",
        lookback_days: int = 756,
    ) -> dict:
        """Reverse-optimize annual equilibrium returns and one-sigma bands."""
        from qlab.core.moments import estimate_moments

        st.budget.charge("research.equilibrium_returns")
        if isinstance(lookback_days, bool) or not isinstance(lookback_days, int):
            raise TypeError("lookback_days must be an integer")
        if lookback_days < 2:
            raise ValueError("lookback_days must be at least 2")

        d = check_as_of(as_of)
        tickers = load_universe().tickers(universe)
        snapshot = market.snapshot(
            tickers,
            d,
            lookback_days=lookback_days + 1,
            offline=st.offline,
            seed=st.seed,
        )
        moments = estimate_moments(
            snapshot,
            lookback_days=lookback_days,
            higher_moments=False,
        )
        moment_set_id = st.put_moment_set(moments)
        current_weights = _current_portfolio_weights(st.registry, snapshot)
        result = compute_equilibrium_returns(
            moments.cov,
            moments.tickers,
            int(moments.diagnostics["T"]),
            target_weights=current_weights,
        )
        portfolio_weight_source = (
            "current_paper_portfolio"
            if current_weights is not None
            else "inverse_volatility_prior_no_paper_book"
        )
        table = [
            {"ticker": ticker, **result["returns"][ticker]}
            for ticker in moments.tickers
        ]
        caveats = {
            "interpretation": "equilibrium prior, not a forecast",
            "uncertainty": "bands are parameter uncertainty",
            "prior_weights": (
                "inverse-volatility weights substitute for market-cap weights; "
                "market caps are unavailable without an added data dependency"
            ),
            "portfolio_weights": portfolio_weight_source,
            "source": snapshot.source,
            "dsr_trial_counted": False,
        }
        run_spec = {
            "as_of": str(d),
            "universe": universe,
            "tickers": moments.tickers,
            "lookback_days": lookback_days,
            "moment_set_id": moment_set_id,
            "n_obs": result["n_obs"],
            "tau": result["tau"],
            "annualization": result["annualization"],
            "risk_aversion": result["risk_aversion"],
            "prior_weights": result["prior_weights"],
            "prior_weight_source": result["prior_weight_source"],
            "portfolio": result["portfolio"],
            "table": table,
            "caveats": caveats,
        }
        run_id = st.registry.log_run("equilibrium", run_spec)
        # This reverse-optimization records no solution or backtest row, so it
        # cannot enlarge either deflated-Sharpe trial universe.
        return {
            "run_id": run_id,
            "moment_set_id": moment_set_id,
            "as_of": str(d),
            "table": table,
            "portfolio": result["portfolio"],
            "prior_weights": result["prior_weights"],
            "caveats": caveats,
        }

    # -- estimation-window evidence ----------------------------------------
    @app.tool(name="research.window_evidence")
    def research_window_evidence(
        as_of: str,
        universe: str = "core",
        cadence: str = "quarterly",
    ) -> dict:
        """Rank window/shrinkage evidence for the configured operational policy."""
        from qlab.algorithms import get_operational_policy
        from qlab.trader.mandate import load_mandate

        st.budget.charge("research.window_evidence")
        d = check_as_of(as_of)
        tickers = load_universe().tickers(universe)
        snapshot = market.snapshot(
            tickers, d, offline=st.offline, seed=st.seed
        )
        policy = get_operational_policy(load_mandate().operational_policy)
        table = window_evidence(
            snapshot,
            policy_solver=policy.id,
            cadence=cadence,
        )
        snapshot_span = {
            "start": snapshot.prices.index[0].date().isoformat(),
            "end": snapshot.prices.index[-1].date().isoformat(),
            "n_obs": int(len(snapshot.prices)),
        }
        caveats = {
            "source": snapshot.source,
            "snapshot_span": snapshot_span,
            "cost_model": "flat",
            "cost_bps": 5.0,
            "dsr_trial_counted": False,
            "ranking": (
                "Sortino descending, then lower annualized realized volatility, "
                "shallower maximum drawdown, and lower turnover."
            ),
            "limitations": [
                "This is descriptive sensitivity evidence, not proof that the "
                "top row will remain best out of sample.",
                "Early rebalances use the history then available; compare each "
                "row's n_rebalances and span before drawing conclusions.",
            ],
        }
        best = table[0]
        run_id = st.registry.log_run("window_evidence", {
            "as_of": str(d),
            "universe": universe,
            "tickers": tickers,
            "cadence": cadence,
            "policy_solver": policy.id,
            "table": table,
            "best": best,
            "caveats": caveats,
        })
        # Evidence rows are deliberately not written to ``backtests``: this
        # diagnostic sweep must not enlarge the registry's DSR trial universe.
        return {
            "run_id": run_id,
            "table": table,
            "best": best,
            "caveats": caveats,
        }

    # -- backtest -----------------------------------------------------------
    @app.tool(name="backtest.run")
    def backtest_run(objective: str, solver: str, universe: str = "core",
                    start: str = "2010-01-01", end: str = "", cadence: str = "quarterly",
                    lookback_days: int = 756, cost_bps: float = 5.0,
                    skew_lambda: float = 0.5, kurt_lambda: float = 0.5) -> dict:
        """Walk-forward backtest one arm. Returns the metric bundle + logs it."""
        st.budget.charge("backtest.run")
        _require_operational_backtest_pair(objective, solver)
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
