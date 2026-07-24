"""News feed providers with deterministic offline fixtures.

The provider seam follows :mod:`qlab.core.data`: callers select a named
provider explicitly or through an environment variable, and ``offline=True``
bypasses online provider selection entirely. Unlike market data, an online RSS
failure never falls back to synthetic news.

Version 1 intentionally has no persistent cache. Synthetic results are cheap
and reproducible, while RSS requests are fresh on every call; a later owner
integration may add a cache under ``state_path("news_cache")`` without changing
this provider contract.
"""

from __future__ import annotations

import calendar
import hashlib
import os
import random
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from qlab.paths import data_path

_FETCH_TIMEOUT_S = 5
_SYNTHETIC_OFFSETS_HOURS = (3, 9, 18, 30, 42, 54, 66)


@dataclass(frozen=True)
class NewsItem:
    """One immutable news record and its source mapping."""

    source: str
    published: str
    headline: str
    summary: str
    url: str
    tickers: tuple[str, ...]
    provider: str

    def provenance(self) -> dict[str, str]:
        """Return the fields needed to trace the record to its origin."""
        return {
            "source": self.source,
            "published": self.published,
            "url": self.url,
            "provider": self.provider,
        }


ProviderFetch = Callable[[datetime, tuple[str, ...]], list[NewsItem]]
PROVIDERS: dict[str, ProviderFetch] = {}


def fetch_news(
    as_of: str | date | datetime,
    universe: Sequence[str],
    lookback_hours: int = 48,
    provider: str | None = None,
    offline: bool = False,
) -> list[NewsItem]:
    """Return recent news for ``universe`` with deterministic ordering.

    ``offline=True`` always uses the synthetic provider and never resolves or
    invokes the configured online provider. Otherwise the provider defaults to
    ``QLAB_NEWS_PROVIDER`` or ``"synthetic"``. Results are bounded to
    ``[as_of - lookback_hours, as_of]`` and sorted by publication time
    descending, then source ascending.
    """
    if isinstance(lookback_hours, bool) or not isinstance(lookback_hours, int):
        raise TypeError("lookback_hours must be an integer")
    if lookback_hours < 0:
        raise ValueError("lookback_hours must be non-negative")

    as_of_dt = _as_datetime(as_of)
    tickers = _normalize_universe(universe)
    provider_name = "synthetic" if offline else _provider_name(provider)
    provider_name, fetch = _resolve_provider(provider_name)
    raw_items = fetch(as_of_dt, tickers)

    cutoff = as_of_dt - timedelta(hours=lookback_hours)
    universe_set = set(tickers)
    items: list[NewsItem] = []
    for item in raw_items:
        if not isinstance(item, NewsItem):
            raise TypeError(
                f"news provider {provider_name!r} returned a non-NewsItem record"
            )
        try:
            published = _as_datetime(item.published)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"news provider {provider_name!r} returned an invalid "
                f"published timestamp {item.published!r}"
            ) from exc
        if published < cutoff or published > as_of_dt:
            continue
        mapped = tuple(
            ticker
            for ticker in _normalize_universe(item.tickers)
            if ticker in universe_set
        )
        if not mapped:
            continue
        items.append(
            replace(
                item,
                published=_iso_timestamp(published),
                tickers=mapped,
                provider=provider_name,
            )
        )

    return sorted(
        items,
        key=lambda item: (
            -_as_datetime(item.published).timestamp(),
            item.source.casefold(),
            item.headline.casefold(),
            item.url,
        ),
    )


@lru_cache(maxsize=4)
def load_news_sources(path: str | Path | None = None) -> dict[str, Any]:
    """Load and validate ``configs/news_sources.yaml`` via :func:`data_path`."""
    config_path = (
        Path(path) if path is not None else data_path("configs", "news_sources.yaml")
    )
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    _validate_config(raw)
    return raw


