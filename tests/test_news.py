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
    monkeypatch.delenv("QLAB_NEWS_PROVIDERS", raising=False)
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


def test_the_point_in_time_boundary_has_no_same_day_exemption():
    """`published.startswith(as_of[:10])` let the whole calendar day through.

    An item filed at 23:59 entered a window whose as_of was 12:00 — a twelve-hour
    look-ahead. Invisible while the only caller passes "now", and fatal the
    moment anything replays an intraday point in time against the archive.
    """
    from qlab.news.feed import NewsItem
    from qlab.news.grounding import ground

    late = NewsItem(source="wire", published="2026-07-31T23:59:00+00:00",
                    headline="filed late", summary="s", url="u",
                    tickers=("SPY",), provider="alpaca")
    early = NewsItem(source="wire", published="2026-07-31T09:00:00+00:00",
                     headline="filed early", summary="s", url="u",
                     tickers=("SPY",), provider="alpaca")
    window = ground([late, early], as_of="2026-07-31T12:00:00+00:00",
                    provider="alpaca", universe=["SPY"])
    kept = [i.headline for i in window.items]
    assert kept == ["filed early"]


def test_the_desk_window_asks_for_an_instant_not_a_calendar_date():
    """`date.today().isoformat()` excluded every story filed so far today.

    The feed reads a bare date as local-midnight-labelled-UTC and drops anything
    published after as_of, so the desk's own news window structurally could not
    contain today's news. Measured at 24 hours of exclusion.
    """
    from datetime import datetime, timezone

    from qlab.state.registry import Registry
    from qlab.ui.server import UISession

    seen = {}

    def capture(as_of, universe, **kw):
        seen["as_of"] = as_of
        return []

    session = UISession(offline_default=True, registry=Registry(":memory:"))
    try:
        import qlab.news.feed as feed

        original = feed.fetch_news
        feed.fetch_news = capture
        try:
            session.fetch_desk_news(True)
        finally:
            feed.fetch_news = original
    finally:
        session.registry.close()

    as_of = seen["as_of"]
    # An instant, not a date: a bare date resolves to midnight and the feed
    # drops everything published after it.
    assert isinstance(as_of, datetime)
    now = datetime.now(timezone.utc)
    assert (now - as_of).total_seconds() < 60


def test_every_registered_provider_is_named_in_the_collision_guard():
    # _FIRST_PARTY is what plugin discovery refuses to let an entry point
    # shadow. A first-party provider registered but missing from it could be
    # replaced by a plugin under its own name, so the two must agree.
    assert set(news.PROVIDERS) <= news._FIRST_PARTY


def test_news_check_reports_each_member_of_a_stack(monkeypatch):
    from qlab.news import check, feed
    from qlab.news.feed import NewsItem
    # An instant inside the window, not a literal date: check_news bounds the
    # window at its own `now`, so a fixed timestamp would make this test pass
    # only in the week it was written (and a future one is dropped outright).
    published = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    monkeypatch.setitem(feed.PROVIDERS, "good", lambda a, u: [NewsItem(
        source="BLS", published=published, headline="h", summary="",
        url="https://bls.gov/x", tickers=("TIP",), provider="good")])
    monkeypatch.setitem(feed.PROVIDERS, "bad", lambda a, u: (_ for _ in ()).throw(RuntimeError("down")))
    report = check.check_news(["TIP"], provider="good,bad")
    assert report["members"]["good"]["ok"] is True
    assert report["members"]["bad"]["ok"] is False and "down" in report["members"]["bad"]["error"]
    assert report["ok"] is True, "one living member is a record"


def test_render_names_the_status_for_one_provider_and_for_each_member(monkeypatch):
    # A stack of one must read exactly as the single-provider check always did:
    # the headline is the line an operator greps for, and losing it left the
    # whole report indented as if it were nested under something.
    from qlab.news import check, feed

    published = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    monkeypatch.setitem(feed.PROVIDERS, "good", lambda a, u: [NewsItem(
        source="BLS", published=published, headline="h", summary="",
        url="https://bls.gov/x", tickers=("TIP",), provider="good")])
    monkeypatch.setitem(
        feed.PROVIDERS, "bad",
        lambda a, u: (_ for _ in ()).throw(RuntimeError("down")))

    one = check.render(check.check_news(["TIP"], provider="good"))
    assert one.startswith("news integration: OK")
    assert len([ln for ln in one.splitlines() if "  provider  " in ln]) == 1

    stack = check.render(check.check_news(["TIP"], provider="good,bad"))
    assert stack.startswith("news integration: OK")
    members = [ln for ln in stack.splitlines() if "  provider  " in ln]
    assert len(members) == 2
    assert "[OK]" in members[0] and "[NOT WORKING]" in members[1]


def test_credential_status_reports_the_whole_stack_not_just_the_singular(monkeypatch):
    # The plural is what the desk reads; a status line naming only the singular
    # reports a configuration the owner is not running.
    from qlab.env import credential_status

    monkeypatch.setenv("QLAB_NEWS_PROVIDERS", "alpaca,edgar,macro")
    monkeypatch.delenv("QLAB_NEWS_PROVIDER", raising=False)
    assert credential_status()["news_provider"] == "alpaca,edgar,macro"
    monkeypatch.delenv("QLAB_NEWS_PROVIDERS")
    monkeypatch.setenv("QLAB_NEWS_PROVIDER", "rss")
    assert credential_status()["news_provider"] == "rss"


