"""GDELT as the many-publisher secondary source.

Its job is independence: ``Claim.corroborated`` needs two distinct
publishers for a secondary story, and one wire cannot supply that. Each
article's domain is its ``source``, so two outlets on one story count as
two. Keyword rules per ticker come from ``news_sources.yaml`` — shared as
data, like every other source list.

Point-in-time by ``seendate``, which GDELT stamps in UTC: the instant is
carried into an offset-aware ISO string so the caller's look-ahead gate
compares instants rather than text. The *request* is point-in-time too — an
explicit ``startdatetime``/``enddatetime`` derived from the caller's ``as_of``,
never a wall-clock ``timespan``.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from qlab.news.feed import NewsItem, _validate_gdelt_rules, load_news_sources

_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
# Measured 2026-08-28: the DOC API answered a two-rule window in ~31s from a
# residential connection. A ten-second ceiling reported a live-but-slow API
# as a timeout, which reads as an outage and is a different fact.
_TIMEOUT_S = 45
# The request window, measured back from the caller's `as_of`. `fetch_news`
# trims every record to the caller's own lookback afterwards; this only bounds
# what is asked for.
_LOOKBACK = timedelta(hours=48)
# GDELT's explicit-window format, in UTC. `timespan=48h` means "48 hours back
# from now", so every non-live as_of asked for today's articles and then
# dropped all of them against the cutoff below — a permanent empty window with
# no error, indistinguishable from a quiet press.
_WINDOW_FORMAT = "%Y%m%d%H%M%S"
# GDELT's per-request ceiling. Named because it bounds every window the desk
# shows, and a silent cap on a news feed reads as "that is all there was".
_MAX_RECORDS = 75
_MIN_INTERVAL_S = 1.0                # GDELT answers a burst with a non-JSON body
_last_request = 0.0


def _get_json(url: str) -> dict:
    global _last_request
    wait = _MIN_INTERVAL_S - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    # User-Agent only, as feed._fetch_rss sends. urlopen does not decode
    # content encodings, so negotiating gzip would hand compressed bytes
    # straight to json.loads on the first live call.
    request = urllib.request.Request(
        url, headers={"User-Agent": "qlab-news/0.1 (+https://github.com/qlab)"})
    response = urllib.request.urlopen(request, timeout=_TIMEOUT_S)
    try:
        payload = response.read()
    finally:
        response.close()
    _last_request = time.monotonic()
    return json.loads(payload)


def fetch(as_of: datetime, universe: tuple[str, ...]) -> list[NewsItem]:
    rules = (load_news_sources().get("gdelt") or {}).get("rules")
    # Refuses an unconfigured or half-written section by name rather than
    # reading as a quiet press: an empty result must be a fact about the
    # coverage, never about who last edited the yaml.
    _validate_gdelt_rules(rules)
    wanted = {t.upper() for t in universe}
    start = (as_of - _LOOKBACK).astimezone(timezone.utc)
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
            "maxrecords": _MAX_RECORDS,
            "startdatetime": start.strftime(_WINDOW_FORMAT),
            "enddatetime": as_of.astimezone(timezone.utc).strftime(
                _WINDOW_FORMAT),
            "sort": "datedesc"}, safe=":")
        payload = _get_json(f"{_DOC_URL}?{params}")
        for art in payload.get("articles", []):
            # GDELT indexes the world's press; the desk reads one language,
            # and an untranslated headline is not evidence it can weigh. Only a
            # field that is PRESENT and non-English drops the article: the query
            # already carries sourcelang:english, so reading a dropped or renamed
            # field as "not english" would empty the window on a schema change —
            # a fact about the payload, not about the press.
            lang = art.get("language")
            if lang is not None and str(lang).lower() != "english":
                continue
            seen = datetime.strptime(
                art["seendate"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            if seen >= as_of or seen < as_of - _LOOKBACK:
                continue
            headline = str(art.get("title") or "").strip()
            url = str(art.get("url") or "").strip()
            if not url:
                # No silent fallback: a secondary-tier article nobody can open
                # corroborates nothing, and an unopenable row in the archive is
                # evidence the desk cannot check.
                raise ValueError(
                    f"gdelt returned an article with no url: {headline!r}")
            # One article matched by two rules is emitted twice, once per ticker
            # set. That is deliberate: archive.py collapses the pair by
            # `content_hash` — which covers source, url, timestamp and text but
            # not tickers — and unions the ticker edges, so the duplicate is the
            # mapping, never the evidence.
            dated.append((seen, NewsItem(
                source=str(art.get("domain") or "gdelt"),
                published=seen.isoformat(),
                headline=headline,
                summary="",
                url=url,
                tickers=tickers,
                provider="gdelt",
            )))
    # Newest first, so a window truncated downstream keeps the freshest
    # coverage rather than whichever rule happened to be listed first.
    dated.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in dated]
