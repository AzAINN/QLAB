"""Session-aware freshness, data-health eligibility, and data permits (P1)."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd
import pytest

from qlab.autopilot import scheduler
from qlab.core.data import DataPolicy
from qlab.data.health import evaluate_panel_health
from qlab.data.permit import build_permit
from qlab.state.registry import Registry

# 2026-07-24 is a Friday NYSE session; 21:00 UTC == 17:00 ET (after 16:20 close+grace).
NOW = datetime(2026, 7, 24, 21, 0, tzinfo=timezone.utc)
TICKERS = ["ACWI", "BNDW"]


def _panel(last_dates: list[str], source: str, synthetic: bool,
           tickers: list[str] = TICKERS) -> pd.DataFrame:
    idx = pd.to_datetime(last_dates)
    df = pd.DataFrame(
        {t: [100.0 + i for i in range(len(idx))] for t in tickers}, index=idx)
    df.attrs["source"] = source
    df.attrs["synthetic"] = synthetic
    return df


# --- scheduler session helpers ----------------------------------------------


def test_last_completed_session_is_today_after_close():
    assert scheduler.last_completed_session(NOW) == date(2026, 7, 24)


def test_last_completed_session_is_prior_session_before_close():
    pre_open = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)  # 08:00 ET
    assert scheduler.last_completed_session(pre_open) == date(2026, 7, 23)


def test_weekend_does_not_make_friday_bar_stale():
    sunday = datetime(2026, 7, 26, 18, 0, tzinfo=timezone.utc)
    assert scheduler.last_completed_session(sunday) == date(2026, 7, 24)


def test_sessions_between_counts_trading_days():
    assert scheduler.sessions_between(date(2026, 7, 21), date(2026, 7, 24)) == 3
    assert scheduler.sessions_between(date(2026, 7, 24), date(2026, 7, 24)) == 0


# --- data-health eligibility -------------------------------------------------


def test_fresh_alpaca_panel_is_paper_eligible():
    panel = _panel(["2026-07-22", "2026-07-23", "2026-07-24"], "alpaca", False)
    h = evaluate_panel_health(panel, DataPolicy.alpaca_operational(),
                              tickers=TICKERS, now=NOW)
    assert h.fresh and h.integrity_verdict == "PASS"
    assert h.eligible_for_paper_proposal
    assert h.eligible_for_research


def test_stale_alpaca_panel_blocks_paper_proposal():
    panel = _panel(["2026-07-20", "2026-07-21"], "alpaca", False)
    h = evaluate_panel_health(panel, DataPolicy.alpaca_operational(),
                              tickers=TICKERS, now=NOW)
    assert not h.fresh
    assert not h.eligible_for_paper_proposal
    assert any("stale" in r for r in h.reasons)


def test_synthetic_panel_is_research_only():
    panel = _panel(["2026-07-23", "2026-07-24"], "synthetic", True)
    h = evaluate_panel_health(panel, DataPolicy.demo(), tickers=TICKERS, now=NOW)
    assert h.eligible_for_research
    assert not h.eligible_for_paper_proposal
    assert not h.eligible_for_execution


def test_yfinance_cache_cannot_satisfy_alpaca_policy():
    panel = _panel(["2026-07-23", "2026-07-24"], "yfinance", False)
    h = evaluate_panel_health(panel, DataPolicy.alpaca_operational(),
                              tickers=TICKERS, now=NOW)
    assert not h.provider_matches_policy
    assert not h.eligible_for_paper_proposal
    assert any("does not match policy provider" in r for r in h.reasons)


def test_missing_ticker_fails_integrity():
    panel = _panel(["2026-07-23", "2026-07-24"], "alpaca", False, tickers=["ACWI"])
    h = evaluate_panel_health(panel, DataPolicy.alpaca_operational(),
                              tickers=TICKERS, now=NOW)
    assert h.integrity_verdict == "FAIL"
    assert h.missing_tickers == ["BNDW"]
    assert not h.eligible_for_paper_proposal


# --- data permits ------------------------------------------------------------


def test_permit_id_is_deterministic_content_address():
    panel = _panel(["2026-07-23", "2026-07-24"], "alpaca", False)
    policy = DataPolicy.alpaca_operational()
    h = evaluate_panel_health(panel, policy, tickers=TICKERS, now=NOW)
    p1 = build_permit(snapshot_id="snap-1", purpose="paper_proposal", policy=policy,
                      health=h, universe=TICKERS, as_of="2026-07-24",
                      retrieved_at="2026-07-24T21:00:00Z")
    p2 = build_permit(snapshot_id="snap-1", purpose="paper_proposal", policy=policy,
                      health=h, universe=TICKERS, as_of="2026-07-24",
                      retrieved_at="2026-07-24T21:05:59Z")  # different wall clock
    assert p1.permit_id == p2.permit_id  # retrieved_at excluded from the hash
    assert p1.permit_id.startswith("sha256:")
    assert p1.eligible_for_paper_proposal


def test_permit_roundtrips_through_registry():
    panel = _panel(["2026-07-23", "2026-07-24"], "alpaca", False)
    policy = DataPolicy.alpaca_operational()
    h = evaluate_panel_health(panel, policy, tickers=TICKERS, now=NOW)
    permit = build_permit(snapshot_id="snap-1", purpose="paper_proposal",
                          policy=policy, health=h, universe=TICKERS,
                          as_of="2026-07-24", retrieved_at="2026-07-24T21:00:00Z")
    reg = Registry(":memory:")
    try:
        pid = reg.record_data_permit(permit.to_dict())
        assert pid == permit.permit_id
        current = reg.current_data_permit("paper_proposal")
        assert current is not None
        assert current["permit_id"] == permit.permit_id
        assert current["permit"]["eligible_for_paper_proposal"] is True
        # Idempotent re-record of the same content-addressed permit.
        reg.record_data_permit(permit.to_dict())
        assert reg.get_data_permit(pid)["provider"] == "alpaca"
    finally:
        reg.close()
