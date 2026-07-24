"""Session-aware freshness and eligibility for a market-data panel.

The single place that answers "may this snapshot move paper money?". It is
deterministic given ``(panel, policy, now)`` so a data permit built from it is
reproducible and auditable. Quote-level freshness (seconds) arrives with the
market stream in a later phase; this covers daily-bar freshness and provenance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
import pandas as pd

from qlab.autopilot.scheduler import last_completed_session, sessions_between
from qlab.core.data import DataPolicy

# Providers whose data may back a paper proposal / execution. Synthetic and
# yfinance are research-grade only; only Alpaca is execution-eligible today.
_EXECUTION_GRADE_PROVIDERS = frozenset({"alpaca"})


@dataclass(frozen=True)
class DataHealth:
    """The integrity verdict on one price panel under one policy."""

    provider: str
    synthetic: bool
    last_bar: date | None
    reference_session: date | None
    bar_age_sessions: int | None
    fresh: bool
    provider_matches_policy: bool
    integrity_verdict: str            # "PASS" | "FAIL"
    missing_tickers: list[str]
    eligible_for_research: bool
    eligible_for_paper_proposal: bool
    eligible_for_execution: bool
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "synthetic": self.synthetic,
            "last_bar": self.last_bar.isoformat() if self.last_bar else None,
            "reference_session": (
                self.reference_session.isoformat() if self.reference_session else None
            ),
            "bar_age_sessions": self.bar_age_sessions,
            "fresh": self.fresh,
            "provider_matches_policy": self.provider_matches_policy,
            "integrity_verdict": self.integrity_verdict,
            "missing_tickers": list(self.missing_tickers),
            "eligible_for_research": self.eligible_for_research,
            "eligible_for_paper_proposal": self.eligible_for_paper_proposal,
            "eligible_for_execution": self.eligible_for_execution,
            "reasons": list(self.reasons),
        }


def _panel_last_bar(prices: pd.DataFrame) -> date | None:
    if prices is None or prices.empty:
        return None
    last = prices.index[-1]
    return last.date() if hasattr(last, "date") else pd.Timestamp(last).date()


def _integrity(
    prices: pd.DataFrame, tickers: list[str],
) -> tuple[str, list[str], list[str]]:
    """A light deterministic integrity check; the referee/data-qa go deeper.

    Returns ``(verdict, reasons, missing_tickers)``.
    """
    reasons: list[str] = []
    if prices is None or prices.empty:
        return "FAIL", ["panel is empty"], list(tickers)
    missing = [t for t in tickers if t not in prices.columns]
    if missing:
        reasons.append(f"missing tickers: {missing}")
    present = [t for t in tickers if t in prices.columns]
    values = prices.reindex(columns=present).to_numpy(dtype=float)
    if values.size and not np.all(np.isfinite(values)):
        reasons.append("panel contains non-finite prices")
    if values.size and (values <= 0).any():
        reasons.append("panel contains non-positive prices")
    if len(prices.index) < 2:
        reasons.append("panel has fewer than two bars")
    return ("FAIL" if reasons else "PASS"), reasons, missing


def evaluate_panel_health(
    prices: pd.DataFrame,
    policy: DataPolicy,
    *,
    tickers: list[str] | None = None,
    now: datetime | None = None,
    quotes_fresh: bool | None = None,
) -> DataHealth:
    """Evaluate freshness, provenance match, and eligibility for ``prices``.

    ``prices`` must carry ``attrs['source']``/``attrs['synthetic']`` (every
    panel from :func:`qlab.core.data.get_prices` does). ``now`` is a
    timezone-aware instant used for session-aware freshness; tests pass it
    explicitly for determinism. ``quotes_fresh`` is the live market stream's
    verdict (P2): when it is ``False`` execution eligibility is withdrawn even
    though the daily bar is current — a stale quote must block execution.
    """
    tickers = list(tickers or (list(prices.columns) if prices is not None else []))
    source = str((prices.attrs.get("source") if prices is not None else "") or "unknown")
    synthetic = bool(prices.attrs.get("synthetic", False)) if prices is not None else True

    last_bar = _panel_last_bar(prices)
    reference = last_completed_session(now) if last_bar is not None else None
    if last_bar is not None and reference is not None:
        bar_age = 0 if last_bar >= reference else sessions_between(last_bar, reference)
    else:
        bar_age = None
    fresh = bar_age == 0

    provider_matches = (source == policy.provider) or (
        synthetic and policy.provider == "synthetic")

    verdict, reasons, missing_tickers = _integrity(prices, tickers)
    if not provider_matches:
        reasons.append(
            f"panel source {source!r} does not match policy provider "
            f"{policy.provider!r}")
    if policy.require_fresh and not fresh:
        reasons.append(
            f"daily bar is stale: last bar {last_bar} is {bar_age} session(s) "
            f"behind the last completed session {reference}")

    execution_grade = source in _EXECUTION_GRADE_PROVIDERS and not synthetic
    integrity_ok = verdict == "PASS"

    eligible_research = integrity_ok
    eligible_paper = (
        integrity_ok
        and provider_matches
        and execution_grade
        and (fresh or not policy.require_fresh)
    )
    # Quote-level freshness is an additional AND on top of the daily-bar gate;
    # a policy that is not itself execution-eligible can never yield
    # execution-grade data. When a live stream reports stale quotes, execution
    # is withdrawn and the reason recorded.
    eligible_exec = eligible_paper and policy.execution_eligible and fresh
    if quotes_fresh is False and eligible_exec:
        eligible_exec = False
        reasons.append("live quote stream is stale; execution withdrawn")

    return DataHealth(
        provider=source,
        synthetic=synthetic,
        last_bar=last_bar,
        reference_session=reference,
        bar_age_sessions=bar_age,
        fresh=fresh,
        provider_matches_policy=provider_matches,
        integrity_verdict=verdict,
        missing_tickers=missing_tickers,
        eligible_for_research=eligible_research,
        eligible_for_paper_proposal=eligible_paper,
        eligible_for_execution=eligible_exec,
        reasons=reasons,
    )
