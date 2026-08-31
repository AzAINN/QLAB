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
import sys
import random
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from qlab.paths import data_path

_FETCH_TIMEOUT_S = 5
# Publication ages for synthetic items, in hours. Every entry is <= 45 so the
# +0..2 jitter below can never push an item past a 48h window: the count of
# in-window items is then a function of universe size alone, not of which
# template the shuffle happened to draw. The old 7-entry schedule combined with
# `block * 72` dated the eighth item onward up to nine days back, so a 20-name
# universe returned a mean of 2.1 items over a 48h window and zero on some
# dates — which would leave the qualitative signals with no input at all.
# One item is deliberately left outside any plausible window (54) so the cutoff
# filter stays exercised rather than becoming vacuously true.
_SYNTHETIC_OFFSETS_HOURS = (
    3, 6, 9, 12, 15, 18, 21, 24, 27, 30, 33, 36, 39, 42, 45, 54,
)


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

PLUGIN_GROUP = "qlab.news.providers"
_FIRST_PARTY = frozenset({"synthetic", "rss", "alpaca", "edgar", "macro", "gdelt"})


def load_plugin_providers() -> dict[str, ProviderFetch]:
    """Discover third-party providers and merge them into ``PROVIDERS``.

    The shareable unit: a pip package declaring an entry point in
    ``qlab.news.providers`` is a provider this desk can name, with no change
    to qlab. First-party names may not be shadowed — a plugin quietly
    replacing ``alpaca`` would be a provenance lie with a familiar label.
    """
    from importlib import metadata

    found: dict[str, ProviderFetch] = {}
    for ep in metadata.entry_points(group=PLUGIN_GROUP):
        if ep.name in _FIRST_PARTY:
            raise RuntimeError(
                f"news plugin {ep.name!r} shadows the first-party provider of "
                "the same name; rename the entry point")
        found[ep.name] = ep.load()
    PROVIDERS.update(found)
    return found


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
    the configured stack (``QLAB_NEWS_PROVIDERS``, then ``QLAB_NEWS_PROVIDER``,
    then ``"synthetic"``) and refuses a stack of more than one member — that is
    :func:`fetch_news_stacked`'s window, not this one. Results are bounded to
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
    # A partial answer is still an answer: its records go through exactly the
    # same window contract below — look-ahead gate, universe mapping, provider
    # stamp — and the refusal is re-raised once they have.
    partial_failures: dict[str, str] = {}
    try:
        raw_items = fetch(as_of_dt, tickers)
    except PartialWindow as exc:
        raw_items = exc.items
        partial_failures = exc.failures

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
        # An untagged item is macro context, not a mis-tagged holding item.
        # Dropping it made a cross-asset desk report "quiet" whenever the wire
        # simply was not naming ACWI or BNDW — which is almost always. It is
        # kept with no tickers, so nothing downstream can mistake it for
        # evidence about a specific position.
        if not mapped and item.tickers:
            continue
        items.append(
            replace(
                item,
                published=_iso_timestamp(published),
                tickers=mapped,
                provider=provider_name,
            )
        )

    ordered = sorted(items, key=_ordering_key)
    if partial_failures:
        raise PartialWindow(ordered, partial_failures)
    return ordered


def _ordering_key(item: NewsItem) -> tuple[float, str, str, str]:
    """The one window ordering: newest first, then source, headline, url.

    A stack of one member must be byte-for-byte a plain fetch, so the merge in
    :func:`fetch_news_stacked` and :func:`fetch_news` share this key rather
    than each spelling out an ordering that could drift apart.
    """
    return (
        -_as_datetime(item.published).timestamp(),
        item.source.casefold(),
        item.headline.casefold(),
        item.url,
    )


# The outcome vocabulary, named once. Three call sites compared the literal
# "ok", and a fourth state (a member that came back with some of its feeds)
# would otherwise have had to be discovered by each of them independently.
NEWS_OUTCOME_OK = "ok"
_PARTIAL_PREFIX = "partial: "


def outcome_is_live(outcome: str) -> bool:
    """Whether a member's outcome carried records. Partial counts as live."""
    text = str(outcome or "")
    return text == NEWS_OUTCOME_OK or text.startswith(_PARTIAL_PREFIX)


