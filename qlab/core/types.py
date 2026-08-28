"""Shared data contracts for the whole system.

Design rule (research-plan §2.2, invariant 1 — *ref-passing, never
data-passing*): the heavy numeric objects here (``DataSnapshot``,
``MomentSet``, ``Objective``) carry numpy tensors for *in-process* use by the
quant core and the solvers. When these cross the MCP boundary they are stored
in the registry and only their **content hash / id + diagnostics** are returned
to an agent — matrices and price panels never enter model context.

Everything that an agent authors or that must be validated at a boundary
(``Weights``, ``Decision``, ``AssetMeta``, ``PriceBar``) is a pydantic model.
The compute-heavy internals are dataclasses to stay cheap and numpy-native.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Boundary-crossing, validated models (pydantic)
# ---------------------------------------------------------------------------
class AssetMeta(BaseModel):
    """Static metadata for one instrument."""

    ticker: str
    name: str = ""
    asset_class: str = "unknown"


class PriceBar(BaseModel):
    """A single OHLCV bar. Adjusted close is what the pipeline actually uses."""

    ticker: str
    dt: date
    open: float
    high: float
    low: float
    close: float
    adj_close: float
    volume: float = 0.0


class Weights(BaseModel):
    """A portfolio allocation. Self-validating: long-only + (near) fully invested.

    This is the object an optimizer produces and the trader consumes, so its
    invariants are enforced here rather than trusted downstream.
    """

    tickers: list[str]
    values: list[float]
    normalized: bool = True

    @field_validator("values")
    @classmethod
    def _finite(cls, v: list[float]) -> list[float]:
        arr = np.asarray(v, dtype=float)
        if not np.all(np.isfinite(arr)):
            raise ValueError("weights contain non-finite values")
        return v

    def model_post_init(self, _ctx: Any) -> None:  # noqa: D401
        if len(self.tickers) != len(self.values):
            raise ValueError("tickers and values length mismatch")

    def as_series(self) -> "pd.Series":
        return pd.Series(self.values, index=self.tickers, dtype=float)

    def as_array(self) -> np.ndarray:
        return np.asarray(self.values, dtype=float)

    @property
    def total(self) -> float:
        return float(np.sum(self.values))

    @classmethod
    def from_series(cls, s: "pd.Series", normalized: bool = True) -> "Weights":
        return cls(tickers=list(s.index), values=[float(x) for x in s.values],
                   normalized=normalized)

    @classmethod
    def equal(cls, tickers: list[str]) -> "Weights":
        n = len(tickers)
        return cls(tickers=list(tickers), values=[1.0 / n] * n)


class Decision(BaseModel):
    """A logged judgment call — the reflection-loop artifact (research-plan §3).

    The *server is the referee of facts; the agent is the author of choices.*
    Every judgment an agent makes (which estimation window, how much shrinkage,
    which regime) is written here with its rationale, so it can later be scored
    against what actually happened. Undocumented judgment cannot enter a
    backtest — this schema is enforced on ``registry.log_decision``.
    """

    decision_id: str = ""
    as_of: date
    kind: Literal[
        "estimation_window", "shrinkage", "regime", "arm_selection",
        "rebalance_gate", "experiment_design", "other",
    ]
    choice: dict[str, Any]
    rationale: str
    challenger_view: str | None = None          # the adversarial/opposite case
    alternatives_considered: list[str] = Field(default_factory=list)
    # Filled in later by the reflection loop when the period resolves:
    realized_outcome: dict[str, Any] | None = None
    reflection: str | None = None

    def content_hash(self) -> str:
        payload = self.model_dump(exclude={"decision_id", "realized_outcome", "reflection"})
        return _hash_obj(payload)


# ---------------------------------------------------------------------------
# Compute-heavy internals (numpy-native dataclasses)
# ---------------------------------------------------------------------------
@dataclass
class DataSnapshot:
    """A point-in-time price panel with a look-ahead tripwire.

    ``DataSnapshot(as_of)`` **provably cannot leak** future information: the
    price frame is truncated to rows on/before ``as_of`` at construction, and
    :meth:`window` only ever looks backward. This is milestone M0's exit
    criterion (research-plan §9) and invariant behind honest agent judgment.
    """

    tickers: list[str]
    prices: pd.DataFrame            # index = dates, columns = tickers (adj close)
    as_of: date
    source: str = "synthetic"

    def __post_init__(self) -> None:
        # The tripwire: hard-truncate to as_of. Any attempt to read past this
        # simply returns nothing to read.
        cutoff = pd.Timestamp(self.as_of)
        self.prices = self.prices.loc[self.prices.index <= cutoff]
        if list(self.prices.columns) != list(self.tickers):
            self.prices = self.prices[self.tickers]

    def window(self, lookback_days: int) -> pd.DataFrame:
        """Return the trailing ``lookback_days`` rows ending at ``as_of``."""
        return self.prices.tail(lookback_days)

    def log_returns(self, lookback_days: int | None = None) -> pd.DataFrame:
        px = self.prices if lookback_days is None else self.window(lookback_days + 1)
        return np.log(px / px.shift(1)).dropna(how="all")

    def content_hash(self) -> str:
        h = hashlib.sha256()
        h.update(str(self.as_of).encode())
        h.update(",".join(self.tickers).encode())
        # hash the values, not the (float-formatted) repr, for stability
        h.update(np.ascontiguousarray(self.prices.to_numpy(dtype=float)).tobytes())
        return h.hexdigest()[:16]


@dataclass
class MomentSet:
    """Estimated co-moments of asset returns at a point in time.

    Holds up to fourth order. ``mu`` is optional and is *deliberately absent*
    from the primary risk-only objective — return estimation is the hardest
    open problem and quantum doesn't touch it (research-plan §4). ``coskew`` and
    ``cokurt`` are the higher-order tensors the MVSK objective consumes.
    """

    tickers: list[str]
    as_of: date
    cov: np.ndarray                             # (n, n)
    mu: np.ndarray | None = None                # (n,)   optional expected returns
    coskew: np.ndarray | None = None            # (n, n, n)
    cokurt: np.ndarray | None = None            # (n, n, n, n)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    # Lineage, deliberately LAST and deliberately outside ``content_hash``:
    # a conditioned set differs from its parent in the tensors, which the
    # hash already covers, and hashing lineage would renumber every moment
    # set already logged.
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.tickers)

    def content_hash(self) -> str:
        h = hashlib.sha256()
        h.update(",".join(self.tickers).encode())
        h.update(str(self.as_of).encode())
        for arr in (self.mu, self.cov, self.coskew, self.cokurt):
            if arr is not None:
                h.update(np.ascontiguousarray(arr, dtype=float).tobytes())
        return h.hexdigest()[:16]

    def summary(self) -> dict[str, Any]:
        """Lightweight diagnostics safe to return to an agent (no raw tensors)."""
        out: dict[str, Any] = {
            "n": self.n,
            "tickers": self.tickers,
            "as_of": str(self.as_of),
            "avg_vol": float(np.sqrt(np.mean(np.diag(self.cov)))),
            "avg_corr": _avg_offdiag_corr(self.cov),
            "has_coskew": self.coskew is not None,
            "has_cokurt": self.cokurt is not None,
        }
        out.update(self.diagnostics)
        # After the diagnostics update: lineage is what the referee reads,
        # and an estimator diagnostic must never be able to shadow it.
        out["provenance"] = dict(self.provenance)
        return out


@dataclass
class Objective:
    """The *one polynomial source of truth* (research-plan §2.2, invariant 4).

    An objective is built once as coefficient tensors and can be compiled to
    (a) a scipy callable, (b) an offline pseudo-Boolean/Ising construction
    report, and (c) a Dirac-3 continuous-HUBO payload — so any divergence
    between representations is encoding drift, not solver quality. See
    :mod:`qlab.core.objective`.

    ``form`` selects which terms are active. ``sense='min'`` throughout: we
    minimise variance, minus a coskew reward, plus a cokurt penalty.
    """

    form: Literal[
        "min_variance", "mvsk", "max_utility", "risk_parity",
        "scenario_cvar", "selection_qubo", "discretized_mv",
    ]
    tickers: list[str]
    cov: np.ndarray
    mu: np.ndarray | None = None
    coskew: np.ndarray | None = None
    cokurt: np.ndarray | None = None
    skew_lambda: float = 0.0
    kurt_lambda: float = 0.0
    risk_aversion: float = 1.0
    sense: Literal["min"] = "min"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.tickers)

    def content_hash(self) -> str:
        payload = {
            "form": self.form,
            "tickers": self.tickers,
            "skew_lambda": self.skew_lambda,
            "kurt_lambda": self.kurt_lambda,
            "risk_aversion": self.risk_aversion,
            "extra": _jsonable(self.extra),
        }
        h = hashlib.sha256(_canonical(payload).encode())
        for arr in (self.mu, self.cov, self.coskew, self.cokurt):
            if arr is not None:
                h.update(np.ascontiguousarray(arr, dtype=float).tobytes())
        return h.hexdigest()[:16]


@dataclass
class SolveResult:
    """Uniform output shared by every algorithm adapter."""

    weights: Weights
    objective_value: float
    solver: str
    status: str = "optimal"
    wall_clock_s: float = 0.0
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "solver": self.solver,
            "status": self.status,
            "objective_value": self.objective_value,
            "wall_clock_s": self.wall_clock_s,
            "weights": dict(zip(self.weights.tickers, self.weights.values)),
            "diagnostics": _jsonable(self.diagnostics),
        }


# ---------------------------------------------------------------------------
# small serialization / hashing helpers
# ---------------------------------------------------------------------------
def _canonical(obj: Any) -> str:
    """Deterministic JSON for content hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _hash_obj(obj: Any) -> str:
    return hashlib.sha256(_canonical(obj).encode()).hexdigest()[:16]


def _jsonable(obj: Any) -> Any:
    """Best-effort conversion of numpy/pandas/datetime into JSON-native types."""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return obj


def _avg_offdiag_corr(cov: np.ndarray) -> float:
    d = np.sqrt(np.clip(np.diag(cov), 1e-18, None))
    corr = cov / np.outer(d, d)
    n = corr.shape[0]
    if n < 2:
        return 0.0
    mask = ~np.eye(n, dtype=bool)
    return float(np.mean(corr[mask]))
