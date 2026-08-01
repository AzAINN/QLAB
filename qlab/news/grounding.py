"""What makes a news record *grounded* rather than just fetched.

Fetching text is easy. Making it evidence a governed desk may reason from means
four properties the raw feed does not give you:

* **Attributable** — publisher, URL, and publication timestamp on every record.
* **Point-in-time** — provably published before the decision that used it, so a
  replay cannot quietly consume its own future.
* **Immutable** — the text that was scored is the text that is kept. Publishers
  edit headlines; a content hash means an edit is a *new* record, never a
  silent rewrite of the one already reasoned over.
* **Corroborated** — one outlet is a rumour, three independent outlets are an
  event. These deserve different weight and must be distinguishable.

This module supplies the last three. It deliberately does not interpret: it
decides what may be *shown* to an interpreter, and how much independent support
each claim has.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field

# Words too common to carry meaning when matching two headlines about the same
# event. Kept small and explicit rather than a stopword library, so the
# corroboration rule stays auditable.
_NOISE = frozenset({
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are",
    "as", "at", "by", "with", "from", "after", "over", "amid", "its", "it",
    "that", "this", "says", "said", "new", "up", "down", "us", "u.s.",
})

_WORD = re.compile(r"[a-z0-9][a-z0-9'\-\.]*")

# Publishers that ARE the event rather than reporting it. A central bank
# announcing its own rate decision, or a company filing an 8-K, needs no
# corroboration — asking three outlets to confirm a primary document gets the
# epistemics backwards. Everything else is secondary and must be corroborated.
PRIMARY_SOURCE_MARKERS = (
    "federal reserve", "european central bank", "ecb", "bank of england",
    "bank of japan", "treasury", "sec", "edgar", "eia", "bls",
    "bureau of labor", "census", "imf", "world bank", "opec",
)


def source_tier(source: str) -> str:
    """``primary`` when the publisher is the event, else ``secondary``."""
    name = str(source or "").strip().lower()
    return ("primary"
            if any(marker in name for marker in PRIMARY_SOURCE_MARKERS)
            else "secondary")


def content_hash(item) -> str:
    """Immutable identity of a news record.

    Covers publisher, URL, timestamp, and the text itself. If a publisher edits
    a headline the hash changes, so the edit surfaces as a distinct record
    rather than silently replacing the evidence already reasoned over.
    """
    material = "|".join((
        str(getattr(item, "source", "")),
        str(getattr(item, "url", "")),
        str(getattr(item, "published", "")),
        str(getattr(item, "headline", "")),
        str(getattr(item, "summary", "")),
    ))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _keywords(item) -> set[str]:
    text = f"{getattr(item, 'headline', '')} {getattr(item, 'summary', '')}".lower()
    return {w for w in _WORD.findall(text) if w not in _NOISE and len(w) > 2}


@dataclass(frozen=True)
class Claim:
    """A story, and how much independent support it has."""

    key: str
    headline: str
    tickers: tuple[str, ...]
    sources: tuple[str, ...]
    item_hashes: tuple[str, ...]
    corroboration: int              # distinct independent publishers
    earliest_published: str
    tier: str = "secondary"         # primary = the publisher IS the event

    @property
    def corroborated(self) -> bool:
        """Whether this claim is supported well enough to treat as an event.

        A primary source stands alone — a central bank's own announcement is
        the event, not a report of it. A secondary claim needs two independent
        publishers before it stops being one outlet's take.
        """
        return self.tier == "primary" or self.corroboration >= 2

    @property
    def support(self) -> str:
        if self.tier == "primary":
            return "primary source"
        if self.corroboration >= 2:
            return f"{self.corroboration} independent publishers"
        return "single secondary source"

    def to_dict(self) -> dict:
        return {
            "key": self.key, "headline": self.headline,
            "tickers": list(self.tickers), "sources": list(self.sources),
            "item_hashes": list(self.item_hashes),
            "corroboration": self.corroboration,
            "corroborated": self.corroborated,
            "tier": self.tier,
            "support": self.support,
            "earliest_published": self.earliest_published,
        }


@dataclass(frozen=True)
class GroundedNews:
    """A fetched window, made auditable."""

    as_of: str
    provider: str
    items: list                              # the raw NewsItem records
    hashes: list[str]
    claims: list[Claim]
    dropped_future: int = 0
    dropped_untagged: int = 0
    window_hash: str = ""
    quality_flags: list[str] = field(default_factory=list)

    @property
    def corroborated_claims(self) -> list[Claim]:
        return [c for c in self.claims if c.corroborated]

    def to_dict(self) -> dict:
        return {
            "as_of": self.as_of,
            "provider": self.provider,
            "item_count": len(self.items),
            "hashes": list(self.hashes),
            "claims": [c.to_dict() for c in self.claims],
            "corroborated_count": len(self.corroborated_claims),
            "dropped_future": self.dropped_future,
            "dropped_untagged": self.dropped_untagged,
            "window_hash": self.window_hash,
            "quality_flags": list(self.quality_flags),
        }


def ground(items, *, as_of: str, provider: str,
           universe: list[str] | None = None,
           min_overlap: int = 3) -> GroundedNews:
    """Turn fetched records into an auditable, point-in-time evidence window.

    Enforces the look-ahead boundary (anything published at/after ``as_of`` is
    dropped and counted), hashes each record for immutability, and groups
    records describing the same story so corroboration is visible.
    """
    universe_set = set(universe or [])
    kept, hashes = [], []
    dropped_future = dropped_untagged = 0

    for item in list(items or []):
        published = str(getattr(item, "published", ""))
        # Point-in-time: a record published at or after the decision instant is
        # the query's own future and must never enter the window.
        # No same-day exemption. `published.startswith(as_of[:10])` let anything
        # sharing the calendar date through, so an item published at 23:59
        # entered a window whose as_of was 12:00 — a twelve-hour look-ahead.
        # Invisible while compose_desk_read passes "now", and fatal the moment
        # anything replays an intraday point in time against the archive.
        if published and published >= as_of:
            dropped_future += 1
            continue
        tickers = tuple(getattr(item, "tickers", ()) or ())
        if universe_set and not (set(tickers) & universe_set):
            dropped_untagged += 1
            continue
        kept.append(item)
        hashes.append(content_hash(item))

    claims = _cluster(kept, hashes, min_overlap=min_overlap)
    window_hash = hashlib.sha256(
        "|".join(sorted(hashes)).encode("utf-8")).hexdigest()[:16]

    flags: list[str] = []
    if not kept:
        flags.append("empty window")
    if dropped_future:
        flags.append(f"{dropped_future} record(s) dropped as look-ahead")
    single_source = {getattr(i, "source", "") for i in kept}
    if (len(kept) >= 3 and len(single_source) == 1
            and source_tier(next(iter(single_source))) == "secondary"):
        # Only a concern for secondary outlets: a window of nothing but ECB
        # releases is single-source by nature and perfectly well grounded.
        flags.append(
            f"all {len(kept)} records come from one secondary publisher "
            f"({next(iter(single_source))}); nothing here is corroborated")

    return GroundedNews(
        as_of=as_of, provider=provider, items=kept, hashes=hashes,
        claims=claims, dropped_future=dropped_future,
        dropped_untagged=dropped_untagged, window_hash=window_hash,
        quality_flags=flags)


def _cluster(items, hashes, *, min_overlap: int) -> list[Claim]:
    """Group records that describe the same story.

    Deliberately crude and transparent: shared significant keywords. A cluster
    spanning several publishers is corroboration; a big cluster from one
    publisher is that publisher repeating itself, and is counted as one.
    """
    groups: list[dict] = []
    for item, item_hash in zip(items, hashes):
        words = _keywords(item)
        placed = False
        for group in groups:
            if len(words & group["words"]) >= min_overlap:
                group["items"].append(item)
                group["hashes"].append(item_hash)
                group["words"] |= words
                placed = True
                break
        if not placed:
            groups.append({"items": [item], "hashes": [item_hash],
                           "words": set(words)})

    claims: list[Claim] = []
    for group in groups:
        members = group["items"]
        # Corroboration counts DISTINCT publishers, never article count: one
        # outlet running five follow-ups is not five confirmations.
        sources = sorted({str(getattr(m, "source", "")) for m in members})
        tickers = sorted({t for m in members
                          for t in (getattr(m, "tickers", ()) or ())})
        published = sorted(str(getattr(m, "published", "")) for m in members)
        # One primary publisher in the cluster makes the whole claim primary:
        # a secondary outlet echoing a Fed release does not weaken the release.
        tier = ("primary" if any(source_tier(s) == "primary" for s in sources)
                else "secondary")
        claims.append(Claim(
            key=hashlib.sha256(
                "|".join(sorted(group["hashes"])).encode()).hexdigest()[:12],
            headline=str(getattr(members[0], "headline", ""))[:200],
            tickers=tuple(tickers),
            sources=tuple(sources),
            item_hashes=tuple(group["hashes"]),
            corroboration=len(sources),
            earliest_published=published[0] if published else "",
            tier=tier,
        ))
    # Primary sources rank above corroborated secondary ones.
    claims.sort(key=lambda c: (c.tier != "primary", -c.corroboration,
                               c.earliest_published))
    return claims


def by_ticker(grounded: GroundedNews) -> dict[str, list[Claim]]:
    """Claims indexed by the ticker they concern."""
    index: dict[str, list[Claim]] = defaultdict(list)
    for claim in grounded.claims:
        for ticker in claim.tickers:
            index[ticker].append(claim)
    return dict(index)