class PartialWindow(RuntimeError):
    """A provider answered with some of its feeds, and lost the rest.

    Measured on 2026-08-28: one publisher started refusing automated requests
    and took a three-feed provider dark although two feeds were answering. A
    provider that refuses outright is one fact; a provider that came back with
    two of three sources is a different one, and the records it did return are
    real. Both the records and the names of what was lost travel with it.
    """

    def __init__(self, items: list[NewsItem], failures: dict[str, str]):
        self.items = list(items)
        self.failures = dict(failures)
        super().__init__(
            "some feeds were unavailable: "
            + "; ".join(f"{k}: {v}" for k, v in self.failures.items()))


class StackFailed(RuntimeError):
    """Every member of the stack failed, and each one's own reason.

    A RuntimeError still, because every caller already catches one — but the
    per-member sentences travel with it. Stamping the aggregate sentence on
    each member reported a diagnosis no member ever gave.
    """

    def __init__(self, outcomes: dict[str, str]):
        self.outcomes = dict(outcomes)
        super().__init__(
            "every provider in the stack failed: "
            + "; ".join(f"{k}: {v}" for k, v in self.outcomes.items()))


@dataclass(frozen=True)
class StackedWindow:
    """Every member's records, and what each member said about fetching."""

    items: list[NewsItem]
    outcomes: dict[str, str]           # provider -> "ok" | error sentence
    providers: tuple[str, ...]
    # provider -> {feed: error}, for members that answered with some feeds.
    # The outcome sentence says the same thing in prose; a caller that has to
    # re-parse that sentence to name the feed is a caller that stops naming it.
    partials: dict[str, dict[str, str]] = field(default_factory=dict)


def parse_provider_stack(value: str | None) -> tuple[str, ...]:
    """The providers to read, in order. Plural env wins, then singular."""
    raw = (value or os.environ.get("QLAB_NEWS_PROVIDERS")
           or os.environ.get("QLAB_NEWS_PROVIDER") or "synthetic")
    names = tuple(n.strip().lower() for n in raw.split(",") if n.strip())
    if not names:
        raise ValueError("the provider stack is empty")
    return names


