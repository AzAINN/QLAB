"""Bob's heartbeat and desk read: the live worker and its qualitative view."""

from __future__ import annotations

import threading

import pytest

from qlab.news.feed import NewsItem
from qlab.operator.heartbeat import BobHeartbeat
from qlab.operator.synthesis import (
    ALIGNED,
    DIVERGENT,
    QUIET,
    compose_read,
    read_news,
    should_open_debate,
)


def _item(headline, summary="", tickers=("ACWI",)):
    return NewsItem(source="test", published="2026-07-24T12:00:00+00:00",
                    headline=headline, summary=summary, url="http://x",
                    tickers=tuple(tickers), provider="synthetic")


def _panel(state="calm", agree=5, disagree=0, failed=0, reason=None):
    return {"robust_state": state, "agreement_count": agree,
            "disagreement_count": disagree, "failed_count": failed,
            "uncertainty_reason": reason, "snapshot_id": "snap-1"}


# --- heartbeat ---------------------------------------------------------------


def test_heartbeat_ticks_and_reports_status():
    seen = []
    hb = BobHeartbeat(lambda: seen.append(1) or {"state": "observing"},
                      interval_s=5.0)
    assert hb.tick_once() == {"state": "observing"}
    status = hb.status()
    assert status["ticks"] == 1 and status["errors"] == 0
    assert status["last_state"] == "observing"


def test_a_failing_tick_never_kills_the_loop():
    errors = []

    def boom():
        raise RuntimeError("registry busy")

    hb = BobHeartbeat(boom, on_error=errors.append)
    assert hb.tick_once() is None
    assert hb.status()["errors"] == 1
    assert isinstance(errors[0], RuntimeError)
    # And a later good tick still works.
    hb._tick = lambda: {"state": "observing"}
    assert hb.tick_once()["state"] == "observing"


def test_an_error_handler_that_raises_cannot_take_the_loop_down():
    def boom():
        raise RuntimeError("x")

    def bad_handler(_exc):
        raise ValueError("handler exploded")

    hb = BobHeartbeat(boom, on_error=bad_handler)
    assert hb.tick_once() is None  # did not propagate


def test_heartbeat_start_stop_is_idempotent_and_bounded():
    ticked = threading.Event()
    hb = BobHeartbeat(lambda: ticked.set() or {"state": "observing"},
                      interval_s=5.0)
    hb.start()
    try:
        assert ticked.wait(2.0), "heartbeat did not tick"
        assert hb.running
        with pytest.raises(RuntimeError, match="already running"):
            hb.start()
    finally:
        hb.stop()
    assert not hb.running


def test_interval_has_a_floor_so_a_quiet_desk_cannot_spin():
    assert BobHeartbeat(lambda: {}, interval_s=0.0).interval_s >= 5.0


# --- news reading ------------------------------------------------------------


def test_tone_counts_distinct_items_not_word_occurrences():
    # One breathless headline must not outweigh several calm ones.
    items = [_item("Crash crash crash selloff rout plunge")] + [
        _item("Quarterly filing published") for _ in range(4)]
    read = read_news(items)
    assert read.risk_off_hits == 1
    assert read.item_count == 5
    assert read.intensity == pytest.approx(0.2)
    assert read.tone == "mixed" or read.tone == "risk_off"


def test_tone_matching_is_word_boundary_not_substring():
    """Substring matching scored 'routine' as 'rout' and 'warning' as 'war',
    quietly turning filings into a selloff."""
    for benign in ("Routine filing published", "Warning label updated",
                   "Forward guidance reiterated"):
        assert read_news([_item(benign)]).tone == "quiet", benign
    # Real words still match, including multi-word phrases with odd spacing.
    assert read_news([_item("Selloff deepens")]).tone == "risk_off"
    assert read_news([_item("Record  high for the index")]).tone == "risk_on"


def test_quiet_when_nothing_carries_tone():
    read = read_news([_item("Index rebalance schedule published")])
    assert read.tone == "quiet" and read.intensity == 0.0


def test_no_news_is_quiet_not_calm():
    read = read_news([])
    assert read.tone == "quiet" and read.item_count == 0


def test_top_tickers_are_ranked():
    items = [_item("Rally", tickers=("ACWI",)),
             _item("Surge", tickers=("ACWI", "GLD")),
             _item("Rebound", tickers=("GLD",))]
    read = read_news(items)
    assert set(read.top_tickers[:2]) == {"ACWI", "GLD"}


# --- the composed read -------------------------------------------------------


def test_calm_tape_with_risk_off_news_is_divergent_even_when_thin():
    """The case Bob exists for: the tape and the story disagree.

    Thin coverage scales conviction down; it must never silence the tension.
    """
    read = compose_read(as_of="2026-07-24", panel=_panel("calm"),
                        news=read_news([_item("Selloff deepens on recession fear"),
                                        _item("Quiet session")]))
    assert read.agreement == DIVERGENT
    assert any("has not repriced" in t for t in read.tensions)
    assert read.conviction <= 0.55
    assert read.would_change_my_mind


