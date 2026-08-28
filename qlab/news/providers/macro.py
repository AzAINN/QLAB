"""Official releases as a primary source, and the calendar of what is ahead.

The publishers here ARE the events — BLS, BEA, EIA, Treasury — so every
record is primary-tier by name. The calendar is not news: a scheduled
release is a future-dated fact, and the look-ahead gate would drop it, and
rightly. It is served to Atlas as ``upcoming`` so the desk can say what is
coming without pretending it has happened.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from qlab.news.feed import NewsItem, _fetch_rss_feeds, load_news_sources


def fetch(as_of: datetime, universe: tuple[str, ...]) -> list[NewsItem]:
    feeds = (load_news_sources().get("macro") or {}).get("feeds") or []
    if not feeds:
        raise RuntimeError("the macro provider has no feeds configured under "
                           "news_sources.yaml: macro.feeds")
    items = _fetch_rss_feeds(feeds, as_of, universe)
    return [NewsItem(**{**item.__dict__, "provider": "macro"}) for item in items]


def upcoming(as_of: datetime, horizon_days: int = 14) -> list[dict]:
    """Scheduled releases ahead of ``as_of`` within the horizon, soonest first."""
    out = []
    for entry in load_news_sources().get("calendar") or []:
        when = datetime.fromisoformat(str(entry["when"]))
        if when.tzinfo is None:
            raise ValueError(f"calendar entry {entry['name']!r} needs a timezone")
        if as_of <= when <= as_of + timedelta(days=horizon_days):
            out.append({"name": entry["name"], "when": when.isoformat(),
                        "days_ahead": (when - as_of).days,
                        "tickers": list(entry.get("tickers", [])),
                        "source": entry.get("source", "")})
    return sorted(out, key=lambda e: e["when"])