def fetch_news_stacked(
    as_of: str | date | datetime,
    universe: Sequence[str],
    providers: Sequence[str],
    lookback_hours: int = 48,
) -> StackedWindow:
    """Fetch every member of a stack; report each member; merge the records.

    A dead member is an outcome, not a smaller window: the sentence travels
    with the result so the desk can say which source went away. Only a stack
    with NO living member raises — that is a desk with no record at all.
    """
    load_plugin_providers()
    items: list[NewsItem] = []
    outcomes: dict[str, str] = {}
    partials: dict[str, dict[str, str]] = {}
    for name in providers:
        try:
            got = fetch_news(as_of, universe, lookback_hours=lookback_hours,
                             provider=name)
        except PartialWindow as exc:
            # The member is live and short some feeds. Its records join the
            # merge and the feeds it lost are named in its outcome, where the
            # desk and the archive both read it.
            outcomes[name] = _PARTIAL_PREFIX + "; ".join(
                f"{feed}: {error}" for feed, error in exc.failures.items())
            partials[name] = dict(exc.failures)
            items.extend(exc.items)
            continue
        except Exception as exc:
            outcomes[name] = str(exc)
            continue
        outcomes[name] = NEWS_OUTCOME_OK
        items.extend(got)
    if not any(outcome_is_live(v) for v in outcomes.values()):
        raise StackFailed(outcomes)
    items.sort(key=_ordering_key)
    return StackedWindow(items=items, outcomes=outcomes,
                         providers=tuple(providers), partials=partials)


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
        # `block` adds one hour per wrap, not 72: it only breaks ties between
        # tickers sharing a slot, and must not walk items out of the window.
        offset = (
            _SYNTHETIC_OFFSETS_HOURS[offset_index]
            + block
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
    return _fetch_rss_feeds(load_news_sources()["feeds"], _as_of, universe)


def _fetch_rss_feeds(
    feeds: list[dict],
    as_of: datetime,
    universe: tuple[str, ...],
) -> list[NewsItem]:
    """Read an explicit list of feed definitions with the one RSS parser.

    Split out of :func:`_fetch_rss` so a provider carrying its own curated
    feeds — the official macro releases — shares this parser instead of
    growing a second one that could drift from it.
    """
    items: list[NewsItem] = []
    # Every feed is attempted, and a failure is collected rather than thrown:
    # one publisher going dark must not decide the window for the publishers
    # that are answering. What that adds up to is decided after the loop.
    failures: dict[str, str] = {}
    first_error: Exception | None = None
    live = 0
    for feed in feeds:
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
            error = RuntimeError(
                "rss news provider requires reachable network feeds; "
                f"source {source!r} at {url!r} is unavailable ({exc})"
            )
            error.__cause__ = exc
            failures[source] = str(exc)
            first_error = first_error or error
            continue
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

        try:
            entries = _parse_feed(payload, url)
        except Exception as exc:
            error = RuntimeError(
                f"rss news provider could not parse source {source!r} "
                f"at {url!r} ({exc})"
            )
            error.__cause__ = exc
            failures[source] = str(exc)
            first_error = first_error or error
            continue
        live += 1

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
    if failures:
        # All dead is the refusal this provider always made — a window with no
        # source behind it is not a window. Some dead with something live is a
        # partial: the caller decides what to do with records it really has.
        if not live:
            raise first_error
        raise PartialWindow(items, failures)
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
    """The one provider this singular API reads, or a refusal.

    ``provider=None`` resolves through :func:`parse_provider_stack`, so the
    plural ``QLAB_NEWS_PROVIDERS`` that docs/news-setup.md tells operators to
    set governs this path too. Reading only the singular variable meant a desk
    configured for two live sources silently fell through to the synthetic
    fixtures, under a configuration that had asked for neither.

    A stack of several members has no singular answer. Returning its first
    member would report one source's window under a configuration that asked
    for all of them, so this refuses and names the API that reads a stack.
    """
    if provider is not None and str(provider).strip():
        return str(provider).strip().lower()
    stack = parse_provider_stack(None)
    if len(stack) > 1:
        raise RuntimeError(
            f"the configured provider stack has {len(stack)} members "
            f"({', '.join(stack)}); fetch_news reads a single provider — call "
            "fetch_news_stacked to read the whole stack, or name one provider")
    return stack[0]


def _resolve_provider(provider: str | None) -> tuple[str, ProviderFetch]:
    name = _provider_name(provider)
    try:
        return name, PROVIDERS[name]
    except KeyError:
        pass
    # A name this process has not imported yet is not an unknown name. Only
    # fetch_news_stacked discovered plugins, so every singular path — the CLI's
    # `news-check --provider`, check_news — refused an installed entry-point
    # provider. Discovery is idempotent and runs only on a miss, so the common
    # path stays one dict lookup.
    load_plugin_providers()
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


def _validate_feeds(feeds: Any, field: str, label: str) -> None:
    """Hold a list of feed definitions to the one feed contract.

    ``field`` and ``label`` name the section in the error, so a bad entry
    under ``macro.feeds`` reads as one rather than as a top-level feed.
    """
    if not isinstance(feeds, list) or not feeds:
        raise ValueError(
            f"news sources config field {field!r} must be a non-empty list"
        )
    for index, feed in enumerate(feeds):
        if not isinstance(feed, dict):
            raise ValueError(f"{label} {index} must be a mapping")
        for field in ("name", "url"):
            value = feed.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"{label} {index} field {field!r} must be a non-empty string"
                )
        direct = feed.get("tickers")
        rules = feed.get("keywords")
        if direct is None and rules is None:
            raise ValueError(
                f"{label} {feed['name']!r} must define tickers or keywords"
            )
        if direct is not None:
            _validate_ticker_list(direct, f"{label} {feed['name']!r} tickers")
        if rules is not None:
            if not isinstance(rules, list) or not rules:
                raise ValueError(
                    f"{label} {feed['name']!r} keywords must be a non-empty list"
                )
            if all(isinstance(rule, str) for rule in rules):
                if direct is None:
                    raise ValueError(
                        f"{label} {feed['name']!r} string keywords need tickers"
                    )
                continue
            for rule_index, rule in enumerate(rules):
                if not isinstance(rule, dict):
                    raise ValueError(
                        f"{label} {feed['name']!r} keyword rule "
                        f"{rule_index} must be a mapping"
                    )
                terms = rule.get("terms", rule.get("keywords"))
                if (
                    not isinstance(terms, list)
                    or not terms
                    or not all(isinstance(term, str) and term.strip() for term in terms)
                ):
                    raise ValueError(
                        f"{label} {feed['name']!r} keyword rule "
                        f"{rule_index} has invalid terms"
                    )
                _validate_ticker_list(
                    rule.get("tickers"),
                    f"{label} {feed['name']!r} keyword rule {rule_index} tickers",
                )



