import json
from datetime import datetime, timezone

import pytest

from qlab.news.providers import edgar

TICKERS = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
           "1": {"cik_str": 884394, "ticker": "SPY", "title": "SPDR S&P 500 ETF"}}

SUBMISSIONS = {
    "name": "Apple Inc.",
    "filings": {"recent": {
        "form": ["8-K", "4", "10-Q"],
        "filingDate": ["2026-08-27", "2026-08-27", "2026-08-01"],
        "acceptanceDateTime": ["2026-08-27T20:31:00.000Z",
                               "2026-08-27T18:00:00.000Z",
                               "2026-08-01T21:00:00.000Z"],
        "accessionNumber": ["0000320193-26-000090", "0000320193-26-000089",
                            "0000320193-26-000070"],
        "primaryDocument": ["a8k.htm", "f4.xml", "q3.htm"],
        "items": ["2.02,9.01", "", ""],
    }},
}


@pytest.fixture
def sec(monkeypatch, tmp_path):
    monkeypatch.setenv("QLAB_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("QLAB_EDGAR_CONTACT", "qlab tests <tests@example.org>")
    seen = []

    class Resp:
        def __init__(self, body):
            self._b = json.dumps(body).encode()

        def read(self):
            return self._b

        def close(self):
            pass

    def urlopen(request, timeout):
        seen.append(request)
        url = request.full_url
        assert "qlab tests" in request.get_header("User-agent")
        if url.endswith("company_tickers.json"):
            return Resp(TICKERS)
        if "submissions/CIK0000320193" in url:
            return Resp(SUBMISSIONS)
        raise AssertionError(url)

    monkeypatch.setattr(edgar.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(edgar, "load_news_sources",
                        lambda: {"edgar": {"issuers": {"QQQ": ["AAPL"]}}})
    return seen


def test_kept_forms_become_primary_records_dated_by_acceptance(sec):
    as_of = datetime(2026, 8, 28, tzinfo=timezone.utc)
    items = edgar.fetch(as_of, ("QQQ",))
    forms = {i.headline.split(" — ")[0] for i in items}
    assert forms == {"8-K", "10-Q"}, "Form 4 is not a kept form"
    eight_k = next(i for i in items if i.headline.startswith("8-K"))
    assert eight_k.published == "2026-08-27T20:31:00+00:00"
    assert eight_k.source == "SEC EDGAR"
    assert eight_k.tickers == ("QQQ",)
    assert "Items 2.02, 9.01" in eight_k.summary
    assert eight_k.url.endswith("/0000320193/000032019326000090/a8k.htm")
    assert eight_k.provider == "edgar"


def test_the_provider_refuses_without_a_contact(monkeypatch):
    monkeypatch.delenv("QLAB_EDGAR_CONTACT", raising=False)
    with pytest.raises(RuntimeError, match="QLAB_EDGAR_CONTACT"):
        edgar.fetch(datetime(2026, 8, 28, tzinfo=timezone.utc), ("SPY",))


def test_the_cik_map_is_cached_and_a_fund_ticker_maps_itself(sec, tmp_path):
    first = edgar.cik_map()
    assert first["SPY"] == "0000884394" and first["AAPL"] == "0000320193"
    assert (tmp_path / "news_cache" / "company_tickers.json").exists()
    fetched_before = len(sec)
    edgar.cik_map()
    assert len(sec) == fetched_before, "the second read is the cache"
