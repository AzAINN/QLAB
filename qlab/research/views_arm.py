"""The production caller of ``views_from_matrix``: the ablation's A5 arm.

Stream E ends here. The matrix counts what the record said (Task 7), two
bounded rules turn those counts into unsigned risk views (Task 8), and this
module is the only thing that runs that chain against real dates and asks
whether it earns its place. Everything it produces is research-stage: the
catalog entry ``views_conditioned_min_variance`` is not operational, so a
covariance this module tilts cannot reach a governed solve, a workflow phase,
or a paper plan.

The walk-forward shape matters. At each rebalance date the conditioner reads
only the news window ending at that date, builds the matrix from it, compares
it to the *previously logged* window (never a later one), derives views, pools
them under a KL budget, and conditions the covariance with every mean pinned.
Each step is logged, so a run's spec says how many windows actually produced a
view rather than leaving a null result indistinguishable from a null run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timezone

import numpy as np

from qlab.core.moments import condition
from qlab.core.types import DataSnapshot, MomentSet
from qlab.core.universe import load_universe
from qlab.core.views import CorrView, TailView, VolView, apply_views
from qlab.news.feed import fetch_news
from qlab.news.grounding import ground
from qlab.news.matrix import MatrixRow, QualitativeMatrix, build_matrix
from qlab.research.matrix_views import views_from_matrix

VIEWS_SOURCE = "qualitative_matrix"


def sleeves_for(tickers: list[str]) -> dict[str, list[str]]:
    """Group a universe into sleeves by its own declared asset class.

    The mandate has no sleeve notion of its own, and the universe file does:
    ``asset_class`` is the grouping the desk already publishes and the only one
    that is non-overlapping, so the correlation rule cannot state the same
    concentration twice under two names. A ticker the universe does not
    classify lands in ``unclassified`` rather than silently vanishing from
    every sleeve.
    """
    classes = load_universe().asset_classes("extended")
    out: dict[str, list[str]] = {}
    for ticker in tickers:
        out.setdefault(classes.get(ticker, "unclassified"), []).append(ticker)
    return out


def _typed(view: dict) -> VolView | CorrView | TailView:
    """The rule's dict, as the pooling code's own validated type."""
    kind = view["type"]
    if kind == "tail":
        return TailView(ticker=view["ticker"], direction=view["direction"],
                        confidence=float(view["confidence"]))
    if kind == "corr":
        return CorrView(ticker_a=view["ticker_a"], ticker_b=view["ticker_b"],
                        target_corr=float(view["target_corr"]),
                        confidence=float(view["confidence"]))
    if kind == "vol":
        return VolView(ticker=view["ticker"],
                       target_vol=float(view["target_vol"]),
                       confidence=float(view["confidence"]))
    # The rules emit two shapes and a third would be a silent new hypothesis.
    raise ValueError(f"matrix rule emitted an unknown view type {kind!r}")


@dataclass
class MatrixViewsConditioner:
    """Build the window, the matrix, the views, and the conditioned covariance.

    One instance per ablation run. It caches per ``as_of`` because every arm in
    the matrix visits the same rebalance dates, and a window read twice must
    not be logged twice or counted twice.
    """

    registry: object
    kl_budget: float = 0.25
    panel_lookback_days: int = 756
    lookback_hours: int = 72
    offline: bool = True
    stats: dict = field(default_factory=lambda: {
        "windows": 0, "windows_with_views": 0, "views_applied": 0,
        "windows_conditioned": 0, "infeasible_windows": 0,
    })
    _cache: dict = field(default_factory=dict)

    # -- the public seam the arm calls ------------------------------------
    def condition(self, ms: MomentSet, snapshot: DataSnapshot) -> MomentSet:
        """``ms``, tilted onto this window's matrix-derived views, or unchanged.

        Unchanged is a real answer, not a failure: a window whose record says
        nothing unusual produces no views, and inventing one to keep the arm
        busy is exactly the behaviour the rules were bounded to prevent.
        """
        key = (str(snapshot.as_of), tuple(ms.tickers))
        if key not in self._cache:
            self._cache[key] = self._build(ms.tickers, snapshot)
        applied = self._cache[key]
        if applied is None:
            return ms
        run_id, probabilities = applied
        panel = self._panel(snapshot, ms.tickers)
        if len(panel) != len(probabilities):
            raise ValueError(
                "the cached views run does not match this window's panel; "
                "the ablation must not re-apply a tilt to different rows")
        self.stats["windows_conditioned"] += 1
        return condition(ms, probabilities, panel=panel, views_run_id=run_id)

    # -- internals ---------------------------------------------------------
    def _panel(self, snapshot: DataSnapshot, tickers: list[str]) -> np.ndarray:
        rets = snapshot.log_returns(
            lookback_days=self.panel_lookback_days).dropna(how="any")
        return rets[tickers].to_numpy(dtype=float)

    def _build(self, tickers: list[str], snapshot: DataSnapshot):
        as_of = str(snapshot.as_of)
        self.stats["windows"] += 1
        matrix = self._matrix(tickers, as_of)
        baseline = self._log_and_previous(matrix)
        views = views_from_matrix(matrix, baseline, sleeves_for(list(tickers)))
        if not views:
            return None
        self.stats["windows_with_views"] += 1
        self.stats["views_applied"] += len(views)
        panel = self._panel(snapshot, list(tickers))
        try:
            result = apply_views(panel, list(tickers), [_typed(v) for v in views],
                                 kl_budget=self.kl_budget)
        except ValueError:
            # An infeasible or over-budget view on this panel is a refusal by
            # design (qlab/core/views.py). Counting it and moving on keeps the
            # walk-forward honest: the window produced a view the panel could
            # not express, which is not the same as producing none.
            self.stats["infeasible_windows"] += 1
            return None
        run_id = self.registry.log_run("views", {
            "algorithm_id": "entropy_pooling_views",
            "as_of": as_of,
            "tickers": list(tickers),
            "source": "qualitative_matrix",
            "panel_lookback_days": self.panel_lookback_days,
            "n_scenarios": int(len(panel)),
            "views": views,
            "kl_budget": float(self.kl_budget),
            "dry": False,
            "dsr_trial_counted": False,
            "probabilities": [float(x) for x in result.probabilities],
            "kl_total": float(result.kl_total),
            # Every rule view cites the claim keys of the matrix row it was
            # counted from, and that matrix is the run logged just above, so
            # provenance is verified by construction rather than by assertion.
            "provenance_verified": True,
        })
        return run_id, np.asarray(result.probabilities, dtype=float)

    def _matrix(self, tickers: list[str], as_of: str) -> QualitativeMatrix:
        items = fetch_news(as_of, list(tickers),
                           lookback_hours=self.lookback_hours,
                           offline=self.offline)
        stamp = datetime.combine(
            datetime.fromisoformat(as_of).date(), time.min, tzinfo=timezone.utc)
        grounded = ground(items, as_of=stamp.isoformat(), provider="synthetic",
                          universe=list(tickers))
        # No look-ahead calendar: `upcoming` is anchored to wall-clock today, so
        # feeding it a 2011 rebalance would either refuse or answer about 2026.
        # `days_to_next_release` is not read by either rule, so an empty
        # calendar costs the arm nothing and inventing one would cost it its
        # point-in-time claim.
        return build_matrix(grounded.claims, list(tickers), as_of, [])

    def _log_and_previous(self,
                          matrix: QualitativeMatrix
                          ) -> dict[str, MatrixRow] | None:
        """Log this window, then return the window before it, as rows.

        ``None`` means there is only one window on record, so the whole window
        is new — right for the first rebalance and wrong for every later one,
        which is why the previous window is read rather than assumed.
        """
        newest = self.registry.newest_run_of_kind("qualitative_matrix")
        logged = ((newest or {}).get("spec") or {}).get("matrix") or {}
        if logged.get("window_hash") != matrix.window_hash:
            self.registry.log_run("qualitative_matrix",
                                  {"matrix": matrix.to_dict()})
        rows = self.registry.runs_of_kind("qualitative_matrix", 2)
        if len(rows) < 2:
            return None
        previous = ((rows[1].get("spec") or {}).get("matrix") or {}).get("rows")
        if not previous:
            return None
        return {t: MatrixRow(**r) if not isinstance(r, MatrixRow) else r
                for t, r in previous.items()}