def test_stress_with_no_coverage_is_divergent():
    read = compose_read(as_of="2026-07-24", panel=_panel("stress"),
                        news=read_news([_item("Routine filing")]))
    assert read.agreement == DIVERGENT
    assert any("no one is writing about" in t for t in read.tensions)


def test_uncertain_panel_is_reported_as_uncertain_not_resolved():
    read = compose_read(
        as_of="2026-07-24",
        panel=_panel("uncertain", agree=2, disagree=2,
                     reason="indicators disagree (2 stress vs 2 calm)"),
        news=read_news([]))
    assert read.quantitative_state == "uncertain"
    assert any("does not agree with itself" in t for t in read.tensions)


def test_agreeing_evidence_is_aligned_with_higher_conviction():
    aligned = compose_read(
        as_of="2026-07-24", panel=_panel("stress"),
        news=read_news([_item("Selloff deepens"), _item("Rout continues")]))
    assert aligned.agreement == ALIGNED
    divergent = compose_read(
        as_of="2026-07-24", panel=_panel("calm"),
        news=read_news([_item("Selloff deepens"), _item("Rout continues")]))
    # Alignment always outranks divergence in conviction.
    assert aligned.conviction > divergent.conviction


def test_a_quiet_desk_reads_quiet_with_low_conviction():
    read = compose_read(as_of="2026-07-24", panel=_panel("calm"),
                        news=read_news([]))
    assert read.agreement == QUIET
    assert read.conviction <= 0.3


def test_failed_indicators_weaken_the_read():
    strong = compose_read(as_of="2026-07-24", panel=_panel("calm"),
                          news=read_news([_item("Rally broadens")]))
    thin = compose_read(as_of="2026-07-24",
                        panel=_panel("calm", agree=3, failed=2),
                        news=read_news([_item("Rally broadens")]))
    assert thin.conviction < strong.conviction
    assert any("failed to compute" in o for o in thin.observations)


def test_read_is_advisory_and_carries_no_instruction():
    read = compose_read(as_of="2026-07-24", panel=_panel(),
                        news=read_news([])).to_dict()
    assert read["advisory"] is True
    assert "targets" not in read and "recommendation" not in read
    assert read["read_hash"]


def test_referee_fails_are_surfaced_as_research_context():
    read = compose_read(
        as_of="2026-07-24", panel=_panel(), news=read_news([]),
        recent_verdicts=[{"verdict": "FAIL", "verdict_id": "v1"}])
    assert any("referee FAIL" in o for o in read.observations)


# --- escalation --------------------------------------------------------------


def test_debate_opens_only_on_material_disagreement():
    quiet = compose_read(as_of="2026-07-24", panel=_panel("calm"),
                         news=read_news([]))
    assert should_open_debate(quiet) == (False, None)

    loud = compose_read(
        as_of="2026-07-24", panel=_panel("calm"),
        news=read_news([_item("Crash deepens"), _item("Selloff widens")]))
    should, claim = should_open_debate(loud)
    assert should is True
    # It must be an allowlisted debate claim, not an invented subject.
    from qlab.governance.debate import ALLOWED_CLAIMS

    assert claim in ALLOWED_CLAIMS


def test_uncertain_panel_always_warrants_a_debate():
    read = compose_read(
        as_of="2026-07-24",
        panel=_panel("uncertain", agree=2, disagree=2, reason="split"),
        news=read_news([]))
    should, claim = should_open_debate(read)
    assert should is True and claim == "regime_read"


def test_a_broken_news_feed_is_visible_not_silently_quiet():
    """An empty news window and a broken feed mean opposite things.

    Silently reporting 'quiet' when the fetch failed would let a dead feed read
    as a calm market.
    """
    from qlab.state.registry import Registry
    from qlab.ui.server import UISession

    session = UISession(offline_default=True, registry=Registry(":memory:"))

    def boom(*_a, **_kw):
        raise RuntimeError("source 'CNBC' at 'http://x' is unavailable (404)")

    import qlab.news.feed as feed_module

    original = feed_module.fetch_news
    feed_module.fetch_news = boom
    try:
        read = session.refresh_desk_read(True)
    finally:
        feed_module.fetch_news = original

    assert read["news_error"]
    assert "404" in read["news_error"]
    assert any("UNAVAILABLE" in o for o in read["observations"])
    assert any("not quiet" in o for o in read["observations"])


def test_read_reports_which_news_source_it_used():
    from qlab.state.registry import Registry
    from qlab.ui.server import UISession

    session = UISession(offline_default=True, registry=Registry(":memory:"))
    read = session.refresh_desk_read(True)
    # Offline always means synthetic, and it must say so rather than implying
    # the headlines are real.
    assert read["news_source"] == "synthetic (demo)"
    assert "news_error" not in read
