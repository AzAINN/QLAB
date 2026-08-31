from datetime import datetime, timezone

import pytest

from qlab.news import feed as news
from qlab.news.providers import macro

CONFIG = {
    "macro": {"feeds": [{"name": "BLS", "url": "https://www.bls.gov/feed/bls_latest.rss",
                         "tickers": ["TIP", "BNDW"]}]},
    "calendar": [
        {"name": "FOMC statement", "when": "2026-09-17T18:00:00+00:00",
         "tickers": ["BNDW", "TLT"], "source": "Federal Reserve"},
        {"name": "CPI", "when": "2026-08-12T12:30:00+00:00",
         "tickers": ["TIP"], "source": "BLS"},
        # Exactly at the as_of the horizon test uses: the window is
        # (as_of, as_of + horizon], so this one is never "upcoming".
        {"name": "Already out", "when": "2026-09-10T00:00:00+00:00",
         "tickers": ["TIP"], "source": "BLS"},
    ],
}


def _calendar(entries):
    return {"calendar": entries}


def test_upcoming_lists_only_what_is_ahead_inside_the_horizon(monkeypatch):
    monkeypatch.setattr(macro, "load_news_sources", lambda: CONFIG)
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    ahead = macro.upcoming(now, horizon_days=14)
    assert [e["name"] for e in ahead] == ["FOMC statement"]
    assert ahead[0]["days_ahead"] == 7
    assert macro.upcoming(now, horizon_days=3) == []
    # Strict lower bound: an entry dated exactly at as_of has happened.
    assert "Already out" not in [e["name"] for e in ahead]


def test_macro_records_are_primary_tier(monkeypatch):
    from qlab.news.grounding import source_tier
    monkeypatch.setattr(macro, "load_news_sources", lambda: CONFIG)
    seen = []

    def fake_feeds(feeds, as_of, universe):
        seen.append([f["name"] for f in feeds])
        from qlab.news.feed import NewsItem
        return [NewsItem(source="BLS", published="2026-09-09T12:30:00+00:00",
                         headline="CPI rose 0.2% in August", summary="", url="https://bls.gov/x",
                         tickers=("TIP",), provider="macro")]

    monkeypatch.setattr(macro, "_fetch_rss_feeds", fake_feeds)
    items = macro.fetch(datetime(2026, 9, 10, tzinfo=timezone.utc), ("TIP",))
    assert seen == [["BLS"]]
    assert source_tier(items[0].source) == "primary"


def test_every_configured_macro_feed_is_a_primary_publisher():
    """The real yaml, not a fixture: a feed added later cannot miss the tier."""
    from qlab.news.grounding import source_tier

    feeds = news.load_news_sources()["macro"]["feeds"]
    assert feeds, "the shipped config must configure macro feeds"
    assert [f["name"] for f in feeds if source_tier(f["name"]) != "primary"] == []


def test_upcoming_orders_by_the_instant_not_the_string(monkeypatch):
    """A -04:00 entry sorts last as text and first as time. Time wins."""
    monkeypatch.setattr(macro, "load_news_sources", lambda: _calendar([
        {"name": "FOMC", "when": "2026-09-15T09:00:00+00:00",
         "tickers": ["TLT"], "source": "Federal Reserve"},
        {"name": "CPI", "when": "2026-09-15T08:00:00-04:00",   # 12:00Z, later
         "tickers": ["TIP"], "source": "BLS"},
        {"name": "Claims", "when": "2026-09-15T00:30:00-04:00",  # 04:30Z, first
         "tickers": ["BNDW"], "source": "BLS"},
    ]))
    ahead = macro.upcoming(datetime(2026, 9, 14, tzinfo=timezone.utc))
    assert [e["name"] for e in ahead] == ["Claims", "FOMC", "CPI"]
    assert ahead[0]["when"] == "2026-09-15T04:30:00+00:00"


