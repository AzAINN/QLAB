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
from qlab.core.views import CorrView, TailView, apply_views
from qlab.news.feed import fetch_news
from qlab.news.grounding import ground
from qlab.news.matrix import MatrixRow, QualitativeMatrix, build_matrix
from qlab.research.matrix_views import views_from_matrix
from qlab.research.view_provenance import verify_view_provenance

VIEWS_SOURCE = "qualitative_matrix"
# The registry is shared with a live owner that logs its own matrices from
# today's real news, for whatever universe it is running. An arm that reads
# those as its own past is reading a later day and another universe: every
# matrix this module writes is stamped, and only stamped ones are read back.
ARM_MATRIX_SOURCE = "ablation_a5"
# How many of the arm's OWN earlier windows may name a different universe
# before the baseline search gives up. Not a bound on registry traffic: source
# and date are SQL predicates, so foreign matrices never reach this loop.
_BASELINE_CANDIDATES = 64


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


def _typed(view: dict) -> CorrView | TailView:
    """The rule's dict, as the pooling code's own validated type."""
    kind = view["type"]
    if kind == "tail":
        return TailView(ticker=view["ticker"], direction=view["direction"],
                        confidence=float(view["confidence"]))
    if kind == "corr":
        return CorrView(ticker_a=view["ticker_a"], ticker_b=view["ticker_b"],
                        target_corr=float(view["target_corr"]),
                        confidence=float(view["confidence"]))
    # The rules emit exactly these two shapes; a third — a vol view included —
    # would be a new hypothesis, and translating one here in advance would hide
    # its arrival behind code that already accepts it.
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
        "unverified_windows": 0,
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
        spec = {
            "algorithm_id": "entropy_pooling_views",
            "as_of": as_of,
            "tickers": list(tickers),
            "source": VIEWS_SOURCE,
            "panel_lookback_days": self.panel_lookback_days,
            "views": views,
            "kl_budget": float(self.kl_budget),
            "dry": False,
            "dsr_trial_counted": False,
            "provenance_source": "matrix_rule",
            "provenance_verified": self._verified(matrix, views),
        }
        if not spec["provenance_verified"]:
            # Recorded, then refused: a view whose cited claim is not in the
            # matrix it was supposedly counted from is exactly what the
            # quarantine exists to stop, and the arm must not tilt on it.
            self.stats["unverified_windows"] += 1
            self.registry.log_run("views", spec)
            return None
        # Counted only past the gate: a window whose views were refused applied
        # none of them, and counting them earlier makes the summary claim the
        # arm acted on evidence it rejected.
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
        spec.update({
            "n_scenarios": int(len(panel)),
            "probabilities": [float(x) for x in result.probabilities],
            "kl_total": float(result.kl_total),
        })
        run_id = self.registry.log_run("views", spec)
        return run_id, np.asarray(result.probabilities, dtype=float)

    def _verified(self, matrix: QualitativeMatrix, views: list[dict]) -> bool:
        """Is every rule view cited to a claim key of the matrix it came from?

        Derived, never asserted: the arm runs the same check
        ``research.apply_views`` runs over an extractor's views, against the
        claim keys of the matrix it has just logged. The shared helper raises
        when a view cites a key the archive does not hold; for a rule-built
        view that is a derived FALSE — the window is recorded and not
        conditioned on — rather than a crash mid-walk.
        """
        keys = {str(k) for r in matrix.rows.values() for k in r.claim_keys}
        try:
            return verify_view_provenance(views, "", keys)
        except ValueError:
            return False

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
        """Log this window, then return the arm's own previous window, as rows.

        "Previous" is three conditions, not one: logged by this arm, dated
        strictly before this window, and over the same tickers. The second
        newest ``qualitative_matrix`` run satisfies none of them on a shared
        registry — the owner logs matrices from today's live news for whatever
        universe it is running, and reading one as this window's past is
        look-ahead and a universe swap in the same line.

        ``None`` means this arm has no earlier window for this universe, so the
        whole window is new — right for the first rebalance, wrong for a later
        one, which is why the baseline is read rather than assumed.
        """
        # log_run content-hashes its spec and inserts ON CONFLICT DO NOTHING,
        # so re-logging an identical window is already a no-op; a hand-rolled
        # "have I logged this" check would only add a second bounded scan.
        self.registry.log_run(
            "qualitative_matrix",
            {"source": ARM_MATRIX_SOURCE, "matrix": matrix.to_dict()})
        tickers = set(matrix.rows)
        # Source and date are SQL predicates, so the rows that come back are
        # already only this arm's own earlier windows, newest first. The only
        # thing left to check here is the universe, and the limit is a bound on
        # how many of THIS ARM's earlier windows may name a different one.
        for run in self.registry.matrix_runs(
                source=ARM_MATRIX_SOURCE, as_of_before=matrix.as_of,
                limit=_BASELINE_CANDIDATES):
            rows = ((run.get("spec") or {}).get("matrix") or {}).get("rows") or {}
            if set(rows) == tickers:
                return {t: MatrixRow(**r) if not isinstance(r, MatrixRow) else r
                        for t, r in rows.items()}
        return None
