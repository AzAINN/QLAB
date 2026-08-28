"""Bounded, provenance-carrying news feeds for the research lane."""

from qlab.news.feed import (
    PROVIDERS,
    NewsItem,
    StackedWindow,
    cached_news_provenance,
    fetch_news,
    fetch_news_stacked,
    load_news_sources,
    load_plugin_providers,
    parse_provider_stack,
)

__all__ = [
    "NewsItem",
    "PROVIDERS",
    "StackedWindow",
    "cached_news_provenance",
    "fetch_news",
    "fetch_news_stacked",
    "load_news_sources",
    "load_plugin_providers",
    "parse_provider_stack",
]
