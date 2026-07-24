"""The trading mandate — hard limits enforced in deterministic code.

Every limit in ``mandate.yaml`` is checked here before a plan may advance from
``proposed`` to ``checked``. This is the difference between governance that is
*architectural* and governance that is *advisory* (research-plan §3, §8.1): the
agent authors targets, but the mandate — not the model — decides what is allowed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

from qlab.core.costs import (
    DEFAULT_ADV_NOTIONAL,
    DEFAULT_COMMISSION_BPS,
    DEFAULT_IMPACT_K,
    DEFAULT_SPREAD_BPS,
)
from qlab.core.universe import load_universe
from qlab.paths import data_path

_PERMITTED_UNIVERSE_TIERS = frozenset({"core", "extended"})
DrawdownTier = Literal["none", "warning", "control", "breaker"]


def tier(
    drawdown: float,
    *,
    warning: float = 0.05,
    control: float = 0.10,
    breaker: float = 0.15,
) -> DrawdownTier:
    """Classify a trailing drawdown against deterministic circuit-breaker tiers."""
    values = {
        "drawdown": float(drawdown),
        "warning": float(warning),
        "control": float(control),
        "breaker": float(breaker),
    }
    for name, value in values.items():
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
    if not 0.0 < values["warning"] < values["control"] < values["breaker"] <= 1.0:
        raise ValueError(
            "drawdown tiers must satisfy 0 < warning < control < breaker <= 1"
        )
    if values["drawdown"] >= values["breaker"]:
        return "breaker"
    if values["drawdown"] >= values["control"]:
        return "control"
    if values["drawdown"] >= values["warning"]:
        return "warning"
    return "none"


class MandateViolation(Exception):
    """Raised when a proposed action breaches the mandate. Fatal by design."""


@dataclass(frozen=True)
class DrawdownTiers:
    """Configurable pre-kill-switch controls; the trailing kill switch is separate."""

    warning: float = 0.05
    control: float = 0.10
    breaker: float = 0.15

    def __post_init__(self) -> None:
        tier(
            0.0,
            warning=self.warning,
            control=self.control,
            breaker=self.breaker,
        )

    def tier(self, drawdown: float) -> DrawdownTier:
        return tier(
            drawdown,
            warning=self.warning,
            control=self.control,
            breaker=self.breaker,
        )


@dataclass(frozen=True)
class CostConfig:
    """Mandated assumptions for transaction-cost estimates."""

    spread_bps: float = DEFAULT_SPREAD_BPS
    commission_bps: float = DEFAULT_COMMISSION_BPS
    impact_k: float = DEFAULT_IMPACT_K
    adv_notional: dict[str, float] = field(
        default_factory=lambda: {"default": DEFAULT_ADV_NOTIONAL}
    )
    # Net-alpha gate assumptions (quant-book bands): a rebalance must "buy"
    # more than it costs. The benefit of closing drift toward the reviewed
    # policy is a mandated assumption, not a forecast; the haircut discounts
    # backtest evidence to live expectations; the multiplier is the safety
    # margin on estimated costs; the equity cap refuses pathological plans
    # outright.
    rebalance_benefit_bps: float = 20.0
    live_haircut: float = 0.5
    safety_multiplier: float = 1.5
    max_cost_bps_of_equity: float = 25.0

    def __post_init__(self) -> None:
        # The gate's semantics require these ranges; a "haircut" above 1
        # would amplify benefit and a multiplier below 1 would shrink the
        # safety margin — silently inverting the gate.
        if not 0.0 < self.live_haircut <= 1.0:
            raise ValueError("costs.live_haircut must be in (0, 1]")
        if self.safety_multiplier < 1.0:
            raise ValueError("costs.safety_multiplier must be >= 1")
        if self.rebalance_benefit_bps <= 0:
            raise ValueError("costs.rebalance_benefit_bps must be positive")
        if self.max_cost_bps_of_equity <= 0:
            raise ValueError("costs.max_cost_bps_of_equity must be positive")

    def adv_for(self, ticker: str) -> float:
        """Return a ticker override or the conservative configured default."""
        if ticker in self.adv_notional:
            return float(self.adv_notional[ticker])
        return float(self.adv_notional.get("default", DEFAULT_ADV_NOTIONAL))

    def __getitem__(self, key: str):
        """Allow config-style access while retaining typed attributes."""
        try:
            return getattr(self, key)
        except AttributeError as exc:
            raise KeyError(key) from exc


@dataclass
class Mandate:
    paper_capital: float = 10000.0
    base_currency: str = "USD"
    universe_whitelist: list[str] = field(default_factory=list)
    universe_tier: str = "core"
    long_only: bool = True
    fully_invested: bool = True
    max_weight_per_asset: float = 0.40
    min_weight_per_asset: float = 0.0
    max_turnover_per_rebalance: float = 0.50
    max_orders_per_day: int = 20
    order_type: str = "marketable_limit"
    max_gross_exposure: float = 1.0
    stress_vol_limit: float = 0.30
    drawdown_tiers: DrawdownTiers = field(default_factory=DrawdownTiers)
    trailing_drawdown_pct: float = 0.15
    drift_band_pct: float = 0.05
    cadence: str = "quarterly"
    regime_triggered: bool = True
    defensive_targets: dict[str, float] = field(default_factory=dict)
    allow_fractional: bool = True
    operational_policy: str = "hrp"
    costs: CostConfig = field(default_factory=CostConfig)

    def __post_init__(self) -> None:
        for name, value in (
            ("max_gross_exposure", self.max_gross_exposure),
            ("stress_vol_limit", self.stress_vol_limit),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"mandate {name} must be finite and positive")

    # -- checks -------------------------------------------------------------
    def check_targets(self, targets: dict[str, float], tol: float = 1e-4) -> None:
        """Validate a target weight map. Raises :class:`MandateViolation`."""
        for t in targets:
            if t not in self.universe_whitelist:
                raise MandateViolation(f"{t} is not in the universe whitelist")
        vals = list(targets.values())
        if self.long_only and any(v < -tol for v in vals):
            raise MandateViolation("long-only mandate: negative weight proposed")
        total = sum(vals)
        gross = sum(abs(v) for v in vals)
        if self.fully_invested and abs(total - 1.0) > 1e-2:
            raise MandateViolation(f"fully-invested mandate: weights sum to {total:.4f}")
        if gross > self.max_gross_exposure + tol:
            raise MandateViolation(
                f"gross exposure {gross:.4f} exceeds cap "
                f"{self.max_gross_exposure:.4f}"
            )
        for t, v in targets.items():
            if v > self.max_weight_per_asset + tol:
                raise MandateViolation(
                    f"{t} weight {v:.3f} exceeds cap {self.max_weight_per_asset}")

    def check_turnover(self, turnover: float) -> None:
        if turnover > self.max_turnover_per_rebalance + 1e-6:
            raise MandateViolation(
                f"turnover {turnover:.3f} exceeds max "
                f"{self.max_turnover_per_rebalance}")

    def check_order_count(self, n_orders: int) -> None:
        if n_orders > self.max_orders_per_day:
            raise MandateViolation(
                f"{n_orders} orders exceeds daily cap {self.max_orders_per_day}")

    def drawdown_breached(self, equity: float, high_water_mark: float) -> bool:
        """True if the trailing-drawdown kill-switch should fire."""
        if high_water_mark <= 0:
            return False
        drawdown = 1.0 - equity / high_water_mark
        return drawdown > self.trailing_drawdown_pct

    def drawdown_tier(self, drawdown: float) -> DrawdownTier:
        """Return the configured pre-kill-switch tier for ``drawdown``."""
        return self.drawdown_tiers.tier(drawdown)


def _cost_number(value, name: str, *, positive: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"mandate costs.{name} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"mandate costs.{name} must be finite")
    if number < 0 or (positive and number <= 0):
        condition = "positive" if positive else "non-negative"
        raise ValueError(f"mandate costs.{name} must be {condition}")
    return number


def _load_costs(raw: object) -> CostConfig:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("mandate costs must be a mapping")

    adv_raw = raw.get("adv_notional", DEFAULT_ADV_NOTIONAL)
    if isinstance(adv_raw, dict):
        adv_notional = {
            str(ticker): _cost_number(value, f"adv_notional.{ticker}", positive=True)
            for ticker, value in adv_raw.items()
        }
        adv_notional.setdefault("default", DEFAULT_ADV_NOTIONAL)
    else:
        adv_notional = {
            "default": _cost_number(adv_raw, "adv_notional", positive=True)
        }

    return CostConfig(
        spread_bps=_cost_number(
            raw.get("spread_bps", DEFAULT_SPREAD_BPS), "spread_bps"
        ),
        commission_bps=_cost_number(
            raw.get("commission_bps", DEFAULT_COMMISSION_BPS), "commission_bps"
        ),
        impact_k=_cost_number(raw.get("impact_k", DEFAULT_IMPACT_K), "impact_k"),
        adv_notional=adv_notional,
        rebalance_benefit_bps=_cost_number(
            raw.get("rebalance_benefit_bps", 20.0), "rebalance_benefit_bps",
            positive=True),
        live_haircut=_cost_number(
            raw.get("live_haircut", 0.5), "live_haircut", positive=True),
        safety_multiplier=_cost_number(
            raw.get("safety_multiplier", 1.5), "safety_multiplier",
            positive=True),
        max_cost_bps_of_equity=_cost_number(
            raw.get("max_cost_bps_of_equity", 25.0), "max_cost_bps_of_equity",
            positive=True),
    )


def _load_defensive_targets(raw: object) -> dict[str, float]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not raw:
        raise ValueError("mandate defensive_targets must be a non-empty mapping")
    targets: dict[str, float] = {}
    for raw_ticker, raw_weight in raw.items():
        ticker = str(raw_ticker)
        if isinstance(raw_weight, bool):
            raise ValueError(f"mandate defensive_targets.{ticker} must be numeric")
        try:
            weight = float(raw_weight)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"mandate defensive_targets.{ticker} must be numeric"
            ) from exc
        if not math.isfinite(weight):
            raise ValueError(
                f"mandate defensive_targets.{ticker} must be finite"
            )
        targets[ticker] = weight
    return targets


def load_mandate(path: str | Path | None = None) -> Mandate:
    """Load and flatten ``mandate.yaml`` into a :class:`Mandate`."""
    p = Path(path) if path else data_path("mandate.yaml")
    with open(p, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError("mandate config must be a mapping")

    configured_tier = raw.get("universe_tier")
    if configured_tier is not None and (
        not isinstance(configured_tier, str) or not configured_tier.strip()
    ):
        raise ValueError("mandate universe_tier must be a non-empty string")
    universe_tier = configured_tier.strip() if configured_tier else "core"
    if universe_tier not in _PERMITTED_UNIVERSE_TIERS:
        permitted = ", ".join(sorted(_PERMITTED_UNIVERSE_TIERS))
        raise MandateViolation(
            f"mandate universe_tier {universe_tier!r} requires algorithm catalog "
            f"promotion before paper trading; permitted tiers: {permitted}"
        )

    if configured_tier is not None or "universe_whitelist" not in raw:
        universe_whitelist = load_universe().tickers(universe_tier)
    else:
        configured_whitelist = raw["universe_whitelist"]
        if not isinstance(configured_whitelist, list):
            raise ValueError("mandate universe_whitelist must be a list")
        universe_whitelist = list(configured_whitelist)

    acct = raw.get("account", {})
    con = raw.get("constraints", {})
    ks = raw.get("kill_switch", {})
    rb = raw.get("rebalance", {})
    ex = raw.get("execution", {})
    allocation = raw.get("allocation", {})
    tier_raw = raw.get("drawdown_tiers", {})
    if not isinstance(tier_raw, dict):
        raise ValueError("mandate drawdown_tiers must be a mapping")
    drawdown_tiers = DrawdownTiers(
        warning=float(tier_raw.get("warning_pct", 0.05)),
        control=float(tier_raw.get("control_pct", 0.10)),
        breaker=float(tier_raw.get("breaker_pct", 0.15)),
    )
    costs = _load_costs(raw.get("costs", {}))
    defensive_targets = _load_defensive_targets(raw.get("defensive_targets"))
    mandate = Mandate(
        paper_capital=float(acct.get("paper_capital", 10000.0)),
        base_currency=acct.get("base_currency", "USD"),
        universe_whitelist=universe_whitelist,
        universe_tier=universe_tier,
        long_only=bool(con.get("long_only", True)),
        fully_invested=bool(con.get("fully_invested", True)),
        max_weight_per_asset=float(con.get("max_weight_per_asset", 0.40)),
        min_weight_per_asset=float(con.get("min_weight_per_asset", 0.0)),
        max_turnover_per_rebalance=float(con.get("max_turnover_per_rebalance", 0.50)),
        max_orders_per_day=int(con.get("max_orders_per_day", 20)),
        order_type=con.get("order_type", "marketable_limit"),
        max_gross_exposure=float(con.get("max_gross_exposure", 1.0)),
        stress_vol_limit=float(con.get("stress_vol_limit", 0.30)),
        drawdown_tiers=drawdown_tiers,
        trailing_drawdown_pct=float(ks.get("trailing_drawdown_pct", 0.15)),
        drift_band_pct=float(rb.get("drift_band_pct", 0.05)),
        cadence=rb.get("cadence", "quarterly"),
        regime_triggered=bool(rb.get("regime_triggered", True)),
        defensive_targets=defensive_targets,
        allow_fractional=bool(ex.get("allow_fractional", True)),
        operational_policy=str(allocation.get("operational_policy", "hrp")),
        costs=costs,
    )
    if defensive_targets:
        try:
            mandate.check_targets(defensive_targets)
        except MandateViolation as exc:
            raise MandateViolation(f"invalid defensive_targets: {exc}") from exc
    return mandate
