"""quant-lab MCP server — the research lab.

Namespaced, ref-passing tools the orchestrator's subagents call:

    data.*       fetch the universe, summarize a point-in-time snapshot
    qa.*         run deterministic, read-only snapshot integrity checks
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

import math
import os
from datetime import date
from numbers import Real

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
from qlab.core.views import (
    CorrView,
    TailView,
    VolView,
    apply_views as apply_risk_views,
)
from qlab.core.window_evidence import window_evidence
from qlab.experiment import recommend
from qlab.mcp.guardrails import LabState, check_as_of, require_fastmcp
from qlab.solvers.base import Constraints


_MAX_RISK_VIEWS = 3
_MAX_VIEW_CONFIDENCE = 0.7
_VIEWS_LOOKBACK_DAYS = 756
_DATA_INTEGRITY_THRESHOLDS = {
    "max_last_bar_age_days": 4,
    "max_longest_gap_days": 5,
    "max_abs_1d_return": 0.35,
    "max_missing_bars": 0,
    "min_span_coverage": 0.95,
}


def _view_string(value: object, field: str, index: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"view {index} field {field!r} must be a non-empty string"
        )
    return value.strip()


def _view_number(value: object, field: str, index: int) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"view {index} field {field!r} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"view {index} field {field!r} must be finite")
    return number


def _verify_view_provenance(canonical_views: list[dict], excerpt: str) -> bool:
    """Every view's quote must be grounded in the operator's source text.

    Returns False (unverified, but permitted) when no excerpt was supplied so
    the audit trail is explicit; raises when an excerpt is supplied and a quote
    is not a whitespace-normalized substring of it — a fabricated or
    laundered quote cannot then reach the analyst's context.
    """
    normalized_excerpt = " ".join(excerpt.split()).lower()
    if not normalized_excerpt:
        return False
    for index, view in enumerate(canonical_views, start=1):
        quote = " ".join(str(view.get("source_quote", "")).split()).lower()
        if not quote or quote not in normalized_excerpt:
            raise ValueError(
                f"view {index} source_quote is not found in the supplied "
                "excerpt; every risk view must quote the operator's text"
            )
    return True


_CORROBORATION_HAIRCUT = 0.5


def _view_flavor(kind: str, payload: dict, panel, tickers: list[str]) -> str:
    """Is a view stress-flavored, calm-flavored, or neutral vs the hard signals?

    News that predicts more risk (higher vol, fatter tails, spiking
    correlations) is 'stress'-flavored; the opposite is 'calm'. Comparing that
    flavor to the deterministic realized-vol regime is how a news view earns —
    or loses — confidence, without any of it touching returns.
    """
    import numpy as np

    index = {t: j for j, t in enumerate(tickers)}
    if kind == "vol":
        j = index.get(payload["ticker"])
        if j is None:
            return "neutral"
        realized = float(np.std(panel[:, j], ddof=0))
        return "stress" if payload["target_vol"] > realized else "calm"
    if kind == "tail":
        return "stress" if payload["direction"] == "fatter" else "calm"
    if kind == "corr":
        a, b = index.get(payload["ticker_a"]), index.get(payload["ticker_b"])
        if a is None or b is None:
            return "neutral"
        current = float(np.corrcoef(panel[:, a], panel[:, b])[0, 1])
        return "stress" if payload["target_corr"] > current else "calm"
    return "neutral"


def _corroborate_views(typed_views, canonical_views, panel, tickers, regime):
    """Haircut a view's confidence when it contradicts the hard-signal regime.

    Deterministic and boundary-safe: agreement with the realized-vol regime
    keeps full confidence; contradiction halves it before entropy pooling.
    Returns (possibly-reweighted typed views, a per-view corroboration report).
    """
    import dataclasses

    stress = regime.get("regime") == "stress"
    out, report = [], []
    for view, canonical in zip(typed_views, canonical_views):
        flavor = _view_flavor(canonical["type"], canonical, panel, tickers)
        agrees = flavor == "neutral" or (flavor == "stress") == stress
        factor = 1.0 if agrees else _CORROBORATION_HAIRCUT
        adjusted = dataclasses.replace(
            view, confidence=view.confidence * factor)
        out.append(adjusted)
        report.append({
            "label": view.label(), "flavor": flavor,
            "hard_regime": regime.get("regime", "unknown"),
            "corroborated": agrees,
            "confidence_before": view.confidence,
            "confidence_after": adjusted.confidence,
        })
    return out, report


def _validated_risk_views(
    views: list[dict],
) -> tuple[list[VolView | CorrView | TailView], list[dict]]:
    """Validate the extractor's exact schema and build deterministic view types."""
    if not isinstance(views, list):
        raise TypeError("views must be a list of view objects")
    if not views:
        raise ValueError("research.apply_views requires at least one risk view")
    if len(views) > _MAX_RISK_VIEWS:
        raise ValueError(
            f"research.apply_views accepts at most {_MAX_RISK_VIEWS} views, "
            f"got {len(views)}"
        )

    fields_by_type = {
        "vol": {
            "type", "ticker", "target_vol", "confidence", "source_quote",
        },
        "corr": {
            "type", "ticker_a", "ticker_b", "target_corr", "confidence",
            "source_quote",
        },
        "tail": {
            "type", "ticker", "direction", "confidence", "source_quote",
        },
    }
    typed: list[VolView | CorrView | TailView] = []
    canonical: list[dict] = []
    for index, raw in enumerate(views, start=1):
        if not isinstance(raw, dict):
            raise TypeError(f"view {index} must be an object")
        if not all(isinstance(key, str) for key in raw):
            raise TypeError(f"view {index} field names must be strings")

        view_type = raw.get("type")
        if not isinstance(view_type, str):
            raise ValueError(
                f"view {index} field 'type' must be one of "
                f"{sorted(fields_by_type)}"
            )
        if view_type not in fields_by_type:
            if view_type in {"return", "price", "directional", "alpha"}:
                raise ValueError(
                    f"view {index} type {view_type!r} is forbidden: expected-"
                    "return and price-direction views may not enter qlab"
                )
            raise ValueError(
                f"view {index} field 'type' must be one of "
                f"{sorted(fields_by_type)}"
            )

        required = fields_by_type[view_type]
        extra = set(raw) - required
        if extra:
            return_fields = sorted(
                key for key in extra
                if any(token in key.lower()
                       for token in ("return", "price", "alpha", "directional"))
            )
            if return_fields:
                raise ValueError(
                    f"view {index} contains forbidden return/price fields "
                    f"{return_fields}; only vol/corr/tail risk views qualify"
                )
            raise ValueError(
                f"view {index} has unexpected fields {sorted(extra)}"
            )
        missing = required - set(raw)
        if missing:
            raise ValueError(
                f"view {index} is missing required fields {sorted(missing)}"
            )

        confidence = _view_number(raw["confidence"], "confidence", index)
        if not 0.0 < confidence <= _MAX_VIEW_CONFIDENCE:
            raise ValueError(
                f"view {index} confidence must be in "
                f"(0, {_MAX_VIEW_CONFIDENCE}], got {confidence}"
            )
        source_quote = _view_string(
            raw["source_quote"], "source_quote", index
        )

        if view_type == "vol":
            ticker = _view_string(raw["ticker"], "ticker", index)
            target_vol = _view_number(
                raw["target_vol"], "target_vol", index
            )
            typed.append(VolView(ticker, target_vol, confidence))
            canonical.append({
                "type": "vol",
                "ticker": ticker,
                "target_vol": target_vol,
                "confidence": confidence,
                "source_quote": source_quote,
            })
        elif view_type == "corr":
            ticker_a = _view_string(raw["ticker_a"], "ticker_a", index)
            ticker_b = _view_string(raw["ticker_b"], "ticker_b", index)
            target_corr = _view_number(
                raw["target_corr"], "target_corr", index
            )
            typed.append(CorrView(
                ticker_a, ticker_b, target_corr, confidence
            ))
            canonical.append({
                "type": "corr",
                "ticker_a": ticker_a,
                "ticker_b": ticker_b,
                "target_corr": target_corr,
                "confidence": confidence,
                "source_quote": source_quote,
            })
        else:
            ticker = _view_string(raw["ticker"], "ticker", index)
            direction = _view_string(
                raw["direction"], "direction", index
            )
            typed.append(TailView(ticker, direction, confidence))
            canonical.append({
                "type": "tail",
                "ticker": ticker,
                "direction": direction,
                "confidence": confidence,
                "source_quote": source_quote,
            })

    return typed, canonical


