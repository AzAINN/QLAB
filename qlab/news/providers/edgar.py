"""SEC EDGAR as a primary source: filings, dated by acceptance time.

The SEC's own record of what an issuer told the market, which is the event
the headlines report. ``source`` carries the marker ``grounding.source_tier``
reads, so every record here is primary-tier with no new code path — and a
primary claim stands alone, which is the corroboration the desk has lacked.

Two SEC rules are honoured by refusal rather than by default: requests must
carry a descriptive User-Agent with a contact (``QLAB_EDGAR_CONTACT``), and
the rate stays under ten requests a second (one submissions call per CIK,
the CIK map cached a week). Point-in-time by ``acceptanceDateTime``: a
filing's *date* is the day, its acceptance the instant, and a morning read
must not see an evening filing.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from qlab.news.feed import NewsItem, load_news_sources
from qlab.paths import state_path

SOURCE = "SEC EDGAR"
KEPT_FORMS = ("8-K", "10-Q", "10-K", "N-PORT", "N-CSR", "13F-HR")
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
_CACHE_TTL = timedelta(days=7)
# How far back the provider will look at all. It is a bound on the request,
# not the desk's window: `fetch_news` is the window authority and trims every
# record to the caller's own lookback afterwards, so a 72h caller sees 72h
# whatever this says — including a 10-Q accepted yesterday, which arrives on
# the same wire as everything else. The bound therefore matters only to a
# caller with a LONG lookback_hours: a quarterly read asking for 90 days would
# silently see a shorter history if this were tighter than the period between
# the periodic forms in KEPT_FORMS (10-Q, 10-K, N-CSR, 13F-HR).
_MAX_LOOKBACK = timedelta(days=120)
_MIN_INTERVAL_S = 0.12               # under the SEC's 10 requests/second
_TIMEOUT_S = 10
_last_request = 0.0


def _contact() -> str:
    contact = os.environ.get("QLAB_EDGAR_CONTACT", "").strip()
    if not contact:
        raise RuntimeError(
            "the edgar provider needs QLAB_EDGAR_CONTACT (e.g. 'Your Name "
            "<you@example.org>'): the SEC requires a descriptive User-Agent "
            "with a contact, and this desk does not send an invented one")
    return contact


def _get_json(url: str) -> dict:
    global _last_request
    wait = _MIN_INTERVAL_S - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    # User-Agent only, as feed._fetch_rss sends. urlopen does not decode
    # content encodings, so negotiating gzip would hand compressed bytes
    # straight to json.loads on the first live call.
    request = urllib.request.Request(
        url, headers={"User-Agent": f"qlab-news/0.1 {_contact()}"})
    response = urllib.request.urlopen(request, timeout=_TIMEOUT_S)
    try:
        payload = response.read()
    finally:
        response.close()
    _last_request = time.monotonic()
    return json.loads(payload)


def cik_map() -> dict[str, str]:
    """Ticker -> zero-padded CIK, from the SEC's list, cached for a week."""
    cache = state_path("news_cache", "company_tickers.json")
    fresh = (cache.exists()
             and datetime.now(timezone.utc)
             - datetime.fromtimestamp(cache.stat().st_mtime, timezone.utc)
             < _CACHE_TTL)
    if fresh:
        raw = json.loads(cache.read_text(encoding="utf-8"))
    else:
        raw = _get_json(_TICKERS_URL)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(raw), encoding="utf-8")
    return {str(row["ticker"]).upper(): f"{int(row['cik_str']):010d}"
            for row in raw.values()}


def _issuers_for(universe: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    """Which CIK-bearing tickers each universe name is read through.

    The fund itself always; plus the operator's curated ``edgar.issuers``
    list — phase-one holdings knowledge, shareable as data.
    """
    config = (load_news_sources().get("edgar") or {}).get("issuers") or {}
    out: dict[str, tuple[str, ...]] = {}
    for name in universe:
        listed = tuple(str(t).upper() for t in config.get(name, ()))
        out[name] = tuple(dict.fromkeys((name.upper(), *listed)))
    return out


def fetch(as_of: datetime, universe: tuple[str, ...]) -> list[NewsItem]:
    _contact()
    ciks = cik_map()
    window_start = as_of - _MAX_LOOKBACK
    items: list[NewsItem] = []
    # Caches the REQUEST, not the record: one submissions call per CIK keeps
    # the rate constraint, while an issuer held by two funds is evidence for
    # both. Deduping the record instead tagged every shared megacap to
    # whichever fund happened to be walked first.
    submissions: dict[str, dict] = {}
    for fund, issuers in _issuers_for(universe).items():
        for ticker in issuers:
            cik = ciks.get(ticker)
            if cik is None:
                continue
            if cik not in submissions:
                submissions[cik] = _get_json(_SUBMISSIONS_URL.format(cik=cik))
            sub = submissions[cik]
            recent = (sub.get("filings") or {}).get("recent") or {}
            name = sub.get("name") or ticker
            forms = recent.get("form", [])
            # strict: a short column is a shape change at the SEC, and zip's
            # default would turn it into a quietly truncated window.
            rows = zip(forms, recent.get("acceptanceDateTime", []),
                       recent.get("accessionNumber", []), recent.get("primaryDocument", []),
                       recent.get("items", []) or [""] * len(forms),
                       strict=True)
            for form, accepted, acc, doc, item_codes in rows:
                if form not in KEPT_FORMS:
                    continue
                published = datetime.fromisoformat(accepted.replace("Z", "+00:00"))
                if published < window_start or published >= as_of:
                    continue
                summary = f"{form} filed by {name}"
                if item_codes:
                    summary += f" — Items {', '.join(c.strip() for c in item_codes.split(','))}"
                items.append(NewsItem(
                    source=SOURCE,
                    published=published.isoformat(),
                    headline=f"{form} — {name}",
                    summary=summary,
                    url=_ARCHIVE_URL.format(cik=cik, acc=acc.replace("-", ""), doc=doc),
                    tickers=(fund,),
                    provider="edgar",
                ))
    return items