def _fetch_synthetic(
    as_of: datetime,
    universe: tuple[str, ...],
) -> list[NewsItem]:
    config = load_news_sources()
    templates = config["synthetic"]
    seed_material = f"{_iso_timestamp(as_of)}|{'|'.join(universe)}"
    seed = int.from_bytes(
        hashlib.sha256(seed_material.encode("utf-8")).digest()[:8],
        "big",
    )
    rng = random.Random(seed)
    ordered_tickers = list(universe)
    rng.shuffle(ordered_tickers)

    items: list[NewsItem] = []
    for index, ticker in enumerate(ordered_tickers):
        raw_templates = templates.get(ticker)
        if raw_templates is None:
            continue
        choices = raw_templates if isinstance(raw_templates, list) else [raw_templates]
        template = choices[rng.randrange(len(choices))]
        block, offset_index = divmod(index, len(_SYNTHETIC_OFFSETS_HOURS))
        offset = (
            _SYNTHETIC_OFFSETS_HOURS[offset_index]
            + block * 72
            + rng.randrange(0, 3)
        )
        published = as_of - timedelta(hours=offset)
        items.append(
            NewsItem(
                source=_format_template(template["source"], ticker),
                published=_iso_timestamp(published),
                headline=_format_template(template["headline"], ticker),
                summary=_format_template(template["summary"], ticker),
                url=_format_template(
                    template.get("url", f"synthetic://qlab/{ticker.lower()}"),
                    ticker,
                ),
                tickers=(ticker,),
                provider="synthetic",
            )
        )
    return items


def _fetch_rss(
    _as_of: datetime,
    universe: tuple[str, ...],
) -> list[NewsItem]:
    config = load_news_sources()
    items: list[NewsItem] = []
    for feed in config["feeds"]:
        source = feed["name"]
        url = feed["url"]
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "qlab-news/0.1 (+https://github.com/qlab)"},
        )
        response = None
        try:
            response = urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_S)
            payload = response.read()
        except Exception as exc:
            raise RuntimeError(
                "rss news provider requires reachable network feeds; "
                f"source {source!r} at {url!r} is unavailable ({exc})"
            ) from exc
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

        try:
            entries = _parse_feed(payload, url)
        except Exception as exc:
            raise RuntimeError(
                f"rss news provider could not parse source {source!r} "
                f"at {url!r} ({exc})"
            ) from exc

        for entry in entries:
            headline = entry.get("headline", "").strip()
            published_raw = entry.get("published", "").strip()
            if not headline or not published_raw:
                continue
            try:
                published = _as_datetime(published_raw)
            except (TypeError, ValueError):
                continue
            summary = entry.get("summary", "").strip()
            tickers = _map_tickers(
                feed,
                f"{headline}\n{summary}",
                universe,
            )
            if not tickers:
                continue
            items.append(
                NewsItem(
                    source=source,
                    published=_iso_timestamp(published),
                    headline=headline,
                    summary=summary,
                    url=entry.get("url", "").strip(),
                    tickers=tickers,
                    provider="rss",
                )
            )
    return items


def _parse_feed(payload: bytes, feed_url: str) -> list[dict[str, str]]:
    try:
        import feedparser  # type: ignore[import-not-found]  # noqa: PLC0415
    except ImportError:
        feedparser = None

    if feedparser is not None:
        parsed = feedparser.parse(payload)
        entries = [
            normalized
            for entry in getattr(parsed, "entries", ())
            if (normalized := _normalize_feedparser_entry(entry, feed_url))
        ]
        if entries:
            return entries

    return _parse_xml_feed(payload, feed_url)


def _normalize_feedparser_entry(
    entry: Mapping[str, Any],
    feed_url: str,
) -> dict[str, str] | None:
    headline = str(entry.get("title", "") or "").strip()
    summary = str(
        entry.get("summary", "")
        or entry.get("description", "")
        or _feedparser_content(entry)
        or ""
    ).strip()
    url = urllib.parse.urljoin(
        feed_url,
        str(entry.get("link", "") or "").strip(),
    )
    published = str(
        entry.get("published", "")
        or entry.get("updated", "")
        or entry.get("created", "")
        or ""
    ).strip()
    if not published:
        parsed_time = (
            entry.get("published_parsed")
            or entry.get("updated_parsed")
            or entry.get("created_parsed")
        )
        if parsed_time:
            published = _iso_timestamp(
                datetime.fromtimestamp(calendar.timegm(parsed_time), timezone.utc)
            )
    if not headline or not published:
        return None
    return {
        "headline": headline,
        "summary": summary,
        "url": url,
        "published": published,
    }


