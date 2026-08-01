"""The durable news record: what it keeps, and what it refuses to claim.

Two governance lines are defended here. The archive is built from the RAW
window, so the macro story that grounding deletes is still answerable later;
and the search seam emits states and prose only, so no path leads from a
headline to a size.
"""

from __future__ import annotations

import ast
import inspect
import random
import re
import socket
import urllib.request
from dataclasses import dataclass, fields, replace
from datetime import date, datetime, timedelta, timezone

import pytest

from qlab.news import archive, feed
from qlab.news.archive import (
    EMPTY_WINDOW_FINGERPRINT,
    MIN_ARCHIVE_ITEMS,
    ArchiveRejected,
    NewsRow,
    RelevanceReport,
    build_archive_batch,
    canonical_timestamp,
    is_canonical,
    macro_lane_supported,
    normalise_terms,
    relevance_report,
    window_fingerprint,
)
from qlab.news.feed import NewsItem, fetch_news
from qlab.news.grounding import content_hash, ground

UNIVERSE = ("SPY", "QQQ", "TLT", "GLD")
AS_OF = "2026-07-31T00:00:00+00:00"
FIRST_SEEN = "2026-07-31T00:00:05+00:00"
P1 = "2026-07-30T09:00:00+00:00"
P2 = "2026-07-30T11:30:00+00:00"


def _item(headline="Chipmaker guides higher", **over) -> NewsItem:
    base = dict(
        source="Reuters",
        published=P1,
        headline=headline,
        summary="The company said orders rose through the quarter.",
        url="https://example.invalid/1",
        tickers=(),
        provider="alpaca",
    )
    base.update(over)
    base["tickers"] = tuple(base["tickers"])
    return NewsItem(**base)


@dataclass(frozen=True)
class _RecalledItem:
    """A NewsItem that also carries entity-recalled symbols.

    NewsItem has no ``raw_tickers`` field today; the archive reads it through
    ``getattr`` so it keeps working either way. This stand-in proves the
    optional half without editing the feed.
    """

    source: str
    published: str
    headline: str
    summary: str
    url: str
    tickers: tuple[str, ...]
    provider: str
    raw_tickers: tuple[str, ...]


def _batch(items, **over):
    kwargs = dict(
        provider="alpaca", offline=False, as_of=AS_OF, lookback_hours=48,
        universe=UNIVERSE, first_seen=FIRST_SEEN,
    )
    kwargs.update(over)
    return build_archive_batch(items, **kwargs)


# --------------------------------------------------------------------------
# identity, and what the batch is built from
# --------------------------------------------------------------------------


def test_item_hash_is_grounding_content_hash():
    items = fetch_news(date(2026, 7, 30), list(UNIVERSE), lookback_hours=48,
                       offline=True)
    assert items, "the synthetic provider must return a window to test against"

    batch = _batch(items, provider="synthetic", offline=True,
                   as_of="2026-07-30T00:00:00+00:00")

    by_hash = {row.item_hash: row for row in batch.rows}
    for item in items:
        expected = content_hash(item)
        # A second hash would fork evidence identity: a stored citation and a
        # live Claim.item_hashes entry would be different strings.
        assert expected in by_hash
        assert by_hash[expected].headline == item.headline
    assert all(len(h) == 16 for h in by_hash)


def test_batch_keeps_macro_items_that_grounding_would_drop():
    samsung = _item("Samsung flags a memory shortage", tickers=())

    batch = _batch([samsung])
    grounded = ground([samsung], as_of=AS_OF, provider="alpaca",
                      universe=list(UNIVERSE))

    # The archive keeps it...
    assert len(batch.rows) == 1
    assert batch.rows[0].headline == "Samsung flags a memory shortage"
    assert batch.ticker_edges == ()
    # ...and grounding deletes it. Feeding the archive from grounded.items is
    # exactly what makes "what would have made Samsung surge" unanswerable.
    assert grounded.items == []
    assert grounded.dropped_untagged == 1