def test_an_edgar_only_stack_is_not_ready_without_a_contact(monkeypatch):
    """Every edgar fetch refuses without QLAB_EDGAR_CONTACT, so counting it as
    a ready news lane promises a window the desk cannot fetch."""
    from qlab.env import credential_status

    monkeypatch.setenv("QLAB_NEWS_PROVIDERS", "edgar")
    monkeypatch.delenv("QLAB_NEWS_PROVIDER", raising=False)
    monkeypatch.delenv("QLAB_EDGAR_CONTACT", raising=False)
    assert credential_status()["news_ready"] is False

    monkeypatch.setenv("QLAB_EDGAR_CONTACT", "A Quant <quant@example.org>")
    assert credential_status()["news_ready"] is True

    # A stack that also holds a keyless real provider is ready either way:
    # readiness is "can any member answer", not "can all of them".
    monkeypatch.delenv("QLAB_EDGAR_CONTACT")
    monkeypatch.setenv("QLAB_NEWS_PROVIDERS", "edgar,macro")
    assert credential_status()["news_ready"] is True


def _rss_payload(headline: str) -> bytes:
    return (
        b"<rss version=\"2.0\"><channel><item>"
        b"<title>" + headline.encode() + b"</title>"
        b"<description>World stocks show wider dispersion.</description>"
        b"<link>https://example.test/story</link>"
        b"<pubDate>Wed, 15 Jan 2025 10:00:00 GMT</pubDate>"
        b"</item></channel></rss>"
    )


def test_one_dead_feed_does_not_take_its_live_neighbours_with_it(monkeypatch):
    # Measured on 2026-08-28: bls.gov returned 403 to every request, and the
    # whole macro provider went dark although BEA was answering. A dead feed
    # must neither kill the live ones nor disappear without a sentence.
    class Response:
        def read(self):
            return _rss_payload("Global equities face a busier volatility backdrop")

        def close(self):
            pass

    def urlopen(request, timeout):
        if "dead" in request.full_url:
            raise URLError("HTTP Error 403: Forbidden")
        return Response()

    monkeypatch.setattr(news.urllib.request, "urlopen", urlopen)
    monkeypatch.setitem(sys.modules, "feedparser", None)
    feeds = [
        {"name": "Dead Feed", "url": "https://example.test/dead.xml",
         "tickers": ["ACWI"]},
        {"name": "Live Feed", "url": "https://example.test/live.xml",
         "tickers": ["ACWI"]},
    ]
    with pytest.raises(news.PartialWindow) as excinfo:
        news._fetch_rss_feeds(
            feeds, datetime(2025, 1, 15, 12, tzinfo=timezone.utc), ("ACWI",))
    partial = excinfo.value
    assert [item.source for item in partial.items] == ["Live Feed"]
    assert "403" in partial.failures["Dead Feed"]
    assert list(partial.failures) == ["Dead Feed"]


def test_every_feed_dead_is_still_the_plain_refusal(monkeypatch):
    def urlopen(request, timeout):
        raise URLError("offline")

    monkeypatch.setattr(news.urllib.request, "urlopen", urlopen)
    feeds = [{"name": "Dead Feed", "url": "https://example.test/dead.xml",
              "tickers": ["ACWI"]}]
    with pytest.raises(RuntimeError) as excinfo:
        news._fetch_rss_feeds(
            feeds, datetime(2025, 1, 15, 12, tzinfo=timezone.utc), ("ACWI",))
    assert not isinstance(excinfo.value, news.PartialWindow)
    assert "requires reachable network feeds" in str(excinfo.value)


def test_a_partial_windows_records_still_go_through_the_window_contract(monkeypatch):
    # Propagating the provider's RAW items would hand the caller records that
    # skipped the look-ahead gate, the universe mapping and the provider stamp
    # — the one path into the desk that is not point-in-time.
    def partial(as_of, universe):
        return [
            NewsItem(source="BEA", published="2025-01-15T10:00:00+00:00",
                     headline="kept", summary="", url="https://x/1",
                     tickers=("ACWI",), provider="whatever"),
            NewsItem(source="BEA", published="2025-01-16T10:00:00+00:00",
                     headline="after as_of", summary="", url="https://x/2",
                     tickers=("ACWI",), provider="whatever"),
        ]

    def fetch(as_of, universe):
        raise news.PartialWindow(partial(as_of, universe), {"BLS": "403"})

    monkeypatch.setitem(news.PROVIDERS, "macro", fetch)
    with pytest.raises(news.PartialWindow) as excinfo:
        news.fetch_news("2025-01-15T12:00:00+00:00", ["ACWI"], provider="macro")
    items = excinfo.value.items
    assert [i.headline for i in items] == ["kept"]
    assert items[0].provider == "macro"
    assert excinfo.value.failures == {"BLS": "403"}


def test_check_reports_a_partial_member_as_working_and_names_what_it_lost(
        monkeypatch):
    # The generic `except Exception` turned a partial answer into NOT WORKING
    # and threw the records away — the exact outcome PartialWindow exists to
    # prevent, in the one place an operator looks to find out what is wrong.
    from qlab.news import check

    published = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    def partial(as_of, universe):
        raise news.PartialWindow(
            [NewsItem(source="Bureau of Economic Analysis", published=published,
                      headline="GDP, second estimate", summary="",
                      url="https://apps.bea.gov/x", tickers=("TIP",),
                      provider="macro")],
            {"BLS": "HTTP Error 403: Forbidden"})

    monkeypatch.setitem(news.PROVIDERS, "macro", partial)
    report = check.check_news(["TIP"], provider="macro")
    assert report["ok"] is True
    assert report["fetched"] == 1 and report["kept"] == 1
    flags = report["quality_flags"]
    assert any(f.startswith("partial: ") and "BLS" in f and "403" in f
               for f in flags)
    rendered = check.render(report)
    assert rendered.startswith("news integration: OK")
    assert any("partial: " in line and "BLS" in line
               for line in rendered.splitlines())