def _validate_gdelt_rules(rules: Any) -> None:
    """Every GDELT rule must say what to search for and who it is about.

    The provider indexes both fields directly, so a rule missing either is a
    runtime failure on the first live call; it is cheaper to refuse the file.
    """
    if not isinstance(rules, list) or not rules:
        raise ValueError(
            "news sources config field 'gdelt.rules' must be a non-empty list"
        )
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ValueError(f"gdelt rule {index} must be a mapping")
        query = rule.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(
                f"gdelt rule {index} field 'query' must be a non-empty string"
            )
        _validate_ticker_list(rule.get("tickers"), f"gdelt rule {index} tickers")


def _validate_calendar(calendar: Any) -> None:
    """Every scheduled release must say what, when, for whom, and by whom.

    The calendar is hand-maintained and future-dated, so nothing downstream
    ever reads it as news and catches a half-written entry: it is either
    right here or wrong on the desk.
    """
    if not isinstance(calendar, list):
        raise ValueError("news sources config field 'calendar' must be a list")
    for index, entry in enumerate(calendar):
        if not isinstance(entry, dict):
            raise ValueError(f"news calendar entry {index} must be a mapping")
        for field in ("name", "when", "source"):
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"news calendar entry {index} field {field!r} must be a "
                    "non-empty string"
                )
        _validate_ticker_list(
            entry.get("tickers"),
            f"news calendar entry {entry['name']!r} tickers",
        )


def _validate_config(raw: Any) -> None:
    if not isinstance(raw, dict):
        raise ValueError("news sources config must be a mapping")
    feeds = raw.get("feeds")
    _validate_feeds(feeds, "feeds", "news feed")

    macro = raw.get("macro")
    if macro is not None:
        if not isinstance(macro, dict):
            raise ValueError("news sources config field 'macro' must be a mapping")
        _validate_feeds(macro.get("feeds"), "macro.feeds", "macro news feed")

    gdelt = raw.get("gdelt")
    if gdelt is not None:
        if not isinstance(gdelt, dict):
            raise ValueError("news sources config field 'gdelt' must be a mapping")
        _validate_gdelt_rules(gdelt.get("rules"))

    calendar = raw.get("calendar")
    if calendar is not None:
        _validate_calendar(calendar)

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


# Alpaca's per-request ceiling. Named because it bounds every window the desk
# shows, and a silent cap on a news feed reads as "that is all there was".
_ALPACA_LIMIT = 50