def test_an_exhausted_calendar_refuses_rather_than_reading_as_quiet(monkeypatch):
    monkeypatch.setattr(macro, "load_news_sources", lambda: _calendar([
        {"name": "CPI", "when": "2026-08-12T12:30:00+00:00",
         "tickers": ["TIP"], "source": "BLS"},
        # Deliberately out of order: the latest entry is not the last one.
        {"name": "FOMC", "when": "2026-08-20T18:00:00+00:00",
         "tickers": ["TLT"], "source": "Federal Reserve"},
        {"name": "Claims", "when": "2026-08-14T12:30:00+00:00",
         "tickers": ["BNDW"], "source": "BLS"},
    ]))
    with pytest.raises(RuntimeError,
                       match=r"2026-08-20.*news_sources\.yaml"):
        macro.upcoming(datetime(2026, 9, 10, tzinfo=timezone.utc))


def test_an_empty_calendar_is_exhausted_not_quiet(monkeypatch):
    monkeypatch.setattr(macro, "load_news_sources", lambda: _calendar([]))
    with pytest.raises(RuntimeError, match=r"news_sources\.yaml"):
        macro.upcoming(datetime(2026, 9, 10, tzinfo=timezone.utc))


def test_a_naive_calendar_entry_is_refused(monkeypatch):
    monkeypatch.setattr(macro, "load_news_sources", lambda: _calendar([
        {"name": "FOMC", "when": "2026-09-17T18:00:00",
         "tickers": ["TLT"], "source": "Federal Reserve"}]))
    with pytest.raises(ValueError, match="needs a timezone"):
        macro.upcoming(datetime(2026, 9, 10, tzinfo=timezone.utc))


def test_a_naive_as_of_is_refused_by_name(monkeypatch):
    monkeypatch.setattr(macro, "load_news_sources", lambda: CONFIG)
    with pytest.raises(ValueError, match="as_of"):
        macro.upcoming(datetime(2026, 9, 10))


def _base_config(**extra):
    config = {
        "feeds": [{"name": "Top", "url": "https://example.test/f.xml",
                   "tickers": ["ACWI"]}],
        "synthetic": {"ACWI": [{"source": "s", "headline": "h", "summary": "y"}]},
    }
    config.update(extra)
    return config


def test_config_holds_macro_feeds_to_the_feed_contract():
    with pytest.raises(ValueError, match=r"macro news feed 0 field 'url'"):
        news._validate_config(_base_config(macro={"feeds": [{"name": "BLS"}]}))
    with pytest.raises(ValueError, match=r"macro news feed 'BLS' must define "
                                         r"tickers or keywords"):
        news._validate_config(_base_config(
            macro={"feeds": [{"name": "BLS", "url": "https://x.test/f"}]}))


def test_config_requires_every_calendar_field():
    with pytest.raises(ValueError, match=r"news calendar entry 0 field 'source'"):
        news._validate_config(_base_config(calendar=[
            {"name": "FOMC", "when": "2026-09-17T18:00:00+00:00",
             "tickers": ["TLT"]}]))
    with pytest.raises(ValueError, match=r"news calendar entry 'FOMC' tickers"):
        news._validate_config(_base_config(calendar=[
            {"name": "FOMC", "when": "2026-09-17T18:00:00+00:00",
             "tickers": [], "source": "Federal Reserve"}]))
    with pytest.raises(ValueError, match=r"news calendar entry 0 must be a mapping"):
        news._validate_config(_base_config(calendar=["FOMC"]))


def test_calendar_days_left_counts_down_to_the_last_entry(monkeypatch):
    """The look-ahead expires silently: `upcoming` refuses only once the last
    entry is already past, which is a day too late to be told. The remaining
    life of the file is a number the desk can show before that."""
    monkeypatch.setattr(macro, "load_news_sources", lambda: CONFIG)
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    assert macro.calendar_days_left(now) == 7          # to the FOMC entry
    # Exhausted and empty are both "no life left", and both are None rather
    # than a negative number pretending to be a countdown.
    assert macro.calendar_days_left(
        datetime(2026, 9, 30, tzinfo=timezone.utc)) is None
    monkeypatch.setattr(macro, "load_news_sources", lambda: _calendar([]))
    assert macro.calendar_days_left(now) is None


def test_calendar_days_left_needs_an_aware_as_of(monkeypatch):
    monkeypatch.setattr(macro, "load_news_sources", lambda: CONFIG)
    with pytest.raises(ValueError, match="timezone-aware"):
        macro.calendar_days_left(datetime(2026, 9, 10))
