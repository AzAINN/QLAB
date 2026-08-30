"""The durable news record: rows the desk can still be asked about tomorrow.

Today a window is fetched, grounded, read and discarded. Nothing is kept, so
"what would have made Samsung surge this morning?" cannot be answered at all —
not answered badly, answered *not at all*, because there is no record to
consult. This module builds the rows that make the window durable, and the
prose that says what those rows do and do not establish.

Three boundaries are deliberate and are the whole point of the unit:

* **It is built from the RAW window, never from a grounded one.**
  :func:`qlab.news.grounding.ground` deletes every item whose tickers do not
  intersect the mandate universe — which is exactly the macro item a Samsung
  story arrives as. Grounding decides what may be *shown to an interpreter*;
  the archive decides what is *kept*. Feeding the archive from
  ``GroundedNews.items`` would make the headline question permanently
  unanswerable while the news drawer still displayed the story.
* **It is pure.** No clock, no network, no registry, no environment. The
  knowledge boundary (``first_seen``) is injected by the caller, which is what
  makes the whole unit testable with no desk and no database, and what makes a
  replay reproduce the archive it is replaying.
* **It cannot emit a number that could be read as an allocation.**
  :class:`RelevanceReport` carries states and prose. There is no weight, no
  target, no sentiment and no direction anywhere on it — a search seam that
  could name a size would be a return forecast reached through the news drawer.

Absence is never a value, in the same discipline as
:mod:`qlab.news.qualitative`: "no rows at all", "rows but none matching", "too
few to be a ratio" and "only synthetic fixtures" are four different facts and a
zero conflates them.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from typing import Mapping, Sequence

# One definition of "canonical", reused rather than reimplemented: these are
# the exact functions that stamped every timestamp the feed ever emitted. A
# second implementation here would drift and the drift would be invisible.
from qlab.news.feed import NewsItem, _as_datetime, _iso_timestamp
from qlab.news.grounding import content_hash, source_tier
from qlab.news.qualitative import INSUFFICIENT, MIN_SIGNAL_ITEMS, NO_WINDOW, OK

# Claims are a deterministic recomputation over stored items, so they are not
# persisted. This is stamped on every batch instead, so a replay across a
# change to grounding's clusterer (_NOISE, _WORD, min_overlap, _cluster) can
# refuse loudly rather than silently compare claims from two different rules.
GROUNDING_VERSION = "1"

# The archive uses the desk's existing floor, imported rather than repeated: a
# ratio over three records is one story rounded to two decimal places.
MIN_ARCHIVE_ITEMS = MIN_SIGNAL_ITEMS

# Providers that keep untagged items. Alpaca is queried both symbol-filtered
# and unfiltered (feed.py:_fetch_alpaca), so macro stories reach the archive.
# The rss provider drops every untagged entry inside the fetch itself
# (feed.py:_fetch_rss, `if not tickers: continue`) — one layer earlier than
# ground() — so under rss the macro lane does not exist and silence in the
# archive is not evidence of silence on the wire. The edgar provider tags every
# filing with the fund it was read for, so it never uses the lane — it is named
# here so a future untagged filing record is kept rather than silently dropped.
# The macro provider shares that rss parser, so it too drops untagged entries
# one layer early today; it is named for edgar's reason — an official release
# naming no holding is macro context, and must not be dropped as noise. The
# gdelt provider tags every article with the rule's tickers, so it is named on
# the same terms: a future untagged article is press coverage, not noise.
MACRO_LANE_PROVIDERS = frozenset({"alpaca", "edgar", "gdelt", "macro"})

EMPTY_WINDOW_FINGERPRINT = "empty"

SYNTHETIC_PROVIDER = "synthetic"

CANONICAL_SHAPE = "YYYY-MM-DDTHH:MM:SS+00:00"

# The schema CHECK is `LIKE '____-__-__T__:__:__+00:00'`. This narrows each
# `_` to a digit, which is what the CHECK is actually for: every point-in-time
# read is a lexicographic string compare, and only digits make lexicographic
# order equal chronological order.
_CANONICAL = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")

# Unicode alphanumerics, underscore excluded: the token form the generated
# search column holds after lower(strip_accents(...)).
_TERM = re.compile(r"[^\W_]+", re.UNICODE)

NO_PRICE_EVIDENCE = "the archive holds no price data"


class ArchiveRejected(ValueError):
    """A record the archive refuses to store, naming what is wrong with it."""


def _strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(ch)
    )


def _norm_ticker(value) -> str:
    return str(value or "").strip().upper()


def is_canonical(value) -> bool:
    """Whether ``value`` is a timestamp the archive schema would accept."""
    return isinstance(value, str) and bool(_CANONICAL.match(value))


def canonical_timestamp(value: str | datetime) -> str:
    """Canonicalise to the one shape the archive stores.

    Identical to what every ``NewsItem.published`` already carries. Callers
    holding a clock reading must pass it through here: ``datetime.now(utc)
    .isoformat()`` carries microseconds and is *not* canonical.
    """
    try:
        return _iso_timestamp(_as_datetime(value))
    except (TypeError, ValueError) as exc:
        raise ArchiveRejected(
            f"cannot canonicalise {value!r} to {CANONICAL_SHAPE} ({exc})"
        ) from exc


def _require_canonical(field: str, value, *, context: str = "") -> str:
    text = "" if value is None else str(value)
    where = f" ({context})" if context else ""
    if not text.strip():
        raise ArchiveRejected(
            f"{field} is empty{where}; the archive stores no undated record. "
            f"Expected {CANONICAL_SHAPE}"
        )
    if not is_canonical(text):
        raise ArchiveRejected(
            f"{field}={text!r} is not canonical{where}; expected "
            f"{CANONICAL_SHAPE} (e.g. 2026-07-31T09:30:00+00:00). "
            f"Wrap it in archive.canonical_timestamp()"
        )
    return text


def macro_lane_supported(provider: str) -> bool:
    """Whether ``provider`` archives stories that name no holding."""
    return str(provider or "").strip().lower() in MACRO_LANE_PROVIDERS


# Question words and connectives. A natural-language question is mostly these,
# and without stripping them every one becomes a "term" the relevance report
# then reports as absent from the universe — "WHAT is not in the mandate
# universe" ahead of the actual answer. They are also useless as search terms:
# "what" matches most of the archive and narrows nothing.
_STOPWORDS = frozenset({
    "a", "об", "an", "and", "are", "as", "at", "be", "been", "but", "by", "can",
    "could", "did", "do", "does", "for", "from", "had", "has", "have", "how",
    "in", "is", "it", "its", "made", "make", "may", "might", "of", "on", "or",
    "should", "that", "the", "their", "then", "there", "these", "this", "to",
    "was", "were", "what", "when", "where", "which", "who", "why", "will",
    "with", "would", "you", "your",
})


def normalise_terms(query: str) -> tuple[str, ...]:
    """Split a query into the token form the search column holds.

    Must match ``lower(strip_accents(headline || ' ' || coalesce(body_text,
    '')))`` exactly: a query normalised differently from the column silently
    matches nothing, and an empty result set reads as an empty record.
    """
    seen: set[str] = set()
    out: list[str] = []
    for token in _TERM.findall(_strip_accents(str(query or "")).lower()):
        if token and token not in seen and token not in _STOPWORDS:
            seen.add(token)
            out.append(token)
    # If a question is nothing but stopwords there are no terms, which is
    # honest: it is a query with no subject, not a query that matched nothing.
    return tuple(out)


def window_fingerprint(
    items: Sequence[NewsItem], *, provider: str, error: str | None
) -> str:
    """Identity of one fetched window, order-independent, error included.

    The error channel is part of the identity on purpose: a fetch that fails
    twice with two different errors is two different facts, and a fingerprint
    over content alone would record the second as "no change".
    """
    hashes = sorted({content_hash(item) for item in items or ()})
    if not hashes and not error:
        # A quiet wire, distinguishable from a failed fetch by construction.
        return EMPTY_WINDOW_FINGERPRINT
    material = "|".join((str(provider or ""), str(error or ""), *hashes))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class NewsRow:
    """One archived record. Field order is the schema's column order."""

    item_hash: str
    published: str
    first_seen: str
    last_seen: str
    seen_count: int
    provider: str
    source: str
    source_tier: str
    headline: str
    # Never named `summary`: Registry._rows json.loads any column literally
    # called summary, so a body of "2024" would come back as int 2024 and a
    # body of "null" as None.
    body_text: str | None
    url: str | None
    synthetic: bool

    def to_row(self) -> tuple:
        return tuple(getattr(self, f.name) for f in fields(self))

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TickerEdge:
    """One sighting of a symbol in a record.

    Separate from the row because ``content_hash`` does not cover tickers while
    the mandate universe does change: the same story re-fetched after a
    universe change maps to a different ticker set, and a JSON column with
    last-write-wins would erase the earlier mapping. Sightings union.
    """

    item_hash: str
    ticker: str
    in_universe: bool

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ArchiveBatch:
    """One archive pass: what to store, and what the pass itself was."""

    rows: tuple[NewsRow, ...]
    ticker_edges: tuple[TickerEdge, ...]
    window_fingerprint: str
    provider: str
    offline: bool
    as_of: str
    lookback_hours: int
    universe: tuple[str, ...]
    returned: int
    duplicates_collapsed: int
    grounding_version: str
    error: str | None

    def to_dict(self) -> dict:
        return {
            "rows": [r.to_dict() for r in self.rows],
            "ticker_edges": [e.to_dict() for e in self.ticker_edges],
            "stored": len(self.rows),
            "window_fingerprint": self.window_fingerprint,
            "provider": self.provider,
            "offline": self.offline,
            "as_of": self.as_of,
            "lookback_hours": self.lookback_hours,
            "universe": list(self.universe),
            "returned": self.returned,
            "duplicates_collapsed": self.duplicates_collapsed,
            "grounding_version": self.grounding_version,
            "error": self.error,
        }