def _index_date(value) -> date:
    """Normalize one price-index value without assuming a pandas index type."""
    converted = value.date() if hasattr(value, "date") else value
    if isinstance(converted, date):
        return converted
    return date.fromisoformat(str(converted)[:10])


def _data_integrity_findings(
    snapshot: DataSnapshot,
    lookback_days: int,
) -> list[dict]:
    """Build the deterministic per-ticker integrity table (never raw prices)."""
    findings: list[dict] = []
    for ticker in snapshot.tickers:
        series = snapshot.prices[ticker].tail(lookback_days)
        valid = series.dropna()
        n_obs = int(valid.shape[0])
        missing_bars = int(series.isna().sum())
        coverage = n_obs / lookback_days

        valid_dates = [_index_date(value) for value in valid.index]
        last_bar = valid_dates[-1] if valid_dates else None
        last_bar_age_days = (
            max(0, (snapshot.as_of - last_bar).days)
            if last_bar is not None
            else None
        )
        longest_gap_days = max(
            (right - left).days
            for left, right in zip(valid_dates, valid_dates[1:])
        ) if len(valid_dates) > 1 else 0

        raw_max_return = series.astype(float).pct_change(
            fill_method=None
        ).abs().max()
        non_finite_return = (
            raw_max_return == raw_max_return
            and not math.isfinite(float(raw_max_return))
        )
        max_abs_return = (
            float(raw_max_return)
            if raw_max_return == raw_max_return
            and math.isfinite(float(raw_max_return))
            else None
        )

        issues: list[str] = []
        if missing_bars > _DATA_INTEGRITY_THRESHOLDS["max_missing_bars"]:
            issues.append("missing_bars")
        if (
            last_bar_age_days is None
            or last_bar_age_days
            > _DATA_INTEGRITY_THRESHOLDS["max_last_bar_age_days"]
        ):
            issues.append("stale_series")
        if (
            longest_gap_days
            > _DATA_INTEGRITY_THRESHOLDS["max_longest_gap_days"]
        ):
            issues.append("long_gap")
        if non_finite_return or (
            max_abs_return is not None
            and max_abs_return
            > _DATA_INTEGRITY_THRESHOLDS["max_abs_1d_return"]
        ):
            issues.append("extreme_jump")
        if coverage < _DATA_INTEGRITY_THRESHOLDS["min_span_coverage"]:
            issues.append("insufficient_span")

        findings.append({
            "ticker": ticker,
            "last_bar": last_bar.isoformat() if last_bar else None,
            "last_bar_age_days": last_bar_age_days,
            "longest_gap_days": int(longest_gap_days),
            "missing_bars": missing_bars,
            "max_abs_1d_return": (
                round(max_abs_return, 6)
                if max_abs_return is not None
                else None
            ),
            "n_obs": n_obs,
            "lookback_days": int(lookback_days),
            "span_coverage": round(coverage, 4),
            "issues": issues,
            "clean": not issues,
        })
    return findings


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

    # -- deterministic data QA ---------------------------------------------
    @app.tool(name="qa.data_integrity")
    def qa_data_integrity(as_of: str, universe: str = "core",
                          lookback_days: int = 756) -> dict:
        """Check snapshot freshness, gaps, jumps, and span without mutating it."""
        st.budget.charge("qa.data_integrity")
        if isinstance(lookback_days, bool) or not isinstance(lookback_days, int):
            raise TypeError("lookback_days must be an integer")
        if lookback_days < 2:
            raise ValueError("lookback_days must be at least 2")

        d = check_as_of(as_of)
        tickers = load_universe().tickers(universe)
        snap = market.snapshot(
            tickers,
            d,
            lookback_days=lookback_days,
            offline=st.offline,
            seed=st.seed,
        )
        findings = _data_integrity_findings(snap, lookback_days)
        flagged = [row["ticker"] for row in findings if not row["clean"]]
        return {
            "as_of": str(d),
            "universe": universe,
            "source": snap.source,
            "lookback_days": lookback_days,
            "thresholds": dict(_DATA_INTEGRITY_THRESHOLDS),
            "findings": findings,
            "flagged_tickers": flagged,
            "clean": not flagged,
        }

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
    # Five deterministic, price-only reads plus one explicitly optional HMM.
    # The optional tool remains registered without hmmlearn so invoking it can
    # refuse loudly with the exact extra required; none forecasts returns.
    def _regime_snapshot(as_of: str, universe: str, lookback_days: int):
        d = check_as_of(as_of)
        tickers = load_universe().tickers(universe)
        return market.snapshot(tickers, d, lookback_days=lookback_days,
                               offline=st.offline, seed=st.seed)

    @app.tool(name="regime.hmm")
    def regime_hmm(as_of: str, universe: str = "core",
                   lookback_days: int = 756) -> dict:
        """Gaussian-HMM posterior over calm, normal, and stress regimes."""
        st.budget.charge("regime.hmm")
        from qlab.signals.hmm import fit_regime_hmm

        snapshot = _regime_snapshot(as_of, universe, lookback_days)
        fitted = fit_regime_hmm(
            snapshot.log_returns().dropna(how="any"),
            n_states=3,
            seed=st.seed,
        )
        latest = fitted["posteriors"].iloc[-1]
        labels = fitted["state_labels"]
        posterior = {
            labels[int(state)]: float(probability)
            for state, probability in latest.items()
        }
        label = max(posterior, key=posterior.get)
        confidence = posterior[label]
        return {
            "indicator": "hmm",
            "method": "gaussian_hmm_portfolio_return_realized_vol",
            "regime": label,
            "label": label,
            "posterior": {
                state: round(posterior.get(state, 0.0), 6)
                for state in ("calm", "normal", "stress")
            },
            "confidence": round(confidence, 6),
            "as_of": str(latest.name.date()),
            "lookback_days": int(lookback_days),
            "transition_matrix": fitted["transition_matrix"].tolist(),
            "state_labels": {
                str(state): regime
                for state, regime in labels.items()
            },
            "reasoning": (
                f"The Gaussian HMM assigns {confidence:.0%} posterior "
                f"probability to {label}."
            ),
        }

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

    # -- admitted realized-volatility prediction --------------------------
    def research_predict_vol(
        as_of: str,
        universe: str = "core",
        lookback_days: int = 756,
    ) -> dict:
        """Evaluate the research-stage next-21-day realized-vol ridge baseline."""
        from qlab.research.prediction import predict_vol_ridge

        st.budget.charge("research.predict_vol")
        if isinstance(lookback_days, bool) or not isinstance(lookback_days, int):
            raise TypeError("lookback_days must be an integer")
        if lookback_days < 300:
            raise ValueError(
                "research.predict_vol requires at least 300 return observations "
                "for lagged features and purged walk-forward folds"
            )

        d = check_as_of(as_of)
        tickers = load_universe().tickers(universe)
        snapshot = market.snapshot(
            tickers,
            d,
            lookback_days=lookback_days + 1,
            offline=st.offline,
            seed=st.seed,
        )
        panel = snapshot.log_returns().dropna(how="any")
        result = predict_vol_ridge(panel)
        caveats = ["risk prediction only", "research stage"]
        run_spec = {
            "algorithm_id": "vol_prediction_ridge",
            "as_of": str(d),
            "universe": universe,
            "tickers": tickers,
            "lookback_days": lookback_days,
            "source": snapshot.source,
            "mean_ic": result["mean_ic"],
            "ic_stability": result["ic_stability"],
            "ic_std": result["ic_std"],
            "usable": result["usable"],
            "chosen_alpha": result["chosen_alpha"],
            "per_fold": result["per_fold"],
            "n_obs": result["n_obs"],
            "features": result["features"],
            "target": result["target"],
            "horizon_days": result["horizon_days"],
            "embargo_days": result["embargo_days"],
            "admission": result["admission"],
            "dsr_trial_counted": False,
            "caveats": caveats,
        }
        run_id = st.registry.log_run("prediction", run_spec)
        # Prediction validation writes neither a solution nor a backtest row,
        # so the DSR trial universe is unchanged.
        return {
            "run_id": run_id,
            "mean_ic": result["mean_ic"],
            "ic_stability": result["ic_stability"],
            "usable": result["usable"],
            "chosen_alpha": result["chosen_alpha"],
            "per_fold": result["per_fold"],
            "caveats": caveats,
        }

    if owner_only:
        # This executes a research-stage model and is therefore reached only
        # through the one-writer owner and its role-scoped stateless proxy.
        app.tool(name="research.predict_vol")(research_predict_vol)

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
    @app.tool(name="news.fetch")
    def news_fetch(
        as_of: str,
        universe: str = "core",
        lookback_hours: int = 48,
    ) -> dict:
        """Fetch recent, provenance-tagged news headlines for the universe.

        Text only — no market numbers, no writes. This is the owner-side feed
        that supplies the quarantined news-extractor's evidence; the extractor
        never fetches. Offline uses the deterministic synthetic provider.
        """
        from qlab.news.feed import fetch_news

        st.budget.charge("news.fetch")
        d = check_as_of(as_of)
        tickers = load_universe().tickers(universe)
        items = fetch_news(
            str(d), tickers, lookback_hours=lookback_hours, offline=st.offline)
        # A single excerpt string the extractor can quote against, plus the
        # structured items for display/provenance.
        excerpt = "\n".join(
            f"[{it.source} {it.published}] {it.headline}. {it.summary}"
            for it in items)
        return {
            "as_of": str(d),
            "universe": universe,
            "n_items": len(items),
            "items": [it.provenance() | {"headline": it.headline,
                                         "summary": it.summary}
                      for it in items],
            "excerpt": excerpt,
        }

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

    # -- quarantined qualitative risk views -------------------------------
    @app.tool(name="research.apply_views")
    def research_apply_views(
        as_of: str,
        universe: str,
        views: list[dict],
        kl_budget: float = 0.25,
        dry: bool = True,
        excerpt: str = "",
    ) -> dict:
        """Validate and apply up to three risk views to a scenario panel.

        This is a dry research diagnostic only. It records the bounded
        entropy-pooling result, but does not persist conditioned tensors or
        feed a downstream objective, solver, workflow phase, or paper plan.

        ``excerpt`` is the operator-supplied source text. When provided, every
        view's ``source_quote`` must be a whitespace-normalized substring of
        it — a deterministic provenance gate so the quarantine does not rest on
        the extractor's prompt alone. The audit run records whether provenance
        was verified.
        """
        st.budget.charge("research.apply_views")
        if not isinstance(dry, bool):
            raise TypeError("dry must be a boolean")
        if not dry:
            raise PermissionError(
                "research.apply_views supports dry=true only; downstream "
                "conditioning is not wired"
            )
        if not isinstance(excerpt, str):
            raise TypeError("excerpt must be a string")
        budget = _view_number(kl_budget, "kl_budget", 0)
        if budget <= 0.0:
            raise ValueError("kl_budget must be positive")
        typed_views, canonical_views = _validated_risk_views(views)
        provenance_verified = _verify_view_provenance(canonical_views, excerpt)

        d = check_as_of(as_of)
        tickers = load_universe().tickers(universe)
        snapshot = market.snapshot(
            tickers,
            d,
            offline=st.offline,
            seed=st.seed,
        )
        panel = snapshot.log_returns(
            lookback_days=_VIEWS_LOOKBACK_DAYS
        ).dropna(how="any")
        panel_array = panel.to_numpy(dtype=float)
        # Corroborate each news view against the deterministic hard-signal
        # regime before pooling: contradiction costs confidence.
        regime = detect_regime(snapshot)
        corroborated_views, corroboration = _corroborate_views(
            typed_views, canonical_views, panel_array, tickers, regime)
        result = apply_risk_views(
            panel_array,
            tickers,
            corroborated_views,
            kl_budget=budget,
        )
        summary = {
            "kl_total": result.kl_total,
            "kl_per_view": result.kl_per_view,
            "moments_before": result.moments_before,
            "moments_after": result.moments_after,
            "applied_labels": list(result.labels),
            "provenance_verified": provenance_verified,
            "hard_regime": regime.get("regime", "unknown"),
            "corroboration": corroboration,
        }
        run_id = st.registry.log_run("views", {
            "algorithm_id": "entropy_pooling_views",
            "as_of": str(d),
            "universe": universe,
            "tickers": tickers,
            "source": snapshot.source,
            "panel_lookback_days": _VIEWS_LOOKBACK_DAYS,
            "n_scenarios": int(len(panel)),
            "views": canonical_views,
            "kl_budget": budget,
            "dry": True,
            "downstream_conditioning": False,
            "dsr_trial_counted": False,
            "result": summary,
        })
        return {"run_id": run_id, **summary}

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
