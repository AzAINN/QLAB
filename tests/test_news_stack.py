from datetime import datetime, timezone
from importlib import metadata

import pytest

from qlab.news import feed
from qlab.news.feed import NewsItem


def test_plugin_providers_are_discovered_from_the_entry_point_group(monkeypatch):
    def fetch(as_of, universe):
        return []

    class Ep:
        name = "acme"
        group = "qlab.news.providers"

        def load(self):
            return fetch

    monkeypatch.setattr(
        metadata, "entry_points",
        lambda **kw: [Ep()] if kw.get("group") == "qlab.news.providers" else [])
    # setitem, not delitem: monkeypatch records an undo only for a key it
    # already saw, so deleting an absent key registers nothing and the
    # discovered provider would outlive this test in PROVIDERS.
    monkeypatch.setitem(feed.PROVIDERS, "acme", None)
    found = feed.load_plugin_providers()
    assert found["acme"] is fetch
    assert feed.PROVIDERS["acme"] is fetch


def test_a_plugin_may_not_shadow_a_first_party_provider(monkeypatch):
    class Ep:
        name = "alpaca"
        group = "qlab.news.providers"

        def load(self):
            return lambda a, u: []

    monkeypatch.setattr(metadata, "entry_points", lambda **kw: [Ep()])
    with pytest.raises(RuntimeError, match="shadows the first-party provider"):
        feed.load_plugin_providers()


def _item(provider, source, headline, published="2026-08-27T12:00:00+00:00"):
    return NewsItem(source=source, published=published, headline=headline,
                    summary="", url=f"https://x/{headline}", tickers=("SPY",),
                    provider=provider)


def test_the_stack_is_parsed_from_the_plural_then_the_singular(monkeypatch):
    monkeypatch.setenv("QLAB_NEWS_PROVIDERS", "alpaca, edgar ,gdelt")
    assert feed.parse_provider_stack(None) == ("alpaca", "edgar", "gdelt")
    monkeypatch.delenv("QLAB_NEWS_PROVIDERS")
    monkeypatch.setenv("QLAB_NEWS_PROVIDER", "rss")
    assert feed.parse_provider_stack(None) == ("rss",)
    assert feed.parse_provider_stack("edgar") == ("edgar",)
    monkeypatch.delenv("QLAB_NEWS_PROVIDER")
    assert feed.parse_provider_stack(None) == ("synthetic",)


def test_a_stack_merges_members_and_reports_each_outcome(monkeypatch):
    # Distinct publication times, and the older one first in stack order, so
    # the merge has to interleave across members rather than concatenate them.
    monkeypatch.setitem(
        feed.PROVIDERS, "one",
        lambda a, u: [_item("one", "A", "h1", "2026-08-27T09:00:00+00:00")])
    monkeypatch.setitem(
        feed.PROVIDERS, "two",
        lambda a, u: [_item("two", "B", "h2", "2026-08-27T15:00:00+00:00")])

    def dead(a, u):
        raise RuntimeError("feed X is unavailable")

    monkeypatch.setitem(feed.PROVIDERS, "dead", dead)
    window = feed.fetch_news_stacked(
        datetime(2026, 8, 28, tzinfo=timezone.utc), ("SPY",), ("one", "two", "dead"))
    assert {i.provider for i in window.items} == {"one", "two"}
    assert [i.headline for i in window.items] == ["h2", "h1"]
    assert window.outcomes["one"] == "ok"
    assert window.outcomes["two"] == "ok"
    assert "unavailable" in window.outcomes["dead"]


def test_a_stack_of_one_is_element_for_element_a_plain_fetch(monkeypatch):
    # The reason _ordering_key is shared rather than copied: a one-member
    # stack must be indistinguishable from the fetch it wraps.
    def fetch(a, u):
        return [
            _item("one", "B", "later", "2026-08-27T15:00:00+00:00"),
            _item("one", "A", "tied", "2026-08-27T09:00:00+00:00"),
            _item("one", "C", "tied", "2026-08-27T09:00:00+00:00"),
        ]

    monkeypatch.setitem(feed.PROVIDERS, "one", fetch)
    as_of = datetime(2026, 8, 28, tzinfo=timezone.utc)
    window = feed.fetch_news_stacked(as_of, ("SPY",), ("one",))
    assert window.items == feed.fetch_news(as_of, ("SPY",), provider="one")


def test_a_stack_member_may_come_from_a_plugin_entry_point(monkeypatch):
    # The stack is load_plugin_providers()'s call site: "acme" is never put in
    # PROVIDERS by the test, so this fails if the discovery call is removed.
    def fetch(a, u):
        return [_item("acme", "A", "h1")]

    class Ep:
        name = "acme"
        group = "qlab.news.providers"

        def load(self):
            return fetch

    monkeypatch.setattr(
        metadata, "entry_points",
        lambda **kw: [Ep()] if kw.get("group") == "qlab.news.providers" else [])
    monkeypatch.setitem(feed.PROVIDERS, "acme", None)
    window = feed.fetch_news_stacked(
        datetime(2026, 8, 28, tzinfo=timezone.utc), ("SPY",), ("acme",))
    assert window.outcomes["acme"] == "ok"
    assert [i.headline for i in window.items] == ["h1"]
    assert window.items[0].provider == "acme"


def test_a_stack_of_all_dead_members_raises(monkeypatch):
    def dead(a, u):
        raise RuntimeError("down")

    monkeypatch.setitem(feed.PROVIDERS, "dead", dead)
    with pytest.raises(RuntimeError, match="every provider in the stack failed"):
        feed.fetch_news_stacked(
            datetime(2026, 8, 28, tzinfo=timezone.utc), ("SPY",), ("dead",))


def test_the_merged_window_is_what_provenance_reports(monkeypatch):
    # fetch_news writes the in-process cache per call, so a stack used to leave
    # it holding only the LAST member's records: cached_news_provenance then
    # described a fraction of the window the desk had actually read, and named
    # the wrong provider for it. The merge is what the desk read, so the merge
    # is what the cache must hold.
    monkeypatch.setitem(
        feed.PROVIDERS, "one",
        lambda a, u: [_item("one", "A", "h1", "2026-08-27T09:00:00+00:00"),
                      _item("one", "A", "h1b", "2026-08-27T10:00:00+00:00")])
    monkeypatch.setitem(
        feed.PROVIDERS, "two",
        lambda a, u: [_item("two", "B", "h2", "2026-08-27T15:00:00+00:00")])
    window = feed.fetch_news_stacked(
        datetime(2026, 8, 28, tzinfo=timezone.utc), ("SPY",), ("one", "two"))
    assert len(window.items) == 3
    assert feed.cached_news_provenance(("SPY",)) == ("one", 3)


def test_an_all_dead_stack_keeps_each_members_own_sentence(monkeypatch):
    # The aggregate sentence stamped on every member said each of them failed
    # for the union of all their reasons — a diagnosis no member ever gave.
    def dead(msg):
        def fetch(a, u):
            raise RuntimeError(msg)
        return fetch

    monkeypatch.setitem(feed.PROVIDERS, "one", dead("feed X is 403"))
    monkeypatch.setitem(feed.PROVIDERS, "two", dead("feed Y timed out"))
    with pytest.raises(feed.StackFailed) as excinfo:
        feed.fetch_news_stacked(
            datetime(2026, 8, 28, tzinfo=timezone.utc), ("SPY",), ("one", "two"))
    assert excinfo.value.outcomes == {"one": "feed X is 403",
                                      "two": "feed Y timed out"}
    # Still a RuntimeError: every existing caller catches it as one.
    assert isinstance(excinfo.value, RuntimeError)