def build_archive_batch(
    items: Sequence[NewsItem],
    *,
    provider: str,
    offline: bool,
    as_of: str,
    lookback_hours: int,
    universe: Sequence[str],
    first_seen: str,
    error: str | None = None,
) -> ArchiveBatch:
    """Turn a raw fetched window into rows the registry can insert.

    ``items`` is the RAW window from :func:`qlab.news.feed.fetch_news`, never
    ``GroundedNews.items``. ``first_seen`` is the caller's clock reading, in
    canonical form, and is the knowledge boundary: when the desk first knew.
    """
    if not str(provider or "").strip():
        raise ArchiveRejected("provider is required; an unattributed archive "
                              "pass cannot be replayed")
    if isinstance(lookback_hours, bool) or not isinstance(lookback_hours, int):
        raise ArchiveRejected("lookback_hours must be an integer")
    if lookback_hours < 0:
        raise ArchiveRejected("lookback_hours must be non-negative")

    # Materialised once: the window is walked three times below and a
    # generator would leave the fingerprint hashing an exhausted iterator.
    records = list(items or ())
    provider_name = str(provider).strip()
    seen_at = _require_canonical("first_seen", first_seen)
    # as_of is normalised rather than refused: fetch_news already accepts a
    # date here and resolves it to midnight UTC, and the batch must record the
    # same instant the fetch was bounded by.
    window_as_of = canonical_timestamp(as_of)
    universe_t = tuple(sorted({
        _norm_ticker(t) for t in (universe or ()) if _norm_ticker(t)
    }))
    universe_set = set(universe_t)
    synthetic = provider_name == SYNTHETIC_PROVIDER

    rows: list[NewsRow] = []
    edges: list[TickerEdge] = []
    by_hash: dict[str, int] = {}
    edge_keys: set[tuple[str, str]] = set()
    duplicates = 0

    for index, item in enumerate(records):
        item_provider = str(getattr(item, "provider", "") or "")
        if item_provider and item_provider != provider_name:
            # A window is one fetch from one provider; a mixed batch would make
            # the synthetic flag a per-row guess instead of a stored fact.
            raise ArchiveRejected(
                f"item {index} carries provider {item_provider!r} but the "
                f"batch is {provider_name!r}"
            )
        item_hash = content_hash(item)
        published = _require_canonical(
            "published", getattr(item, "published", ""),
            context=f"item {index} {str(getattr(item, 'headline', ''))[:60]!r}")

        if item_hash in by_hash:
            # First occurrence wins so the batch is deterministic; the
            # registry's seen_count arithmetic is then exactly right rather
            # than incidentally right.
            duplicates += 1
        else:
            by_hash[item_hash] = len(rows)
            source = str(getattr(item, "source", "") or "")
            summary = str(getattr(item, "summary", "") or "").strip()
            url = str(getattr(item, "url", "") or "").strip()
            rows.append(NewsRow(
                item_hash=item_hash,
                published=published,
                first_seen=seen_at,
                # A fresh batch has been seen once, now. The registry advances
                # last_seen and seen_count on conflict; first_seen never moves.
                last_seen=seen_at,
                # "archive passes that returned this item" — nothing more. It
                # cannot detect a provider replay: identical text produces the
                # identical hash and edited text produces a new row, so the
                # count cannot tell those two apart.
                seen_count=1,
                provider=provider_name,
                source=source,
                # Frozen at write: the tier rule may change, but what the desk
                # believed about this publisher when it stored the row may not.
                source_tier=source_tier(source),
                headline=str(getattr(item, "headline", "") or ""),
                # An empty string is not a value.
                body_text=summary or None,
                url=url or None,
                synthetic=synthetic,
            ))

        # Edges are unioned even for a collapsed duplicate: the row is the same
        # record, but the two copies may map to different symbols.
        raw_extra = tuple(getattr(item, "raw_tickers", ()) or ())
        for ticker in tuple(getattr(item, "tickers", ()) or ()) + raw_extra:
            symbol = _norm_ticker(ticker)
            if not symbol or (item_hash, symbol) in edge_keys:
                continue
            edge_keys.add((item_hash, symbol))
            # Computed at write time against the universe passed in. An empty
            # universe is legal and makes every sighting out-of-universe; a
            # symbol recalled by entity extraction but never held is recorded,
            # not dropped, so recall degrades visibly instead of silently.
            edges.append(TickerEdge(item_hash, symbol, symbol in universe_set))

    return ArchiveBatch(
        rows=tuple(rows),
        ticker_edges=tuple(edges),
        window_fingerprint=window_fingerprint(
            records, provider=provider_name, error=error),
        provider=provider_name,
        offline=bool(offline),
        as_of=window_as_of,
        lookback_hours=int(lookback_hours),
        universe=universe_t,
        returned=len(records),
        duplicates_collapsed=duplicates,
        grounding_version=GROUNDING_VERSION,
        # A failed fetch and a quiet wire are different facts; both are
        # recordable, and an error never suppresses the rows that did arrive.
        error=str(error) if error else None,
    )