def _feedparser_content(entry: Mapping[str, Any]) -> str:
    content = entry.get("content")
    if not isinstance(content, list):
        return ""
    for part in content:
        if isinstance(part, Mapping) and part.get("value"):
            return str(part["value"])
    return ""


def _parse_xml_feed(payload: bytes, feed_url: str) -> list[dict[str, str]]:
    root = ElementTree.fromstring(payload)
    entries: list[dict[str, str]] = []
    for node in root.iter():
        if _local_name(node.tag) not in {"item", "entry"}:
            continue
        headline = _xml_text(node, "title")
        published = _xml_text(node, "pubdate", "published", "updated", "date")
        if not headline or not published:
            continue
        entries.append(
            {
                "headline": headline,
                "summary": _xml_text(
                    node,
                    "summary",
                    "description",
                    "content",
                    "encoded",
                ),
                "url": urllib.parse.urljoin(feed_url, _xml_link(node)),
                "published": published,
            }
        )
    return entries


def _xml_text(node: ElementTree.Element, *names: str) -> str:
    wanted = set(names)
    for child in node.iter():
        if child is node or _local_name(child.tag) not in wanted:
            continue
        value = "".join(child.itertext()).strip()
        if value:
            return value
    return ""


def _xml_link(node: ElementTree.Element) -> str:
    fallback = ""
    for child in node.iter():
        if child is node or _local_name(child.tag) != "link":
            continue
        href = str(child.attrib.get("href", "") or "").strip()
        rel = str(child.attrib.get("rel", "alternate") or "alternate").casefold()
        if href and rel == "alternate":
            return href
        text = "".join(child.itertext()).strip()
        fallback = fallback or href or text
    return fallback


def _map_tickers(
    feed: Mapping[str, Any],
    text: str,
    universe: tuple[str, ...],
) -> tuple[str, ...]:
    universe_set = set(universe)
    mapped: set[str] = set()
    direct = feed.get("tickers")
    keyword_rules = feed.get("keywords")
    if direct is not None:
        direct_tickers = {str(ticker).strip().upper() for ticker in direct}
        if (
            isinstance(keyword_rules, list)
            and keyword_rules
            and all(isinstance(term, str) for term in keyword_rules)
        ):
            folded = text.casefold()
            if any(term.casefold() in folded for term in keyword_rules):
                mapped.update(direct_tickers)
        else:
            mapped.update(direct_tickers)

    if isinstance(keyword_rules, list):
        folded = text.casefold()
        for rule in keyword_rules:
            if not isinstance(rule, Mapping):
                continue
            terms = rule.get("terms", rule.get("keywords", ()))
            if any(str(term).casefold() in folded for term in terms):
                mapped.update(
                    str(ticker).strip().upper()
                    for ticker in rule.get("tickers", ())
                )

    return tuple(ticker for ticker in universe if ticker in mapped & universe_set)


def _provider_name(provider: str | None) -> str:
    return (
        provider or os.environ.get("QLAB_NEWS_PROVIDER") or "synthetic"
    ).strip().lower()


def _resolve_provider(provider: str | None) -> tuple[str, ProviderFetch]:
    name = _provider_name(provider)
    try:
        return name, PROVIDERS[name]
    except KeyError as exc:
        available = ", ".join(sorted(PROVIDERS))
        raise RuntimeError(
            f"unknown news provider {name!r}; available providers: {available}"
        ) from exc