def test_duplicate_item_hash_in_one_batch_collapses_to_first():
    first = _item("Rates hold steady", tickers=("SPY",))
    other = _item("Gold firms", url="https://example.invalid/2",
                  tickers=("GLD",))
    # Identical text, different mapping: content_hash covers neither tickers
    # nor provider, so this is the same record.
    duplicate = replace(first, tickers=("QQQ",))

    batch = _batch([first, other, duplicate])

    assert batch.returned == 3
    assert batch.duplicates_collapsed == 1
    assert len(batch.rows) == 2
    # First occurrence wins, in place: the surviving row keeps position 0
    # rather than being re-appended, so the registry's seen_count arithmetic
    # is exactly right rather than incidentally right.
    assert batch.rows[0].item_hash == content_hash(first)
    assert batch.rows[1].item_hash == content_hash(other)
    # The collapsed copy's sighting still unions in.
    same = {e.ticker for e in batch.ticker_edges
            if e.item_hash == content_hash(first)}
    assert same == {"SPY", "QQQ"}


def test_ticker_edges_union_across_a_universe_change():
    item = _item(tickers=("SPY", "QQQ"))

    narrow = _batch([item], universe=("SPY",))
    wide = _batch([item], universe=("SPY", "QQQ"))

    narrow_edges = {(e.ticker, e.in_universe) for e in narrow.ticker_edges}
    wide_edges = {(e.ticker, e.in_universe) for e in wide.ticker_edges}
    assert narrow_edges == {("SPY", True), ("QQQ", False)}
    assert wide_edges == {("SPY", True), ("QQQ", True)}
    assert narrow_edges != wide_edges
    # Building the second batch must not have disturbed the first: the two
    # mappings union in the registry, they never overwrite.
    assert {(e.ticker, e.in_universe) for e in narrow.ticker_edges} == narrow_edges
    assert narrow.rows[0].item_hash == wide.rows[0].item_hash


def test_out_of_universe_symbols_are_recorded_not_dropped():
    item = _item(tickers=("SPY", "QQQ"))

    empty_universe = _batch([item], universe=())
    assert {(e.ticker, e.in_universe) for e in empty_universe.ticker_edges} == {
        ("SPY", False), ("QQQ", False)}

    recalled = _RecalledItem(
        source=item.source, published=item.published, headline=item.headline,
        summary=item.summary, url=item.url, tickers=("SPY",),
        provider=item.provider, raw_tickers=("005930.KS", "NVDA"))
    with_recall = _batch([recalled])
    plain = _batch([replace(item, tickers=("SPY",))])

    edges = {(e.ticker, e.in_universe) for e in with_recall.ticker_edges}
    assert edges == {("SPY", True), ("005930.KS", False), ("NVDA", False)}
    # Without the attribute the module behaves identically minus those edges,
    # so entity recall degrades visibly rather than crashing.
    assert {(e.ticker, e.in_universe) for e in plain.ticker_edges} == {
        ("SPY", True)}
    assert with_recall.rows[0].item_hash == plain.rows[0].item_hash


# --------------------------------------------------------------------------
# the point-in-time boundary
# --------------------------------------------------------------------------


def test_non_canonical_timestamp_is_refused_loudly():
    # The exact output shape of UISession._now_iso, which carries microseconds.
    live = datetime.now(timezone.utc).isoformat()
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+00:00$", live)
    micros = datetime(2026, 7, 31, 12, 0, 0, 123456,
                      tzinfo=timezone.utc).isoformat()
    assert not is_canonical(micros)

    with pytest.raises(ArchiveRejected) as excinfo:
        _batch([_item()], first_seen=micros)
    message = str(excinfo.value)
    assert "first_seen" in message and "+00:00" in message

    # The same refusal on the look-ahead side: one microsecond-bearing row
    # silently defeats a lexicographic point-in-time compare.
    with pytest.raises(ArchiveRejected) as excinfo:
        _batch([_item(published="2026-07-30T09:00:00.500000+00:00")])
    assert "published" in str(excinfo.value)

    with pytest.raises(ArchiveRejected) as excinfo:
        _batch([_item(published="")])
    assert "published" in str(excinfo.value)


def test_canonical_timestamp_matches_feed_iso_timestamp():
    aware = datetime(2026, 7, 31, 9, 30, 0, 500000,
                     tzinfo=timezone(timedelta(hours=-4)))
    assert canonical_timestamp(aware) == feed._iso_timestamp(aware)
    assert canonical_timestamp(aware) == "2026-07-31T13:30:00+00:00"
    assert is_canonical(canonical_timestamp(aware))
    # 'Z' is a different spelling of the same instant and the schema CHECK
    # rejects it, so the archive must too.
    assert not is_canonical("2026-07-31T13:30:00Z")
    assert canonical_timestamp("2026-07-31T13:30:00Z") == "2026-07-31T13:30:00+00:00"
    with pytest.raises(ArchiveRejected):
        canonical_timestamp("not a timestamp")


