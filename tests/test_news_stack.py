from importlib import metadata

from qlab.news import feed


def test_plugin_providers_are_discovered_from_the_entry_point_group(monkeypatch):
    def fetch(as_of, universe):
        return []

    class Ep:
        name = "acme"
        group = "qlab.news.providers"

        def load(self):
            return fetch

    monkeypatch.setattr(
        metadata, "entry_points",
        lambda **kw: [Ep()] if kw.get("group") == "qlab.news.providers" else [])
    monkeypatch.delitem(feed.PROVIDERS, "acme", raising=False)
    found = feed.load_plugin_providers()
    assert found["acme"] is fetch
    assert feed.PROVIDERS["acme"] is fetch


def test_a_plugin_may_not_shadow_a_first_party_provider(monkeypatch):
    class Ep:
        name = "alpaca"
        group = "qlab.news.providers"

        def load(self):
            return lambda a, u: []

    monkeypatch.setattr(metadata, "entry_points", lambda **kw: [Ep()])
    import pytest
    with pytest.raises(RuntimeError, match="shadows the first-party provider"):
        feed.load_plugin_providers()
