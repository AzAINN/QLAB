"""Grounded news: point-in-time, immutable, corroborated."""

from __future__ import annotations

import os

import pytest

from qlab.news.feed import NewsItem
from qlab.news.grounding import by_ticker, content_hash, ground


def _item(headline, source="wire-a", published="2026-07-25T10:00:00+00:00",
          tickers=("ACWI",), summary="", url="http://x/1"):
    return NewsItem(source=source, published=published, headline=headline,
                    summary=summary, url=url, tickers=tuple(tickers),
                    provider="test")


AS_OF = "2026-07-26T00:00:00+00:00"


# --- immutability ------------------------------------------------------------


def test_content_hash_changes_when_a_publisher_edits_a_headline():
    original = _item("Fed holds rates steady")
    edited = _item("Fed holds rates steady, signals caution")
    assert content_hash(original) != content_hash(edited)
    # And is stable for an unchanged record.
    assert content_hash(original) == content_hash(_item("Fed holds rates steady"))


def test_window_hash_identifies_the_exact_evidence_set():
    a = ground([_item("A"), _item("B", url="http://x/2")],
               as_of=AS_OF, provider="test")
    b = ground([_item("B", url="http://x/2"), _item("A")],
               as_of=AS_OF, provider="test")
    # Order must not change the identity of the window.
    assert a.window_hash == b.window_hash
    c = ground([_item("A")], as_of=AS_OF, provider="test")
    assert c.window_hash != a.window_hash


# --- point in time -----------------------------------------------------------


def test_records_from_the_future_are_dropped_and_counted():
    grounded = ground(
        [_item("Past event", published="2026-07-25T10:00:00+00:00"),
         _item("Tomorrow's news", published="2026-07-28T10:00:00+00:00",
               url="http://x/2")],
        as_of=AS_OF, provider="test")
    assert len(grounded.items) == 1
    assert grounded.dropped_future == 1
    assert any("look-ahead" in f for f in grounded.quality_flags)


def test_records_outside_the_universe_are_dropped():
    grounded = ground(
        [_item("About our book", tickers=("ACWI",)),
         _item("About something else", tickers=("TSLA",), url="http://x/2")],
        as_of=AS_OF, provider="test", universe=["ACWI", "BNDW"])
    assert len(grounded.items) == 1
    assert grounded.dropped_untagged == 1


# --- corroboration -----------------------------------------------------------


def test_two_publishers_on_one_story_is_corroboration():
    grounded = ground([
        _item("Fed signals extended pause on interest rates", source="wire-a"),
        _item("Fed signals extended pause on interest rates policy",
              source="wire-b", url="http://y/1"),
    ], as_of=AS_OF, provider="test")
    assert len(grounded.claims) == 1
    claim = grounded.claims[0]
    assert claim.corroboration == 2 and claim.corroborated is True
    assert set(claim.sources) == {"wire-a", "wire-b"}


def test_one_publisher_repeating_itself_is_not_corroboration():
    """Five follow-ups from one outlet are one claim, not five confirmations."""
    items = [_item("Fed signals extended pause on interest rates policy",
                   source="wire-a", url=f"http://x/{i}") for i in range(5)]
    grounded = ground(items, as_of=AS_OF, provider="test")
    assert len(grounded.claims) == 1
    assert grounded.claims[0].corroboration == 1
    assert grounded.claims[0].corroborated is False
    assert any("one secondary publisher" in f for f in grounded.quality_flags)


def test_unrelated_stories_stay_separate_claims():
    grounded = ground([
        _item("Fed signals extended pause on interest rates", source="wire-a"),
        _item("Oil inventories build sharply in gulf coast storage",
              source="wire-b", tickers=("GSG",), url="http://y/1"),
    ], as_of=AS_OF, provider="test")
    assert len(grounded.claims) == 2
    assert all(not c.corroborated for c in grounded.claims)


def test_claims_are_ranked_with_corroborated_first():
    grounded = ground([
        _item("Solo story about something entirely different here",
              source="wire-a", url="http://x/9"),
        _item("Fed signals extended pause on interest rates", source="wire-b",
              url="http://y/1"),
        _item("Fed signals extended pause on interest rates policy",
              source="wire-c", url="http://z/1"),
    ], as_of=AS_OF, provider="test")
    assert grounded.claims[0].corroboration == 2
    assert grounded.corroborated_claims == [grounded.claims[0]]


def test_by_ticker_indexes_claims():
    grounded = ground([
        _item("Fed pause", tickers=("BNDW", "EMB")),
        _item("Oil builds", tickers=("GSG",), url="http://y/1", source="wire-b"),
    ], as_of=AS_OF, provider="test")
    index = by_ticker(grounded)
    assert set(index) == {"BNDW", "EMB", "GSG"}


# --- the empty case ----------------------------------------------------------


def test_an_empty_window_is_flagged_not_silent():
    grounded = ground([], as_of=AS_OF, provider="test")
    assert grounded.items == []
    assert "empty window" in grounded.quality_flags
    assert grounded.to_dict()["corroborated_count"] == 0


