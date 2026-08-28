import json
from datetime import datetime, timezone

import pytest

from qlab.news import feed as news
from qlab.news.providers import gdelt

ARTICLES = {"articles": [
    {"url": "https://www.reuters.com/markets/gold-x", "title": "Gold climbs as yields fall",
     "seendate": "20260827T140000Z", "domain": "reuters.com", "language": "English"},
    {"url": "https://www.ft.com/content/y", "title": "Bullion buyers return",
     "seendate": "20260827T150000Z", "domain": "ft.com", "language": "English"},
    {"url": "https://example.fr/z", "title": "L'or monte", "seendate": "20260827T150000Z",
     "domain": "example.fr", "language": "French"},
]}


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    """Neutralise the inter-request pace and its module-level clock.

    The throttle exists for the live API. Left running it would make every
    test in this file wait on the one before it — the cross-test timing state
    the suite is meant to be free of — for no assertion in return.
    """
    monkeypatch.setattr(gdelt, "_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(gdelt, "_last_request", 0.0)


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def close(self):
        pass


def _payload_urlopen(monkeypatch, payload, calls=None):
    """Serve ``payload`` (bytes or object) to every request; record full_urls."""
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def urlopen(request, timeout):
        if calls is not None:
            calls.append(request.full_url)
        return _Resp(body)

    monkeypatch.setattr(gdelt.urllib.request, "urlopen", urlopen)


def test_articles_map_by_rule_and_carry_their_domain_as_source(monkeypatch):
    monkeypatch.setattr(gdelt, "load_news_sources", lambda: {"gdelt": {"rules": [
        {"query": "gold OR bullion", "tickers": ["GLD"]}]}})
    calls = []
    _payload_urlopen(monkeypatch, ARTICLES, calls)
    items = gdelt.fetch(datetime(2026, 8, 28, tzinfo=timezone.utc), ("GLD", "SPY"))
    assert len(calls) == 1 and "sourcelang:english" in calls[0]
    assert {i.source for i in items} == {"reuters.com", "ft.com"}, "English only"
    assert all(i.tickers == ("GLD",) and i.provider == "gdelt" for i in items)
    # Newest first: a truncated window must show the freshest coverage.
    assert [i.published for i in items] == [
        "2026-08-27T15:00:00+00:00", "2026-08-27T14:00:00+00:00"]


def test_rules_naming_no_held_ticker_are_never_requested(monkeypatch):
    monkeypatch.setattr(gdelt, "load_news_sources", lambda: {"gdelt": {"rules": [
        {"query": "gold OR bullion", "tickers": ["GLD"]},
        {"query": "crude oil OR OPEC", "tickers": ["USO"]}]}})
    calls = []
    _payload_urlopen(monkeypatch, ARTICLES, calls)
    items = gdelt.fetch(datetime(2026, 8, 28, tzinfo=timezone.utc), ("USO",))
    assert len(calls) == 1 and "crude" in calls[0]
    assert all(i.tickers == ("USO",) for i in items)


def test_articles_at_or_after_as_of_are_not_visible(monkeypatch):
    monkeypatch.setattr(gdelt, "load_news_sources", lambda: {"gdelt": {"rules": [
        {"query": "gold OR bullion", "tickers": ["GLD"]}]}})
    _payload_urlopen(monkeypatch, ARTICLES)
    # as_of sits exactly on the earlier article: the window is [as_of - 48h, as_of).
    items = gdelt.fetch(datetime(2026, 8, 27, 14, tzinfo=timezone.utc), ("GLD",))
    assert items == []


def test_a_malformed_payload_raises_rather_than_shortening_the_window(monkeypatch):
    monkeypatch.setattr(gdelt, "load_news_sources", lambda: {"gdelt": {"rules": [
        {"query": "gold OR bullion", "tickers": ["GLD"]}]}})
    _payload_urlopen(monkeypatch, b"<html>rate limited</html>")
    with pytest.raises(ValueError):
        gdelt.fetch(datetime(2026, 8, 28, tzinfo=timezone.utc), ("GLD",))


def test_an_unreachable_api_raises_rather_than_returning_nothing(monkeypatch):
    monkeypatch.setattr(gdelt, "load_news_sources", lambda: {"gdelt": {"rules": [
        {"query": "gold OR bullion", "tickers": ["GLD"]}]}})

    def urlopen(request, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(gdelt.urllib.request, "urlopen", urlopen)
    with pytest.raises(OSError):
        gdelt.fetch(datetime(2026, 8, 28, tzinfo=timezone.utc), ("GLD",))


def test_a_rule_missing_its_query_is_refused_by_name(monkeypatch):
    monkeypatch.setattr(gdelt, "load_news_sources", lambda: {"gdelt": {"rules": [
        {"tickers": ["GLD"]}]}})
    _payload_urlopen(monkeypatch, ARTICLES)
    with pytest.raises(ValueError, match=r"gdelt rule 0 field 'query'"):
        gdelt.fetch(datetime(2026, 8, 28, tzinfo=timezone.utc), ("GLD",))


def _base_config(**extra):
    config = {
        "feeds": [{"name": "Top", "url": "https://example.test/f.xml",
                   "tickers": ["ACWI"]}],
        "synthetic": {"ACWI": [{"source": "s", "headline": "h", "summary": "y"}]},
    }
    config.update(extra)
    return config


def test_config_holds_gdelt_rules_to_their_contract():
    with pytest.raises(ValueError, match=r"gdelt rule 0 field 'query'"):
        news._validate_config(_base_config(gdelt={"rules": [{"tickers": ["GLD"]}]}))
    with pytest.raises(ValueError, match=r"gdelt rule 0 tickers"):
        news._validate_config(_base_config(
            gdelt={"rules": [{"query": "gold", "tickers": []}]}))
    with pytest.raises(ValueError, match=r"gdelt rule 0 must be a mapping"):
        news._validate_config(_base_config(gdelt={"rules": ["gold"]}))
    with pytest.raises(ValueError, match=r"'gdelt.rules' must be a non-empty list"):
        news._validate_config(_base_config(gdelt={"rules": []}))


def test_the_shipped_config_names_gdelt_rules_for_held_tickers():
    from qlab.trader.mandate import load_mandate

    held = {t.upper() for t in load_mandate().universe_whitelist}
    rules = news.load_news_sources()["gdelt"]["rules"]
    assert rules
    for index, rule in enumerate(rules):
        assert rule["query"].strip(), f"rule {index} has no query"
        # A rule naming nothing the desk holds is dead on arrival: fetch skips
        # it every time, so it costs a reader's attention and buys no coverage.
        named = {t.upper() for t in rule["tickers"]}
        assert named & held, f"rule {index} names no held ticker: {sorted(named)}"


def test_gdelt_archives_stories_that_name_no_holding():
    from qlab.news import archive

    assert archive.macro_lane_supported("gdelt")


def test_an_article_missing_the_language_field_is_kept(monkeypatch):
    # The query already carries sourcelang:english. Reading a missing field as
    # "not english" would empty the window on a GDELT schema change — an empty
    # window that is a fact about the payload, not about the press.
    monkeypatch.setattr(gdelt, "load_news_sources", lambda: {"gdelt": {"rules": [
        {"query": "gold OR bullion", "tickers": ["GLD"]}]}})
    _payload_urlopen(monkeypatch, {"articles": [
        {"url": "https://www.reuters.com/markets/gold-x", "title": "Gold climbs",
         "seendate": "20260827T140000Z", "domain": "reuters.com"}]})
    items = gdelt.fetch(datetime(2026, 8, 28, tzinfo=timezone.utc), ("GLD",))
    assert [i.source for i in items] == ["reuters.com"]


def test_an_article_without_a_url_is_refused_by_its_headline(monkeypatch):
    # A secondary-tier article nobody can open corroborates nothing.
    monkeypatch.setattr(gdelt, "load_news_sources", lambda: {"gdelt": {"rules": [
        {"query": "gold OR bullion", "tickers": ["GLD"]}]}})
    _payload_urlopen(monkeypatch, {"articles": [
        {"title": "Gold climbs as yields fall", "seendate": "20260827T140000Z",
         "domain": "reuters.com", "language": "English"}]})
    with pytest.raises(ValueError, match=r"Gold climbs as yields fall"):
        gdelt.fetch(datetime(2026, 8, 28, tzinfo=timezone.utc), ("GLD",))


def test_one_article_matched_by_two_rules_keeps_one_identity(monkeypatch):
    # Two emissions, one story: the archive collapses them by content_hash and
    # unions the ticker edges, so the duplicate is the ticker mapping, not the
    # evidence.
    from qlab.news.grounding import content_hash

    monkeypatch.setattr(gdelt, "load_news_sources", lambda: {"gdelt": {"rules": [
        {"query": "gold OR bullion", "tickers": ["GLD"]},
        {"query": "federal reserve", "tickers": ["TLT"]}]}})
    _payload_urlopen(monkeypatch, {"articles": [ARTICLES["articles"][0]]})
    items = gdelt.fetch(datetime(2026, 8, 28, tzinfo=timezone.utc), ("GLD", "TLT"))
    assert len(items) == 2
    assert content_hash(items[0]) == content_hash(items[1])
    assert {i.tickers for i in items} == {("GLD",), ("TLT",)}
