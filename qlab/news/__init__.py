"""Bounded, provenance-carrying news feeds for the research lane."""

from qlab.news.feed import (
    PROVIDERS,
    NewsItem,
    fetch_news,
    load_news_sources,
)

__all__ = [
    "NewsItem",
    "PROVIDERS",
    "fetch_news",
    "load_news_sources",
]