def _fetch_alpaca(
    as_of: datetime,
    universe: tuple[str, ...],
    *,
    market_context: bool = True,
) -> list[NewsItem]:
    """Fetch ticker-tagged news from Alpaca (Benzinga-backed).

    Strictly better grounded than generic RSS for this purpose: the venue tags
    each story with the symbols it concerns, so no keyword guessing is needed,
    and the window is a real date range rather than "whatever is in the feed
    right now" — which is what makes point-in-time replay possible.

    Uses the same credentials as the broker and market data. Fails loud: a
    news provider that silently returns nothing is indistinguishable from a
    quiet market, and those mean opposite things.
    """
    # Resolved the same way the broker and data lanes resolve, so a browser
    # login reaches news too. This read env vars directly, which meant an
    # operator who ran `alpaca profile login` — the flow the desk now
    # recommends, and the only paper-only one — was told to go and paste API
    # keys for news alone.
    from qlab.trader.alpaca_auth import (
        AlpacaAuthError, resolve_alpaca_credentials)

    try:
        creds = resolve_alpaca_credentials()
    except AlpacaAuthError as exc:
        # A malformed credential source is not absence; say which it was.
        raise RuntimeError(f"alpaca news credentials unusable: {exc}") from exc
    if creds is None:
        raise RuntimeError(
            "alpaca news provider needs a credential: run "
            "`alpaca profile login`, or set ALPACA_API_KEY and "
            "ALPACA_API_SECRET")
    try:
        from alpaca.data.historical.news import NewsClient
        from alpaca.data.requests import NewsRequest
    except ImportError as exc:
        # Name the interpreter. "install qlab[trader]" alone is actively
        # misleading on a machine that HAS installed it — into a different
        # environment. An editable install shares the source between
        # environments but not the dependencies, so the desk can be running
        # this exact file from a Python that never got alpaca-py, and the
        # operator reads a message telling them to do what they already did.
        raise RuntimeError(
            f"alpaca news provider requires the 'alpaca-py' package, which is "
            f"not installed in {sys.executable}. Install it into THIS "
            f"interpreter: {sys.executable} -m pip install 'qlab[trader]' "
            f"(a different environment having it does not help — that is the "
            f"usual cause of this message)") from exc

    client = (
        NewsClient(oauth_token=creds.oauth_token)
        if creds.kind == "oauth"
        else NewsClient(creds.api_key, creds.secret_key)
    )
    def _query(symbols: str | None) -> list:
        request = NewsRequest(
            symbols=symbols,
            start=as_of - timedelta(days=7),
            end=as_of,
            limit=_ALPACA_LIMIT,
            include_content=True,
            exclude_contentless=True,
        )
        try:
            response = client.get_news(request)
        except Exception as exc:
            raise RuntimeError(f"alpaca news request failed ({exc})") from exc
        raw = getattr(response, "data", None)
        return raw.get("news", []) if isinstance(raw, dict) else (raw or [])

    universe_set = set(universe)
    items: list[NewsItem] = []
    seen: set[str] = set()

    def _collect(records, *, require_universe: bool) -> None:
        for record in records:
            symbols = tuple(
                s for s in (getattr(record, "symbols", None) or [])
                if s in universe_set)
            if require_universe and not symbols:
                continue
            url = str(getattr(record, "url", "") or "")
            headline = str(getattr(record, "headline", "") or "")
            # The two queries overlap by construction, so dedupe on the record's
            # own identity rather than letting one story count twice toward
            # corroboration — that would manufacture agreement out of nothing.
            key = url or headline
            if not key or key in seen:
                continue
            seen.add(key)
            published = getattr(record, "created_at", None)
            items.append(NewsItem(
                source=str(getattr(record, "source", "") or "alpaca"),
                published=_iso_timestamp(_as_datetime(published)) if published
                else _iso_timestamp(as_of),
                headline=headline,
                summary=str(getattr(record, "summary", "") or ""),
                url=url,
                tickers=tuple(sorted(symbols)),
                provider="alpaca",
            ))

    _collect(_query(",".join(universe)), require_universe=True)
    if market_context:
        # A cross-asset desk holding ACWI/BNDW/EMB gets almost no symbol-tagged
        # coverage — Benzinga tags US equities, and six of this desk's seven
        # tickers return nothing. Without the untagged market window the desk
        # reports "quiet" for a market that was not quiet at all; it simply was
        # not talking about these tickers. Items carry no tickers, which is
        # honest: they are macro context, not evidence about a holding.
        _collect(_query(None), require_universe=False)
    return items


PROVIDERS.update(
    {
        "synthetic": _fetch_synthetic,
        "rss": _fetch_rss,
        "alpaca": _fetch_alpaca,
    }
)

# Last statements in the module, deliberately: the first-party provider modules
# import NewsItem and load_news_sources from here, so this cycle only closes
# once every name they need is defined.
from qlab.news.providers import register_first_party  # noqa: E402

register_first_party(PROVIDERS)