# --- source authority --------------------------------------------------------


def test_a_primary_source_stands_alone():
    """A central bank announcing its own decision IS the event.

    Demanding three outlets confirm a Fed release gets the epistemics
    backwards, and would rank a well-sourced rumour above a primary document.
    """
    grounded = ground([_item("Monetary policy decisions",
                             source="European Central Bank")],
                      as_of=AS_OF, provider="test")
    claim = grounded.claims[0]
    assert claim.tier == "primary"
    assert claim.corroborated is True          # despite corroboration == 1
    assert claim.support == "primary source"
    assert grounded.corroborated_claims == [claim]


def test_a_single_secondary_source_is_not_enough():
    grounded = ground([_item("Traders whisper about a surprise cut",
                             source="SomeBlog")],
                      as_of=AS_OF, provider="test")
    claim = grounded.claims[0]
    assert claim.tier == "secondary"
    assert claim.corroborated is False
    assert claim.support == "single secondary source"


def test_primary_ranks_above_corroborated_secondary():
    grounded = ground([
        _item("Two outlets discussing an unrelated market rumour today",
              source="wire-a", url="http://a/1"),
        _item("Two outlets discussing an unrelated market rumour now",
              source="wire-b", url="http://b/1"),
        _item("Monetary policy decisions", source="Federal Reserve",
              url="http://fed/1"),
    ], as_of=AS_OF, provider="test")
    assert grounded.claims[0].tier == "primary"
    assert grounded.claims[1].corroboration == 2


def test_a_secondary_echo_does_not_weaken_a_primary_claim():
    grounded = ground([
        _item("Federal Reserve holds rates steady at current levels",
              source="Federal Reserve"),
        _item("Federal Reserve holds rates steady at current target levels",
              source="wire-a", url="http://a/1"),
    ], as_of=AS_OF, provider="test")
    assert len(grounded.claims) == 1
    assert grounded.claims[0].tier == "primary"


def test_a_window_of_only_primary_releases_is_not_flagged_as_unsupported():
    grounded = ground([
        _item("Monetary policy decisions", source="European Central Bank",
              url="http://e/1"),
        _item("Survey on credit terms published", source="European Central Bank",
              url="http://e/2"),
        _item("Banknote design shortlist announced",
              source="European Central Bank", url="http://e/3"),
    ], as_of=AS_OF, provider="test")
    assert not any("corroborated" in f for f in grounded.quality_flags)


# --- dotenv + integration check ----------------------------------------------


def test_dotenv_never_overrides_an_exported_variable(tmp_path, monkeypatch):
    from qlab.env import load_dotenv

    env = tmp_path / ".env"
    env.write_text("ALPACA_API_KEY=from_file\nQLAB_NEWS_PROVIDER=rss\n")
    monkeypatch.setenv("ALPACA_API_KEY", "from_shell")
    monkeypatch.delenv("QLAB_NEWS_PROVIDER", raising=False)

    applied = load_dotenv(env)
    # An explicitly exported credential outranks the file.
    assert os.environ["ALPACA_API_KEY"] == "from_shell"
    assert "ALPACA_API_KEY" not in applied
    assert os.environ["QLAB_NEWS_PROVIDER"] == "rss"


def test_dotenv_parsing_handles_quotes_exports_and_comments():
    from qlab.env import parse_env

    parsed = parse_env(
        '# comment\n'
        'export ALPACA_API_KEY=abc123  # trailing\n'
        'QUOTED="has # hash"\n'
        "SINGLE='sq'\n"
        '\n'
        'NOT_A_LINE\n')
    assert parsed["ALPACA_API_KEY"] == "abc123"
    assert parsed["QUOTED"] == "has # hash"     # quoted hash is not a comment
    assert parsed["SINGLE"] == "sq"
    assert "NOT_A_LINE" not in parsed


def test_check_reports_synthetic_as_not_real_news(monkeypatch):
    from qlab.news.check import check_news, render

    monkeypatch.delenv("QLAB_NEWS_PROVIDER", raising=False)
    report = check_news(["ACWI"])
    assert report["ok"] is False
    assert "deterministic fixtures" in report["error"]
    assert "NOT WORKING" in render(report)


def test_check_names_missing_alpaca_credentials(monkeypatch):
    from qlab.news.check import check_news

    monkeypatch.setenv("QLAB_NEWS_PROVIDER", "alpaca")
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    report = check_news(["ACWI"])
    assert report["ok"] is False
    assert "ALPACA_API_KEY" in report["error"]


def test_check_never_echoes_a_credential(monkeypatch):
    from qlab.news.check import check_news, render

    monkeypatch.setenv("QLAB_NEWS_PROVIDER", "alpaca")
    monkeypatch.setenv("ALPACA_API_KEY", "SUPERSECRETKEY")
    monkeypatch.setenv("ALPACA_API_SECRET", "SUPERSECRETSECRET")
    rendered = render(check_news(["ACWI"]))
    assert "SUPERSECRET" not in rendered
