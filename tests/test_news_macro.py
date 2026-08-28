from datetime import datetime, timezone

from qlab.news.providers import macro

CONFIG = {
    "macro": {"feeds": [{"name": "BLS", "url": "https://www.bls.gov/feed/bls_latest.rss",
                         "tickers": ["TIP", "BNDW"]}]},
    "calendar": [
        {"name": "FOMC statement", "when": "2026-09-17T18:00:00+00:00",
         "tickers": ["BNDW", "TLT"], "source": "Federal Reserve"},
        {"name": "CPI", "when": "2026-08-12T12:30:00+00:00",
         "tickers": ["TIP"], "source": "BLS"},
    ],
}


def test_upcoming_lists_only_what_is_ahead_inside_the_horizon(monkeypatch):
    monkeypatch.setattr(macro, "load_news_sources", lambda: CONFIG)
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    ahead = macro.upcoming(now, horizon_days=14)
    assert [e["name"] for e in ahead] == ["FOMC statement"]
    assert ahead[0]["days_ahead"] == 7
    assert macro.upcoming(now, horizon_days=3) == []


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
