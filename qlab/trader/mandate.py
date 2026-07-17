"""The trading mandate — hard limits enforced in deterministic code.

Every limit in ``mandate.yaml`` is checked here before a plan may advance from
``proposed`` to ``checked``. This is the difference between governance that is
*architectural* and governance that is *advisory* (research-plan §3, §8.1): the
agent authors targets, but the mandate — not the model — decides what is allowed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_MANDATE = _REPO_ROOT / "mandate.yaml"


class MandateViolation(Exception):
    """Raised when a proposed action breaches the mandate. Fatal by design."""


@dataclass
class Mandate:
    paper_capital: float = 10000.0
    base_currency: str = "USD"
    universe_whitelist: list[str] = field(default_factory=list)
    long_only: bool = True
    fully_invested: bool = True
    max_weight_per_asset: float = 0.40
    min_weight_per_asset: float = 0.0
    max_turnover_per_rebalance: float = 0.50
    max_orders_per_day: int = 20
    order_type: str = "marketable_limit"
    trailing_drawdown_pct: float = 0.15
    drift_band_pct: float = 0.05
    cadence: str = "quarterly"
    regime_triggered: bool = True
    allow_fractional: bool = True

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
        if self.fully_invested and abs(total - 1.0) > 1e-2:
            raise MandateViolation(f"fully-invested mandate: weights sum to {total:.4f}")
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


def load_mandate(path: str | Path | None = None) -> Mandate:
    """Load and flatten ``mandate.yaml`` into a :class:`Mandate`."""
    p = Path(path) if path else _DEFAULT_MANDATE
    with open(p, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    acct = raw.get("account", {})
    con = raw.get("constraints", {})
    ks = raw.get("kill_switch", {})
    rb = raw.get("rebalance", {})
    ex = raw.get("execution", {})
    return Mandate(
        paper_capital=float(acct.get("paper_capital", 10000.0)),
        base_currency=acct.get("base_currency", "USD"),
        universe_whitelist=list(raw.get("universe_whitelist", [])),
        long_only=bool(con.get("long_only", True)),
        fully_invested=bool(con.get("fully_invested", True)),
        max_weight_per_asset=float(con.get("max_weight_per_asset", 0.40)),
        min_weight_per_asset=float(con.get("min_weight_per_asset", 0.0)),
        max_turnover_per_rebalance=float(con.get("max_turnover_per_rebalance", 0.50)),
        max_orders_per_day=int(con.get("max_orders_per_day", 20)),
        order_type=con.get("order_type", "marketable_limit"),
        trailing_drawdown_pct=float(ks.get("trailing_drawdown_pct", 0.15)),
        drift_band_pct=float(rb.get("drift_band_pct", 0.05)),
        cadence=rb.get("cadence", "quarterly"),
        regime_triggered=bool(rb.get("regime_triggered", True)),
        allow_fractional=bool(ex.get("allow_fractional", True)),
    )