@dataclass(frozen=True)
class RelevanceReport:
    """What a search over the archive does — and does not — establish.

    Every field here is a state, a name or a count. There is deliberately no
    weight, target, allocation, notional, position, score, sentiment, tone or
    direction: the reasoner may cite this record, and must not be able to reach
    a size through it.
    """

    in_universe_tickers: tuple[str, ...]
    out_of_universe_terms: tuple[str, ...]
    universe: tuple[str, ...]
    corroboration_value: float | None
    corroboration_state: str
    archive_lag_hours: float | None
    not_established: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "in_universe_tickers": list(self.in_universe_tickers),
            "out_of_universe_terms": list(self.out_of_universe_terms),
            "universe": list(self.universe),
            "corroboration_value": self.corroboration_value,
            "corroboration_state": self.corroboration_state,
            "archive_lag_hours": self.archive_lag_hours,
            "not_established": list(self.not_established),
        }


def _page_tickers(page: Sequence[Mapping]) -> set[str]:
    found: set[str] = set()
    for row in page or ():
        if not isinstance(row, Mapping):
            raise ArchiveRejected(
                "page rows must be mappings from the search result set")
        raw = row.get("tickers")
        if raw is None:
            raw = row.get("ticker") or ()
        if isinstance(raw, str):
            raw = [part for part in re.split(r"[,\s]+", raw) if part]
        for ticker in raw or ():
            symbol = _norm_ticker(ticker)
            if symbol:
                found.add(symbol)
    return found


