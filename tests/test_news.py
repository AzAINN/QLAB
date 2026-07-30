"""Offline, deterministic tests for the isolated news provider library."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from urllib.error import URLError

import pytest

from qlab.news import feed as news
from qlab.news.feed import NewsItem

CORE = ("ACWI", "BNDW", "GSG", "IGF", "GLD", "VNQ", "EMB")


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def test_news_item_provenance_shape() -> None:
    item = NewsItem(
        source="Example Wire",
        published="2025-01-15T10:00:00+00:00",
        headline="Example headline",
        summary="Example summary",
        url="https://example.test/story",
        tickers=("ACWI",),
        provider="rss",
    )

    assert item.provenance() == {
        "source": "Example Wire",
        "published": "2025-01-15T10:00:00+00:00",
        "url": "https://example.test/story",
        "provider": "rss",
    }


def test_synthetic_fetch_is_deterministic_filtered_and_provider_tagged(
    monkeypatch,
) -> None:
    as_of = datetime(2025, 1, 15, 12, tzinfo=timezone.utc)
    monkeypatch.setenv("QLAB_NEWS_PROVIDER", "rss")

    first = news.fetch_news(as_of, CORE, lookback_hours=48, offline=True)
    second = news.fetch_news(as_of, reversed(CORE), lookback_hours=48, offline=True)
    narrow = news.fetch_news(as_of, CORE, lookback_hours=12, offline=True)

    assert first
    assert first == second
    assert narrow
    assert set(narrow) < set(first)
    assert all(item.provider == "synthetic" for item in first)
    assert all(set(item.tickers) <= set(CORE) for item in first)
    cutoff = as_of - timedelta(hours=48)
    assert all(cutoff <= _timestamp(item.published) <= as_of for item in first)
    assert news.cached_news_provenance(CORE) == ("synthetic", len(narrow))
    expected_order = sorted(
        first,
        key=lambda item: (
            -_timestamp(item.published).timestamp(),
            item.source.casefold(),
            item.headline.casefold(),
            item.url,
        ),
    )
    assert first == expected_order


def test_rss_provider_fails_loudly_when_network_is_unavailable(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        news,
        "load_news_sources",
        lambda: {
            "feeds": [
                {
                    "name": "Offline Test Feed",
                    "url": "https://example.test/feed.xml",
                    "tickers": ["ACWI"],
                }
            ],
            "synthetic": {},
        },
    )

    def unavailable(*args, **kwargs):
        raise URLError("offline")

    monkeypatch.setattr(news.urllib.request, "urlopen", unavailable)

    with pytest.raises(
        RuntimeError,
        match=r"rss news provider requires reachable network feeds.*"
        r"Offline Test Feed.*unavailable",
    ):
        news.fetch_news(
            "2025-01-15T12:00:00+00:00",
            ["ACWI"],
            provider="rss",
        )


def test_rss_stdlib_parser_handles_rss2_atom_and_keyword_mapping(
    monkeypatch,
) -> None:
    rss = b"""\
    <rss version="2.0"><channel><item>
      <title>Global equities face a busier volatility backdrop</title>
      <description>World stocks show wider dispersion.</description>
      <link>https://example.test/equities</link>
      <pubDate>Wed, 15 Jan 2025 10:00:00 GMT</pubDate>
    </item></channel></rss>
    """
    atom = b"""\
    <feed xmlns="http://www.w3.org/2005/Atom"><entry>
      <title>Real estate refinancing risks remain uneven</title>
      <summary>Property market financing conditions diverge.</summary>
      <link href="/property"/>
      <updated>2025-01-15T09:00:00Z</updated>
    </entry></feed>
    """
    config = {
        "feeds": [
            {
                "name": "RSS Test",
                "url": "https://example.test/rss.xml",
                "tickers": ["ACWI"],
            },
            {
                "name": "Atom Test",
                "url": "https://example.test/atom.xml",
                "keywords": [
                    {
                        "terms": ["real estate", "property market"],
                        "tickers": ["VNQ"],
                    }
                ],
            },
        ],
        "synthetic": {},
    }
    payloads = {
        "https://example.test/rss.xml": rss,
        "https://example.test/atom.xml": atom,
    }

    class Response:
        def __init__(self, payload: bytes):
            self.payload = payload

        def read(self) -> bytes:
            return self.payload

        def close(self) -> None:
            pass

    def urlopen(request, timeout):
        assert timeout == news._FETCH_TIMEOUT_S
        return Response(payloads[request.full_url])

    monkeypatch.setattr(news, "load_news_sources", lambda: config)
    monkeypatch.setattr(news.urllib.request, "urlopen", urlopen)
    monkeypatch.setitem(sys.modules, "feedparser", None)

    items = news.fetch_news(
        "2025-01-15T12:00:00+00:00",
        ["ACWI", "VNQ"],
        provider="rss",
    )

    assert [(item.source, item.tickers) for item in items] == [
        ("RSS Test", ("ACWI",)),
        ("Atom Test", ("VNQ",)),
    ]
    assert items[1].url == "https://example.test/property"
    assert all(item.provider == "rss" for item in items)


def test_news_source_config_covers_the_cross_asset_core() -> None:
    config = news.load_news_sources()

    assert config["feeds"]
    mapped: set[str] = set()
    for feed in config["feeds"]:
        assert feed["name"] and feed["url"]
        mapped.update(feed.get("tickers", ()))
        for rule in feed.get("keywords", ()):
            if isinstance(rule, dict):
                mapped.update(rule["tickers"])

    assert set(CORE) <= mapped
    assert set(CORE) <= set(config["synthetic"])


def test_the_synthetic_window_does_not_thin_as_the_universe_grows():
    """The offline news window must scale with the universe, not collapse.

    The old schedule had 7 offsets and added `block * 72` hours per wrap, so the
    eighth ticker onward was dated up to nine days back. Measured on the 20-name
    universe that gave a mean of 2.1 items in a 48h window and ZERO on some
    dates — which would leave the qualitative signals with no input at all, on a
    lane whose entire purpose is to be deterministic.
    """
    from datetime import date, timedelta

    from qlab.news.feed import fetch_news

    core7 = ["ACWI", "BNDW", "GSG", "IGF", "GLD", "VNQ", "EMB"]
    wide = ["ACWI", "SPY", "QQQ", "IWM", "EEM", "BNDW", "TLT", "IEF", "TIP",
            "LQD", "HYG", "EMB", "GLD", "SLV", "GSG", "DBC", "USO", "IGF",
            "VNQ", "RWO"]
    for universe, expected in ((core7, 7), (wide, 19)):
        counts = {
            len(fetch_news(date(2026, 7, 30) - timedelta(days=k), universe,
                           lookback_hours=48, offline=True))
            for k in range(12)
        }
        # One count, not a range: the in-window total is a function of universe
        # size alone, never of which template the shuffle happened to draw.
        assert counts == {expected}, (len(universe), counts)


def test_one_synthetic_item_stays_outside_the_window():
    """Otherwise the cutoff filter is vacuously true and stops being a test."""
    from datetime import date

    from qlab.news.feed import fetch_news

    wide = ["ACWI", "SPY", "QQQ", "IWM", "EEM", "BNDW", "TLT", "IEF", "TIP",
            "LQD", "HYG", "EMB", "GLD", "SLV", "GSG", "DBC", "USO", "IGF",
            "VNQ", "RWO"]
    inside = fetch_news(date(2026, 7, 30), wide, lookback_hours=48, offline=True)
    outside = fetch_news(date(2026, 7, 30), wide, lookback_hours=168, offline=True)
    assert len(outside) > len(inside)


def test_every_whitelisted_ticker_has_a_synthetic_template():
    """`_fetch_synthetic` silently skips a ticker with no template.

    That is correct for an ad-hoc caller passing arbitrary symbols, but a
    mandate whitelist member with no fixture is a config error that would show
    up only as a quietly thinner window.
    """
    from qlab.news.feed import load_news_sources
    from qlab.trader.mandate import load_mandate

    templates = load_news_sources()["synthetic"]
    missing = [t for t in load_mandate().universe_whitelist if t not in templates]
    assert not missing, f"whitelisted tickers with no synthetic news: {missing}"