# --------------------------------------------------------------------------
# the window itself
# --------------------------------------------------------------------------


def test_window_fingerprint_is_stable_under_reordering_and_covers_the_error():
    items = [_item("A", url="https://example.invalid/a"),
             _item("B", url="https://example.invalid/b", published=P2),
             _item("C", url="https://example.invalid/c")]
    shuffled = list(items)
    random.Random(7).shuffle(shuffled)

    base = window_fingerprint(items, provider="alpaca", error=None)
    assert window_fingerprint(shuffled, provider="alpaca", error=None) == base
    assert _batch(shuffled).window_fingerprint == base

    boom = window_fingerprint(items, provider="alpaca", error="boom")
    other = window_fingerprint(items, provider="alpaca", error="other")
    # A second, different error must register as a change; a fingerprint over
    # content alone would record it as "nothing happened".
    assert len({base, boom, other}) == 3
    assert window_fingerprint(items, provider="rss", error=None) != base


def test_batch_consumes_a_one_shot_iterable_exactly_once():
    items = [_item("A", url="https://example.invalid/a", tickers=("SPY",)),
             _item("B", url="https://example.invalid/b", published=P2)]
    streamed = _batch(iter(items))
    # The window is walked three times inside the builder; a generator left
    # unmaterialised would fingerprint an exhausted iterator and report a
    # quiet wire for a window that had rows in it.
    assert streamed.returned == 2
    assert len(streamed.rows) == 2
    assert streamed.window_fingerprint == _batch(items).window_fingerprint
    assert streamed.window_fingerprint != EMPTY_WINDOW_FINGERPRINT