def _normalize_universe(universe: Sequence[str]) -> tuple[str, ...]:
    if isinstance(universe, (str, bytes)):
        raise TypeError("universe must be a sequence of ticker strings")
    normalized: set[str] = set()
    for ticker in universe:
        if not isinstance(ticker, str) or not ticker.strip():
            raise ValueError("universe contains an invalid ticker")
        normalized.add(ticker.strip().upper())
    return tuple(sorted(normalized))


def _as_datetime(value: str | date | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("timestamp must not be empty")
        try:
            parsed = datetime.fromisoformat(
                f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
            )
        except ValueError:
            parsed = parsedate_to_datetime(text)
    else:
        raise TypeError("timestamp must be an ISO string, date, or datetime")

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _format_template(value: Any, ticker: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(
            f"synthetic news template for {ticker!r} has an invalid text field"
        )
    try:
        return value.format(ticker=ticker).strip()
    except (IndexError, KeyError, ValueError) as exc:
        raise RuntimeError(
            f"synthetic news template for {ticker!r} could not be rendered"
        ) from exc


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1].casefold()


def _validate_config(raw: Any) -> None:
    if not isinstance(raw, dict):
        raise ValueError("news sources config must be a mapping")
    feeds = raw.get("feeds")
    if not isinstance(feeds, list) or not feeds:
        raise ValueError("news sources config field 'feeds' must be a non-empty list")
    for index, feed in enumerate(feeds):
        if not isinstance(feed, dict):
            raise ValueError(f"news feed {index} must be a mapping")
        for field in ("name", "url"):
            value = feed.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"news feed {index} field {field!r} must be a non-empty string"
                )
        direct = feed.get("tickers")
        rules = feed.get("keywords")
        if direct is None and rules is None:
            raise ValueError(
                f"news feed {feed['name']!r} must define tickers or keywords"
            )
        if direct is not None:
            _validate_ticker_list(direct, f"news feed {feed['name']!r} tickers")
        if rules is not None:
            if not isinstance(rules, list) or not rules:
                raise ValueError(
                    f"news feed {feed['name']!r} keywords must be a non-empty list"
                )
            if all(isinstance(rule, str) for rule in rules):
                if direct is None:
                    raise ValueError(
                        f"news feed {feed['name']!r} string keywords need tickers"
                    )
                continue
            for rule_index, rule in enumerate(rules):
                if not isinstance(rule, dict):
                    raise ValueError(
                        f"news feed {feed['name']!r} keyword rule "
                        f"{rule_index} must be a mapping"
                    )
                terms = rule.get("terms", rule.get("keywords"))
                if (
                    not isinstance(terms, list)
                    or not terms
                    or not all(isinstance(term, str) and term.strip() for term in terms)
                ):
                    raise ValueError(
                        f"news feed {feed['name']!r} keyword rule "
                        f"{rule_index} has invalid terms"
                    )
                _validate_ticker_list(
                    rule.get("tickers"),
                    f"news feed {feed['name']!r} keyword rule {rule_index} tickers",
                )

    synthetic = raw.get("synthetic")
    if not isinstance(synthetic, dict) or not synthetic:
        raise ValueError(
            "news sources config field 'synthetic' must be a non-empty mapping"
        )
    for ticker, templates in synthetic.items():
        if not isinstance(ticker, str) or not ticker.strip():
            raise ValueError("synthetic news config contains an invalid ticker")
        choices = templates if isinstance(templates, list) else [templates]
        if not choices:
            raise ValueError(
                f"synthetic news config for {ticker!r} must not be empty"
            )
        for template in choices:
            if not isinstance(template, dict):
                raise ValueError(
                    f"synthetic news template for {ticker!r} must be a mapping"
                )
            for field in ("source", "headline", "summary"):
                value = template.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(
                        f"synthetic news template for {ticker!r} field "
                        f"{field!r} must be a non-empty string"
                    )


def _validate_ticker_list(value: Any, label: str) -> None:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(ticker, str) and ticker.strip() for ticker in value)
    ):
        raise ValueError(f"{label} must be a non-empty list of strings")


PROVIDERS.update(
    {
        "synthetic": _fetch_synthetic,
        "rss": _fetch_rss,
    }
)
