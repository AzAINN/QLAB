"""Official releases as a primary source, and the calendar of what is ahead.

The publishers here ARE the events — whichever official sources
``news_sources.yaml`` lists under ``macro.feeds`` — so every record is
primary-tier by name. The shipped list is BEA alone: BLS and the US Treasury
were removed on 2026-08-28, dead at 403 and 404, and nothing was named to
replace them that had been verified live. The calendar is not news: a scheduled
release is a future-dated fact, and the look-ahead gate would drop it, and
rightly. It is served to Atlas as ``upcoming`` so the desk can say what is
coming without pretending it has happened.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from qlab.news.feed import NewsItem, _fetch_rss_feeds, load_news_sources


def fetch(as_of: datetime, universe: tuple[str, ...]) -> list[NewsItem]:
    feeds = (load_news_sources().get("macro") or {}).get("feeds") or []
    if not feeds:
        raise RuntimeError("the macro provider has no feeds configured under "
                           "news_sources.yaml: macro.feeds")
    items = _fetch_rss_feeds(feeds, as_of, universe)
    return [replace(item, provider="macro") for item in items]


def calendar_days_left(as_of: datetime) -> int | None:
    """Whole days from ``as_of`` to the last dated entry, or ``None``.

    ``upcoming`` refuses only once the calendar is ALREADY exhausted, which is
    the first day it is too late to hear about. This is the same fact one day
    earlier, so a status line can show the file running out while there is
    still time to extend it. ``None`` is "no life left" -- empty or expired --
    never a negative number dressed up as a countdown.
    """
    if as_of.tzinfo is None:
        raise ValueError("calendar_days_left() needs a timezone-aware as_of; a "
                         "naive datetime has no instant to count from")
    as_of = as_of.astimezone(timezone.utc)
    latest: datetime | None = None
    for entry in load_news_sources().get("calendar") or []:
        when = datetime.fromisoformat(str(entry["when"]))
        if when.tzinfo is None:
            raise ValueError(f"calendar entry {entry['name']!r} needs a timezone")
        when = when.astimezone(timezone.utc)
        latest = when if latest is None else max(latest, when)
    if latest is None or latest < as_of:
        return None
    return (latest - as_of).days


def upcoming(as_of: datetime, horizon_days: int = 14) -> list[dict]:
    """Scheduled releases in ``(as_of, as_of + horizon_days]``, soonest first.

    Every instant is normalised to UTC before it is compared or ordered: an
    entry only has to be tz-aware, so ordering the ISO text alone would put a
    ``-04:00`` release in the wrong place with nothing to say it had.

    Refuses loudly once the hand-maintained calendar runs out, so an empty
    result is a fact about the fortnight and never about who last edited the
    yaml.
    """
    if as_of.tzinfo is None:
        raise ValueError("upcoming() needs a timezone-aware as_of; a naive "
                         "datetime has no instant to compare releases against")
    as_of = as_of.astimezone(timezone.utc)

    ahead: list[tuple[datetime, dict]] = []
    latest: datetime | None = None
    for entry in load_news_sources().get("calendar") or []:
        when = datetime.fromisoformat(str(entry["when"]))
        if when.tzinfo is None:
            raise ValueError(f"calendar entry {entry['name']!r} needs a timezone")
        when = when.astimezone(timezone.utc)
        # max over ALL entries: the yaml is hand-maintained and need not be
        # sorted, so the last element is not the last date.
        latest = when if latest is None else max(latest, when)
        if as_of < when <= as_of + timedelta(days=horizon_days):
            ahead.append((when, {"name": entry["name"], "when": when.isoformat(),
                                 "days_ahead": (when - as_of).days,
                                 "tickers": list(entry.get("tickers", [])),
                                 "source": entry.get("source", "")}))
    if latest is None or latest < as_of:
        raise RuntimeError(
            "the release calendar is exhausted: its last entry is dated "
            + ("nothing at all — it is empty" if latest is None
               else latest.isoformat())
            + f", before {as_of.isoformat()}. Extend configs/"
            "news_sources.yaml `calendar:` — an empty look-ahead must mean a "
            "quiet fortnight, not a stale file.")
    return [payload for _, payload in sorted(ahead, key=lambda pair: pair[0])]