def test_batch_is_pure_no_clock_no_network(monkeypatch):
    class _NoClock(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D102
            raise AssertionError("the archive read the clock")

        @classmethod
        def utcnow(cls):  # noqa: D102
            raise AssertionError("the archive read the clock")

    def _boom(*args, **kwargs):
        raise AssertionError("the archive touched the network")

    monkeypatch.setattr(archive, "datetime", _NoClock, raising=False)
    monkeypatch.setattr(feed, "datetime", _NoClock)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    monkeypatch.setattr(socket, "socket", _boom)

    batch = _batch([_item(tickers=("SPY",))])
    assert batch.rows[0].first_seen == FIRST_SEEN == batch.rows[0].last_seen
    assert batch.rows[0].seen_count == 1

    # Purity is enforced, not asserted: nothing outside this allowlist may be
    # imported, or first_seen would stop being the caller's knowledge boundary.
    allowed = {"__future__", "hashlib", "re", "unicodedata", "dataclasses",
               "datetime", "typing", "qlab.news.feed", "qlab.news.grounding",
               "qlab.news.qualitative"}
    tree = ast.parse(inspect.getsource(archive))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported <= allowed, f"unexpected imports: {imported - allowed}"


def test_synthetic_provider_marks_every_row():
    items = [_item("A", url="https://example.invalid/a", provider="synthetic"),
             _item("B", url="https://example.invalid/b", provider="synthetic")]
    synthetic = _batch(items, provider="synthetic", offline=True)
    assert [r.synthetic for r in synthetic.rows] == [True, True]

    real = [replace(i, provider="alpaca") for i in items]
    live = _batch(real, provider="alpaca")
    assert [r.synthetic for r in live.rows] == [False, False]

    # Storable but never citable is a stored property, not a query default
    # every caller has to remember; a mixed window is refused outright.
    with pytest.raises(ArchiveRejected):
        _batch([items[0]], provider="alpaca")


def test_body_field_is_never_named_summary():
    item = _item(summary="2024")
    batch = _batch([item])
    row = batch.rows[0]

    names = {f.name for f in fields(NewsRow)}
    assert "summary" not in names
    assert "summary" not in row.to_dict()
    assert "summary" not in batch.to_dict()["rows"][0]
    # Registry._rows json.loads any column literally named summary, so this
    # body would come back as int 2024 on that one row.
    assert row.body_text == "2024"

    # An empty string is not a value.
    blank = _batch([_item(summary="", url="")]).rows[0]
    assert blank.body_text is None and blank.url is None


def test_to_row_is_positional_in_schema_column_order():
    row = _batch([_item(tickers=("SPY",))]).rows[0]
    assert row.to_row() == (
        row.item_hash, row.published, row.first_seen, row.last_seen,
        row.seen_count, row.provider, row.source, row.source_tier,
        row.headline, row.body_text, row.url, row.synthetic,
    )
    assert len(row.to_row()) == len(fields(NewsRow))
    assert row.source_tier == "secondary"
    fed = _batch([_item(source="Federal Reserve")]).rows[0]
    assert fed.source_tier == "primary"


def test_empty_window_produces_a_valid_batch_not_an_exception():
    quiet = _batch(())
    assert quiet.rows == () and quiet.ticker_edges == ()
    assert quiet.returned == 0 and quiet.duplicates_collapsed == 0
    assert quiet.window_fingerprint == EMPTY_WINDOW_FINGERPRINT
    assert quiet.error is None
    assert quiet.universe == tuple(sorted(UNIVERSE))
    assert quiet.grounding_version == archive.GROUNDING_VERSION

    failed = _batch((), error="alpaca news request failed")
    # A failed fetch and a quiet wire are different facts: the error is carried
    # through, and it moves the fingerprint so a later, different failure is
    # still recorded as a change.
    assert failed.error == "alpaca news request failed"
    assert failed.window_fingerprint != EMPTY_WINDOW_FINGERPRINT
    assert _batch((), error="socket timeout").window_fingerprint not in (
        EMPTY_WINDOW_FINGERPRINT, failed.window_fingerprint)


# --------------------------------------------------------------------------
# the search seam
# --------------------------------------------------------------------------


def _report(**over) -> RelevanceReport:
    kwargs = dict(
        terms=("samsung",), universe=UNIVERSE, matched_total=0, page=(),
        single_secondary_total=0, synthetic_excluded=0, newest_published=None,
        archive_begins=None, providers_in_window=("alpaca",), as_of=AS_OF,
        now=AS_OF,
    )
    kwargs.update(over)
    return relevance_report(**kwargs)


def _said(report: RelevanceReport, fragment: str) -> bool:
    return any(fragment in line for line in report.not_established)


def test_relevance_report_out_of_universe_query_implicates_no_holding():
    report = _report(
        terms=("samsung",), matched_total=6,
        page=({"tickers": ["005930.KS"], "headline": "Samsung guides"},),
        archive_begins="2026-07-01T00:00:00+00:00")

    # Relevance is resolved against actual holdings, not asserted.
    assert report.in_universe_tickers == ()
    assert report.out_of_universe_terms == ("SAMSUNG",)
    assert _said(report, "SAMSUNG is not in the mandate universe")
    assert report.universe == tuple(sorted(UNIVERSE))


def test_relevance_report_can_never_carry_a_weight():
    report = _report(
        terms=("samsung", "surge"), matched_total=12, single_secondary_total=4,
        page=({"tickers": ["SPY", "QQQ"]}, {"tickers": "TLT"}),
        newest_published="2026-07-30T14:30:00+00:00",
        archive_begins="2026-07-01T00:00:00+00:00",
        providers_in_window=("alpaca", "rss"))

    banned = ("weight", "target", "allocation", "notional", "position",
              "score", "sentiment", "tone", "direction")
    names = {f.name for f in fields(RelevanceReport)} | set(report.to_dict())
    for word in banned:
        assert not any(word in name for name in names), word

    for line in report.not_established:
        for number in re.findall(r"\d*\.\d+", line):
            # A bare fraction in prose is a size waiting to be read as one.
            assert not 0 < float(number) <= 1, line
    assert report.in_universe_tickers == ("QQQ", "SPY", "TLT")


def test_relevance_report_below_floor_reports_insufficient_not_zero():
    thin = _report(matched_total=3, archive_begins="2026-07-01T00:00:00+00:00")
    assert thin.corroboration_value is None
    assert thin.corroboration_state == "insufficient"
    assert _said(thin, f"{MIN_ARCHIVE_ITEMS}-record floor")

    silent = _report(matched_total=0)
    assert silent.corroboration_value is None
    assert silent.corroboration_state == "no_window"

    enough = _report(matched_total=10, single_secondary_total=2,
                     archive_begins="2026-07-01T00:00:00+00:00")
    assert enough.corroboration_state == "ok"
    assert enough.corroboration_value == pytest.approx(0.8)
    assert _said(enough, "2 of 10 matched records are single-secondary-source")


def test_relevance_report_distinguishes_empty_archive_from_quiet_wire():
    no_rows = _report(matched_total=0, archive_begins=None)
    predates = _report(matched_total=0, as_of="2026-06-01T00:00:00+00:00",
                       archive_begins="2026-07-01T00:00:00+00:00")
    no_match = _report(matched_total=0,
                       archive_begins="2026-07-01T00:00:00+00:00")

    assert _said(no_rows, "holds no rows at all")
    assert _said(predates, "the archive begins at 2026-07-01T00:00:00+00:00")
    assert _said(no_match, "none match these terms")

    # Each case says something the other two do not; a replay of a
    # pre-archive decision must never read as a quiet market.
    marks = ("holds no rows at all", "this window predates it",
             "none match these terms")
    for report, mine in zip((no_rows, predates, no_match), marks):
        for mark in marks:
            assert _said(report, mark) is (mark == mine), (mine, mark)


def test_relevance_report_states_a_synthetic_only_window():
    # The default desk mode is synthetic/simulated, so a fresh desk archives
    # only fixture rows and every real search comes back empty.
    fixtures = _report(matched_total=0, synthetic_excluded=7,
                       archive_begins="2026-07-01T00:00:00+00:00")
    assert _said(fixtures, "only synthetic fixture rows")

    real = _report(matched_total=0, synthetic_excluded=0,
                   archive_begins="2026-07-01T00:00:00+00:00")
    assert not _said(real, "only synthetic fixture rows")


def test_relevance_report_states_rss_has_no_macro_lane():
    assert macro_lane_supported("alpaca") is True
    assert macro_lane_supported("rss") is False
    assert macro_lane_supported("") is False

    line = "absence here is not evidence of absence"
    assert _said(_report(providers_in_window=("rss",)), line)
    assert not _said(_report(providers_in_window=("alpaca",)), line)
    both = _report(providers_in_window=("alpaca", "rss"))
    assert _said(both, line)
    assert sum(line in s for s in both.not_established) == 1


def test_archive_lag_is_none_when_there_are_no_rows():
    empty = _report(newest_published=None)
    assert empty.archive_lag_hours is None
    assert not _said(empty, "the newest archived record")

    fresh = _report(newest_published="2026-07-30T22:00:00+00:00", now=AS_OF,
                    matched_total=6, archive_begins="2026-07-01T00:00:00+00:00")
    assert fresh.archive_lag_hours == pytest.approx(2.0)
    assert not _said(fresh, "the newest archived record")

    stale = _report(newest_published="2026-07-29T12:00:00+00:00", now=AS_OF,
                    matched_total=6, archive_begins="2026-07-01T00:00:00+00:00")
    assert stale.archive_lag_hours == pytest.approx(36.0)
    assert _said(stale, "the newest archived record is 36.0h old")


def test_relevance_report_always_states_no_price_evidence():
    price_line = "the archive holds no price data"
    rich = _report(terms=("spy", "inflows"), matched_total=40,
                   single_secondary_total=0,
                   page=({"tickers": ["SPY"]},),
                   newest_published="2026-07-30T23:30:00+00:00",
                   archive_begins="2026-01-01T00:00:00+00:00",
                   providers_in_window=("alpaca",))
    assert rich.corroboration_state == "ok"
    assert rich.in_universe_tickers == ("SPY",)
    # Even here — corroborated, in-universe, fresh — the record establishes
    # nothing about a price.
    assert _said(rich, price_line)
    assert _said(_report(), price_line)
    assert _said(_report(matched_total=0, archive_begins=None), price_line)


def test_normalise_terms_matches_the_search_column_normalisation():
    assert normalise_terms("Nestlé  SURGE ") == ("nestle", "surge")
    assert normalise_terms("") == ()
    assert normalise_terms("   ") == ()
    assert normalise_terms("!!! --- ,,,") == ()
    assert normalise_terms("U.S. rates, rates") == ("u", "s", "rates")


def test_relevance_report_refuses_inconsistent_aggregates():
    # The aggregates come from one query over the full match set; a pair that
    # cannot both be true means they were computed over different sets.
    with pytest.raises(ArchiveRejected):
        _report(matched_total=3, single_secondary_total=5)
    with pytest.raises(ArchiveRejected):
        _report(matched_total=-1)
    with pytest.raises(ArchiveRejected):
        _report(page=("not a mapping",))
