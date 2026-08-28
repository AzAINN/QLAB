"""GDELT as the many-publisher secondary source.

Its job is independence: ``Claim.corroborated`` needs two distinct
publishers for a secondary story, and one wire cannot supply that. Each
article's domain is its ``source``, so two outlets on one story count as
two. Keyword rules per ticker come from ``news_sources.yaml`` — shared as
data, like every other source list.

Point-in-time by ``seendate``, which GDELT stamps in UTC: the instant is
carried into an offset-aware ISO string so the caller's look-ahead gate
compares instants rather than text.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from qlab.news.feed import NewsItem, _validate_gdelt_rules, load_news_sources

_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
_TIMEOUT_S = 10
# The request window. `fetch_news` trims every record to the caller's own
# lookback afterwards; this only bounds what is asked for.
_LOOKBACK = timedelta(hours=48)


def fetch(as_of: datetime, universe: tuple[str, ...]) -> list[NewsItem]:
    rules = (load_news_sources().get("gdelt") or {}).get("rules")
    # Refuses an unconfigured or half-written section by name rather than
    # reading as a quiet press: an empty result must be a fact about the
    # coverage, never about who last edited the yaml.
    _validate_gdelt_rules(rules)
    wanted = {t.upper() for t in universe}
    dated: list[tuple[datetime, NewsItem]] = []
    for rule in rules:
        tickers = tuple(t for t in rule["tickers"] if t.upper() in wanted)
        if not tickers:
            continue
        query = f"({rule['query']}) sourcelang:english"
        # `safe=":"` keeps the operator colon literal — GDELT's query grammar
        # is read as text, and a percent-encoded `sourcelang%3Aenglish` is not
        # the filter this desk asked for.
        params = urllib.parse.urlencode({
            "query": query, "mode": "artlist", "format": "json",
            "maxrecords": 75, "timespan": "48h", "sort": "datedesc"}, safe=":")
        request = urllib.request.Request(
            f"{_DOC_URL}?{params}",
            # User-Agent only. urlopen does not decode content encodings, so
            # negotiating gzip would hand compressed bytes to json.loads.
            headers={"User-Agent": "qlab-news/0.1 (+https://github.com/qlab)"})
        response = urllib.request.urlopen(request, timeout=_TIMEOUT_S)
        try:
            payload = json.loads(response.read())
        finally:
            response.close()
        for art in payload.get("articles", []):
            # GDELT indexes the world's press; the desk reads one language,
            # and an untranslated headline is not evidence it can weigh.
            if str(art.get("language", "")).lower() != "english":
                continue
            seen = datetime.strptime(
                art["seendate"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            if seen >= as_of or seen < as_of - _LOOKBACK:
                continue
            dated.append((seen, NewsItem(
                source=str(art.get("domain") or "gdelt"),
                published=seen.isoformat(),
                headline=str(art.get("title") or "").strip(),
                summary="",
                url=str(art.get("url") or ""),
                tickers=tickers,
                provider="gdelt",
            )))
    # Newest first, so a window truncated downstream keeps the freshest
    # coverage rather than whichever rule happened to be listed first.
    dated.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in dated]