def relevance_report(
    *,
    terms: Sequence[str],
    universe: Sequence[str],
    matched_total: int,
    page: Sequence[Mapping],
    single_secondary_total: int,
    synthetic_excluded: int,
    newest_published: str | None,
    archive_begins: str | None,
    providers_in_window: Sequence[str],
    as_of: str,
    now: str,
) -> RelevanceReport:
    """Resolve a query against the record and state what it cannot support.

    The aggregate arguments (``matched_total``, ``single_secondary_total``,
    ``synthetic_excluded``, ``newest_published``, ``archive_begins``) are
    computed by the caller over the FULL match set, never over ``page``: a
    statistic that changes with ``offset`` is not a fact about the record.
    ``page`` is used only to name the in-universe holdings actually present.
    """
    for label, count in (("matched_total", matched_total),
                         ("single_secondary_total", single_secondary_total),
                         ("synthetic_excluded", synthetic_excluded)):
        if isinstance(count, bool) or not isinstance(count, int):
            raise ArchiveRejected(f"{label} must be an integer")
        if count < 0:
            raise ArchiveRejected(f"{label} must be non-negative")
    if single_secondary_total > matched_total:
        raise ArchiveRejected(
            f"single_secondary_total ({single_secondary_total}) exceeds "
            f"matched_total ({matched_total}); the aggregates were computed "
            f"over different sets")

    universe_t = tuple(sorted({
        _norm_ticker(t) for t in (universe or ()) if _norm_ticker(t)
    }))
    # Idempotent for already-normalised input, and the only way the terms the
    # report names are the terms the SQL actually matched on.
    query_terms = normalise_terms(" ".join(str(t) for t in (terms or ())))
    as_of_c = canonical_timestamp(as_of)
    now_c = canonical_timestamp(now)
    newest = canonical_timestamp(newest_published) if newest_published else None
    begins = canonical_timestamp(archive_begins) if archive_begins else None
    providers = tuple(sorted({
        str(p).strip().lower() for p in (providers_in_window or ()) if str(p).strip()
    }))

    in_universe = tuple(sorted(_page_tickers(page) & set(universe_t)))
    out_of_universe = tuple(
        t.upper() for t in query_terms if t.upper() not in set(universe_t))

    if matched_total == 0:
        value, state = None, NO_WINDOW
    elif matched_total < MIN_ARCHIVE_ITEMS:
        # None, never 0.0: too few records to be a ratio is not "nothing is
        # corroborated".
        value, state = None, INSUFFICIENT
    else:
        value, state = (
            (matched_total - single_secondary_total) / matched_total, OK)

    lag = (
        None if newest is None
        else (_as_datetime(now_c) - _as_datetime(newest)).total_seconds() / 3600.0
    )

    said: list[str] = []
    # (1) Unconditional. The archive stores text and nothing else; "what would
    # have made X surge" must never be answered as though a record of a move
    # were in evidence here.
    said.append(f"{NO_PRICE_EVIDENCE}; nothing in these records establishes "
                f"that any price moved")
    # (2) Relevance is resolved against actual holdings, never asserted.
    for term in out_of_universe:
        said.append(f"{term} is not in the mandate universe, so no holding is "
                    f"directly implicated")
    if matched_total == 0:
        # (3) The default desk mode is synthetic/simulated, so a fresh desk
        # archives only fixture rows and every real search returns empty.
        # Without this line the reasoner narrates that as "the record is
        # silent", which is a claim about the world rather than the fixture.
        if synthetic_excluded > 0:
            said.append(
                f"the archive holds only synthetic fixture rows for this "
                f"window ({synthetic_excluded}); nothing here is a real record")
        if begins is None:
            # (4) No rows at all.
            said.append("the archive holds no rows at all; this is a gap in "
                        "the record, not a quiet wire")
        elif as_of_c >= begins:
            # (4b) Rows exist and cover the window, but none match. Named
            # separately so a pre-archive replay and a genuinely silent wire
            # never read the same.
            said.append("the archive holds records covering this window but "
                        "none match these terms; the record is silent on them, "
                        "which is not evidence that nothing happened")
    if begins is not None and as_of_c < begins:
        # (5)
        said.append(f"the archive begins at {begins}; this window predates it")
    if 0 < matched_total < MIN_ARCHIVE_ITEMS:
        # (6)
        said.append(f"{matched_total} matched record(s) is below the "
                    f"{MIN_ARCHIVE_ITEMS}-record floor; no ratio is reported")
    if matched_total >= MIN_ARCHIVE_ITEMS and single_secondary_total > 0:
        # (7) One outlet is a rumour; the count is named rather than folded
        # into the ratio alone.
        said.append(f"{single_secondary_total} of {matched_total} matched "
                    f"records are single-secondary-source")
    for provider in providers:
        # (8)
        if not macro_lane_supported(provider):
            said.append(
                f"this window was fetched via {provider}, which stores only "
                f"universe-tagged stories; absence here is not evidence of "
                f"absence")
    if lag is not None and lag > 6:
        # (9)
        said.append(f"the newest archived record is {lag:.1f}h old")

    return RelevanceReport(
        in_universe_tickers=in_universe,
        out_of_universe_terms=out_of_universe,
        universe=universe_t,
        corroboration_value=value,
        corroboration_state=state,
        # None when the archive holds no rows: absence is never zero.
        archive_lag_hours=lag,
        not_established=tuple(said),
    )
