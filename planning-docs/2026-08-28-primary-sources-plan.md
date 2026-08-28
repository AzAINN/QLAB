# Primary Sources and the Qualitative Matrix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Feed the desk's qualitative lane primary documents and independent publishers (SEC EDGAR, official macro releases, GDELT) through installable, shareable providers, then turn the grounded record into a deterministic qualitative matrix that reaches the optimizer only as bounded, mean-pinned risk views through the catalog.

**Architecture:** New providers are new entries in the existing `PROVIDERS` registry behind the unchanged `ProviderFetch` contract, stackable via `QLAB_NEWS_PROVIDERS`, discoverable from third-party packages through a `qlab.news.providers` entry-point group. The matrix is a point-in-time table of record properties (no sign, no direction) persisted as a run; explicit rules map it to the existing `VolView`/`CorrView`/`TailView` types; `moments.condition` yields a lineage-bearing moment set consumed by a catalog entry that enters at stage `research` and earns `operational` through the ablation.

**Tech Stack:** Python 3.10+ stdlib (`urllib`, `json`, `xml.etree`, `importlib.metadata`), existing `qlab.news.*`, `qlab.core.views`, DuckDB registry, pytest (offline, monkeypatched `urlopen`).

**Spec:** `planning-docs/2026-08-28-primary-sources-design.md`

## Global Constraints

- Tests never open `.lab/registry.duckdb`; use `Registry(":memory:")` and monkeypatch `urllib.request.urlopen`. The suite must pass with no network.
- No new runtime dependency: first-party providers are stdlib-only. `pip install qlab[news]` stays `feedparser>=6.0`.
- One DuckDB writer: providers and the matrix never open the registry; the owner session does, under its lock.
- Fail loud: a dead provider member is reported per member, never absorbed into a smaller window.
- Point-in-time: EDGAR records date by `acceptanceDateTime` (UTC); anything at/after `as_of` is dropped by `ground()` and counted.
- No signed or directional qualitative column, and no view type that can carry a return.
- Run only the touched test modules per task; the full suite runs once at the end of each stream.
- Commit messages: imperative, `type(scope): …`, no AI-attribution trailers.
- Resolve files through `qlab/paths.py` (`data_path`, `state_path`) — never `Path(__file__).parents[...]`.

---

## File Structure

**Stream D**
- `qlab/news/feed.py` — modify: `fetch_news_stacked`, `parse_provider_stack`, `load_plugin_providers`.
- `qlab/news/providers/__init__.py` — create: first-party registration (`register_first_party()`).
- `qlab/news/providers/edgar.py` — create: CIK map, submissions fetch, `fetch(as_of, universe)`.
- `qlab/news/providers/macro.py` — create: primary RSS set + `upcoming(as_of)` calendar.
- `qlab/news/providers/gdelt.py` — create: DOC API queries from keyword rules.
- `qlab/news/archive.py` — modify: `MACRO_LANE_PROVIDERS` widens.
- `qlab/news/check.py` — modify: per-member report.
- `qlab/ui/server.py` — modify: `news_provider_for` returns a stack; `fetch_desk_news` / `archive_desk_news` loop per member; `/api/news/upcoming`.
- `qlab/autopilot/cli.py` — modify: `news-check --provider` accepts a stack.
- `configs/news_sources.yaml` — modify: `edgar:`, `gdelt:`, `calendar:` sections.
- `pyproject.toml` — modify: ship `configs/news_sources.yaml`; drop stale `index.html` package-data.
- Tests: `tests/test_news_stack.py`, `tests/test_news_edgar.py`, `tests/test_news_macro.py`, `tests/test_news_gdelt.py`, extend `tests/test_ui.py`.

**Stream E**
- `qlab/news/matrix.py` — create: `QualitativeMatrix`, `build_matrix(archive_rows, claims, universe, as_of, calendar)`.
- `qlab/research/matrix_views.py` — create: `views_from_matrix(matrix, baseline) -> list[dict]`.
- `qlab/core/moments.py` — modify: `condition(moment_set, probabilities)`.
- `qlab/mcp/quant_lab.py` — modify: `research.qualitative_matrix`, `moments.condition` tools; provenance gate accepts `source_claims`.
- `qlab/algorithms/catalog.py` — modify: `views_conditioned_min_variance` at stage `research`.
- `qlab/governance/referee.py` — modify: conditioned-moment lineage check.
- `configs/specs/ablation_v1.yaml` — modify: the new arm.
- `agents/atlas.md`, `agents/moments-analyst.md` — modify: the read tool; then `python -m qlab.agents.loader sync`.
- Tests: `tests/test_qualitative_matrix.py`, `tests/test_matrix_views.py`, `tests/test_moments_condition.py`, extend `tests/test_referee.py`, `tests/test_algorithms.py`.

---

## Stream D — primary sources

### Task 1: Ship the sources config and discover plugin providers

**Files:**
- Modify: `pyproject.toml:90-97`
- Modify: `qlab/news/feed.py:75-80` (registry) and `:742-750` (registration)
- Test: `tests/test_news_stack.py`

**Interfaces:**
- Produces: `qlab.news.feed.load_plugin_providers() -> dict[str, ProviderFetch]` (idempotent; merges into `PROVIDERS`); entry-point group name `"qlab.news.providers"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_news_stack.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_news_stack.py -q`
Expected: FAIL — `AttributeError: module 'qlab.news.feed' has no attribute 'load_plugin_providers'`

- [ ] **Step 3: Implement plugin discovery**

In `qlab/news/feed.py`, after the `PROVIDERS` declaration:

```python
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
```

And in `pyproject.toml` replace the two blocks:

```toml
[tool.setuptools.package-data]
# (the web client is retired; nothing to ship here)

[tool.setuptools.data-files]
"share/qlab" = ["mandate.yaml"]
"share/qlab/configs" = ["configs/universe.yaml", "configs/news_sources.yaml"]
"share/qlab/configs/specs" = ["configs/specs/*.yaml"]
"share/qlab/agents" = ["agents/*.md"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_news_stack.py -q`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml qlab/news/feed.py tests/test_news_stack.py
git commit -m "feat(news): discover plugin providers; ship news_sources.yaml in the wheel"
```

### Task 2: Stacked providers

**Files:**
- Modify: `qlab/news/feed.py:80-130` (`fetch_news`) — add `parse_provider_stack`, `fetch_news_stacked`, `StackedWindow`.
- Test: `tests/test_news_stack.py`

**Interfaces:**
- Produces: `parse_provider_stack(value: str | None) -> tuple[str, ...]` (splits `QLAB_NEWS_PROVIDERS` or falls back to `QLAB_NEWS_PROVIDER` or `("synthetic",)`); `fetch_news_stacked(as_of, universe, providers, lookback_hours=48) -> StackedWindow` with `.items: list[NewsItem]` (merged, sorted like `fetch_news`) and `.outcomes: dict[str, str]` (`"ok"` or the error sentence).

- [ ] **Step 1: Write the failing tests**

```python
from datetime import datetime, timezone

from qlab.news import feed
from qlab.news.feed import NewsItem


def _item(provider, source, headline, published="2026-08-27T12:00:00+00:00"):
    return NewsItem(source=source, published=published, headline=headline,
                    summary="", url=f"https://x/{headline}", tickers=("SPY",),
                    provider=provider)


def test_the_stack_is_parsed_from_the_plural_then_the_singular(monkeypatch):
    monkeypatch.setenv("QLAB_NEWS_PROVIDERS", "alpaca, edgar ,gdelt")
    assert feed.parse_provider_stack(None) == ("alpaca", "edgar", "gdelt")
    monkeypatch.delenv("QLAB_NEWS_PROVIDERS")
    monkeypatch.setenv("QLAB_NEWS_PROVIDER", "rss")
    assert feed.parse_provider_stack(None) == ("rss",)
    assert feed.parse_provider_stack("edgar") == ("edgar",)
    monkeypatch.delenv("QLAB_NEWS_PROVIDER")
    assert feed.parse_provider_stack(None) == ("synthetic",)


def test_a_stack_merges_members_and_reports_each_outcome(monkeypatch):
    monkeypatch.setitem(feed.PROVIDERS, "one", lambda a, u: [_item("one", "A", "h1")])
    monkeypatch.setitem(feed.PROVIDERS, "two", lambda a, u: [_item("two", "B", "h2")])

    def dead(a, u):
        raise RuntimeError("feed X is unavailable")

    monkeypatch.setitem(feed.PROVIDERS, "dead", dead)
    window = feed.fetch_news_stacked(
        datetime(2026, 8, 28, tzinfo=timezone.utc), ("SPY",), ("one", "two", "dead"))
    assert {i.provider for i in window.items} == {"one", "two"}
    assert window.outcomes["one"] == "ok" and window.outcomes["two"] == "ok"
    assert "unavailable" in window.outcomes["dead"]


def test_a_stack_of_all_dead_members_raises(monkeypatch):
    import pytest

    def dead(a, u):
        raise RuntimeError("down")

    monkeypatch.setitem(feed.PROVIDERS, "dead", dead)
    with pytest.raises(RuntimeError, match="every provider in the stack failed"):
        feed.fetch_news_stacked(
            datetime(2026, 8, 28, tzinfo=timezone.utc), ("SPY",), ("dead",))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_news_stack.py -q`
Expected: FAIL — `AttributeError: ... has no attribute 'parse_provider_stack'`

- [ ] **Step 3: Implement the stack**

In `qlab/news/feed.py`:

```python
@dataclass(frozen=True)
class StackedWindow:
    """Every member's records, and what each member said about fetching."""
    items: list[NewsItem]
    outcomes: dict[str, str]           # provider -> "ok" | error sentence
    providers: tuple[str, ...]


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
    for name in providers:
        try:
            got = fetch_news(as_of, universe, lookback_hours=lookback_hours,
                             provider=name)
        except Exception as exc:
            outcomes[name] = str(exc)
            continue
        outcomes[name] = "ok"
        items.extend(got)
    if not any(v == "ok" for v in outcomes.values()):
        raise RuntimeError(
            "every provider in the stack failed: "
            + "; ".join(f"{k}: {v}" for k, v in outcomes.items()))
    items.sort(key=lambda i: (i.published, i.source), reverse=True)
    return StackedWindow(items=items, outcomes=outcomes, providers=tuple(providers))
```

(`fetch_news` sorts by `published` desc then `source` asc; keep the stack's order rule identical by reusing the same key — read `fetch_news`'s sort and copy it verbatim if it differs from the line above.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_news_stack.py -q`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add qlab/news/feed.py tests/test_news_stack.py
git commit -m "feat(news): a stack of providers, each reported, merged into one window"
```

### Task 3: The EDGAR provider

**Files:**
- Create: `qlab/news/providers/__init__.py`, `qlab/news/providers/edgar.py`
- Modify: `qlab/news/feed.py:742` (register), `qlab/news/archive.py:65` (`MACRO_LANE_PROVIDERS`), `configs/news_sources.yaml` (`edgar:` section)
- Test: `tests/test_news_edgar.py`

**Interfaces:**
- Produces: `qlab.news.providers.edgar.fetch(as_of: datetime, universe: tuple[str, ...]) -> list[NewsItem]`; `cik_map() -> dict[str, str]` (ticker → 10-digit CIK, cached under `state_path("news_cache", "company_tickers.json")` for 7 days); `KEPT_FORMS = ("8-K", "10-Q", "10-K", "N-PORT", "N-CSR", "13F-HR")`.
- Consumes: `NewsItem` from Task 2's module; `load_news_sources()` from `feed.py`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_news_edgar.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_news_edgar.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'qlab.news.providers'`

- [ ] **Step 3: Implement the provider**

`qlab/news/providers/__init__.py`:

```python
"""First-party news providers: stdlib-only, keyless, registered by name."""

from __future__ import annotations


def register_first_party(registry: dict) -> None:
    """Add the first-party providers to ``feed.PROVIDERS``.

    Called from ``feed.py`` at import; imported lazily there to keep the
    provider modules from importing ``feed`` at module load (a cycle).
    """
    from qlab.news.providers import edgar, gdelt, macro

    registry.update({"edgar": edgar.fetch, "macro": macro.fetch, "gdelt": gdelt.fetch})
```

`qlab/news/providers/edgar.py`:

```python
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
_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}/{doc}"
_CACHE_TTL = timedelta(days=7)
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
    request = urllib.request.Request(
        url, headers={"User-Agent": f"qlab-news/0.1 {_contact()}",
                      "Accept-Encoding": "gzip, deflate"})
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
        out[name] = (name.upper(), *listed)
    return out


def fetch(as_of: datetime, universe: tuple[str, ...]) -> list[NewsItem]:
    _contact()
    ciks = cik_map()
    window_start = as_of - timedelta(hours=72)
    items: list[NewsItem] = []
    seen: set[str] = set()
    for fund, issuers in _issuers_for(universe).items():
        for ticker in issuers:
            cik = ciks.get(ticker)
            if cik is None or cik in seen:
                continue
            seen.add(cik)
            sub = _get_json(_SUBMISSIONS_URL.format(cik=cik))
            recent = (sub.get("filings") or {}).get("recent") or {}
            name = sub.get("name") or ticker
            rows = zip(recent.get("form", []), recent.get("acceptanceDateTime", []),
                       recent.get("accessionNumber", []), recent.get("primaryDocument", []),
                       recent.get("items", []) or [""] * len(recent.get("form", [])))
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
                    url=_ARCHIVE_URL.format(cik_int=int(cik), acc=acc.replace("-", ""), doc=doc),
                    tickers=(fund,),
                    provider="edgar",
                ))
    return items
```

In `feed.py`, replace the registration block:

```python
PROVIDERS.update({"synthetic": _fetch_synthetic, "rss": _fetch_rss, "alpaca": _fetch_alpaca})
from qlab.news.providers import register_first_party  # noqa: E402  (after PROVIDERS exists)
register_first_party(PROVIDERS)
```

In `archive.py`: `MACRO_LANE_PROVIDERS = frozenset({"alpaca", "edgar", "macro", "gdelt"})`.

In `configs/news_sources.yaml`, append:

```yaml
# SEC EDGAR: the funds themselves always; the issuers below are the operator's
# curated constituent list per ETF — phase-one holdings knowledge, shared as
# data. Requires QLAB_EDGAR_CONTACT in .env (the SEC asks who is calling).
edgar:
  issuers:
    QQQ: [AAPL, MSFT, NVDA, AMZN, META, AVGO, GOOGL, TSLA]
    SPY: [AAPL, MSFT, NVDA, AMZN, META, BRK-B, GOOGL, JPM]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_news_edgar.py tests/test_news.py tests/test_news_grounding.py -q`
Expected: all passed (the grounding tests prove `SEC EDGAR` is primary-tier: add one assertion `assert source_tier("SEC EDGAR") == "primary"` to `tests/test_news_grounding.py`).

- [ ] **Step 5: Commit**

```bash
git add qlab/news/providers configs/news_sources.yaml qlab/news/feed.py qlab/news/archive.py tests/test_news_edgar.py tests/test_news_grounding.py
git commit -m "feat(news): SEC EDGAR as a primary source, dated by acceptance time"
```

### Task 4: The macro provider and the release calendar

**Files:**
- Create: `qlab/news/providers/macro.py`
- Modify: `configs/news_sources.yaml` (`macro:` feeds, `calendar:`), `qlab/ui/server.py` (`GET /api/news/upcoming`)
- Test: `tests/test_news_macro.py`, extend `tests/test_ui.py`

**Interfaces:**
- Produces: `macro.fetch(as_of, universe) -> list[NewsItem]` (primary RSS via `feed._fetch_rss_feeds(feeds, as_of, universe)` — extract that helper from `_fetch_rss` so both share the parser); `macro.upcoming(as_of: datetime, horizon_days: int = 14) -> list[dict]` with keys `name, when (ISO), tickers, source`.
- Consumes: `feed.load_news_sources()`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_news_macro.py
from datetime import datetime, timezone

from qlab.news.providers import macro

CONFIG = {
    "macro": {"feeds": [{"name": "BLS", "url": "https://www.bls.gov/feed/bls_latest.rss",
                         "tickers": ["TIP", "BNDW"]}]},
    "calendar": [
        {"name": "FOMC statement", "when": "2026-09-17T18:00:00+00:00",
         "tickers": ["BNDW", "TLT"], "source": "Federal Reserve"},
        {"name": "CPI", "when": "2026-08-12T12:30:00+00:00",
         "tickers": ["TIP"], "source": "BLS"},
    ],
}


def test_upcoming_lists_only_what_is_ahead_inside_the_horizon(monkeypatch):
    monkeypatch.setattr(macro, "load_news_sources", lambda: CONFIG)
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    ahead = macro.upcoming(now, horizon_days=14)
    assert [e["name"] for e in ahead] == ["FOMC statement"]
    assert ahead[0]["days_ahead"] == 7
    assert macro.upcoming(now, horizon_days=3) == []


def test_macro_records_are_primary_tier(monkeypatch):
    from qlab.news.grounding import source_tier
    monkeypatch.setattr(macro, "load_news_sources", lambda: CONFIG)
    seen = []

    def fake_feeds(feeds, as_of, universe):
        seen.append([f["name"] for f in feeds])
        from qlab.news.feed import NewsItem
        return [NewsItem(source="BLS", published="2026-09-09T12:30:00+00:00",
                         headline="CPI rose 0.2% in August", summary="", url="https://bls.gov/x",
                         tickers=("TIP",), provider="macro")]

    monkeypatch.setattr(macro, "_fetch_rss_feeds", fake_feeds)
    items = macro.fetch(datetime(2026, 9, 10, tzinfo=timezone.utc), ("TIP",))
    assert seen == [["BLS"]]
    assert source_tier(items[0].source) == "primary"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_news_macro.py -q`
Expected: FAIL — `ImportError: cannot import name 'macro'`

- [ ] **Step 3: Implement**

First, in `feed.py`, split the body of `_fetch_rss` so that the loop over feeds lives in `def _fetch_rss_feeds(feeds: list[dict], as_of: datetime, universe: tuple[str, ...]) -> list[NewsItem]` and `_fetch_rss` becomes `return _fetch_rss_feeds(load_news_sources()["feeds"], _as_of, universe)`. (Move the code, change nothing in it; `tests/test_news.py` pins the parser.)

`qlab/news/providers/macro.py`:

```python
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
```

`configs/news_sources.yaml`, append:

```yaml
macro:
  feeds:
    - name: BLS
      url: https://www.bls.gov/feed/bls_latest.rss
      tickers: [TIP, BNDW]
    - name: BEA
      url: https://apps.bea.gov/rss/rss.xml
      tickers: [ACWI, BNDW]
    - name: US Treasury
      url: https://home.treasury.gov/news/press-releases/rss
      tickers: [TLT, IEF, BNDW]

# Scheduled releases. Dated in the future on purpose and never served as
# news — they are what Atlas reads as "what is coming". Maintain per quarter.
calendar:
  - {name: FOMC statement, when: "2026-09-17T18:00:00+00:00", tickers: [BNDW, TLT, IEF], source: Federal Reserve}
  - {name: CPI (August), when: "2026-09-11T12:30:00+00:00", tickers: [TIP, BNDW], source: BLS}
  - {name: Employment situation (August), when: "2026-09-04T12:30:00+00:00", tickers: [BNDW, ACWI], source: BLS}
```

`qlab/ui/server.py`, in `handle_api`'s GET arms:

```python
    if method == "GET" and path == "/api/news/upcoming":
        from qlab.news.providers.macro import upcoming
        return 200, {"upcoming": upcoming(datetime.now(timezone.utc))}
```

Add to `tests/test_ui.py`:

```python
def test_upcoming_releases_are_served_and_dated(session, monkeypatch):
    from qlab.news.providers import macro
    monkeypatch.setattr(macro, "load_news_sources", lambda: {"calendar": [
        {"name": "FOMC", "when": "2999-01-01T18:00:00+00:00", "tickers": ["TLT"]}]})
    status, out = handle_api(session, "GET", "/api/news/upcoming", {}, {})
    assert status == 200 and out["upcoming"] == [] , "beyond the 14-day horizon"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_news_macro.py tests/test_news.py tests/test_ui.py -q -k "macro or upcoming or rss"`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add qlab/news/providers/macro.py qlab/news/feed.py configs/news_sources.yaml qlab/ui/server.py tests/test_news_macro.py tests/test_ui.py
git commit -m "feat(news): official releases as a primary provider, and the calendar of what is ahead"
```

### Task 5: The GDELT provider

**Files:**
- Create: `qlab/news/providers/gdelt.py`
- Modify: `configs/news_sources.yaml` (`gdelt:` rules)
- Test: `tests/test_news_gdelt.py`

**Interfaces:**
- Produces: `gdelt.fetch(as_of, universe) -> list[NewsItem]`; `source` is the article's domain; `provider = "gdelt"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_news_gdelt.py
import json
from datetime import datetime, timezone

from qlab.news.providers import gdelt

ARTICLES = {"articles": [
    {"url": "https://www.reuters.com/markets/gold-x", "title": "Gold climbs as yields fall",
     "seendate": "20260827T140000Z", "domain": "reuters.com", "language": "English"},
    {"url": "https://www.ft.com/content/y", "title": "Bullion buyers return",
     "seendate": "20260827T150000Z", "domain": "ft.com", "language": "English"},
    {"url": "https://example.fr/z", "title": "L'or monte", "seendate": "20260827T150000Z",
     "domain": "example.fr", "language": "French"},
]}


def test_articles_map_by_rule_and_carry_their_domain_as_source(monkeypatch):
    monkeypatch.setattr(gdelt, "load_news_sources", lambda: {"gdelt": {"rules": [
        {"query": "gold OR bullion", "tickers": ["GLD"]}]}})
    calls = []

    class Resp:
        def read(self): return json.dumps(ARTICLES).encode()
        def close(self): pass

    def urlopen(request, timeout):
        calls.append(request.full_url)
        return Resp()

    monkeypatch.setattr(gdelt.urllib.request, "urlopen", urlopen)
    items = gdelt.fetch(datetime(2026, 8, 28, tzinfo=timezone.utc), ("GLD", "SPY"))
    assert len(calls) == 1 and "sourcelang:english" in calls[0]
    assert {i.source for i in items} == {"reuters.com", "ft.com"}, "English only"
    assert all(i.tickers == ("GLD",) and i.provider == "gdelt" for i in items)
    assert items[0].published == "2026-08-27T15:00:00+00:00"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_news_gdelt.py -q`
Expected: FAIL — `ImportError: cannot import name 'gdelt'`

- [ ] **Step 3: Implement**

```python
"""GDELT as the many-publisher secondary source.

Its job is independence: ``Claim.corroborated`` needs two distinct
publishers for a secondary story, and one wire cannot supply that. Each
article's domain is its ``source``, so two outlets on one story count as
two. Keyword rules per ticker come from ``news_sources.yaml`` — shared as
data, like every other source list.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from qlab.news.feed import NewsItem, load_news_sources

_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
_TIMEOUT_S = 10


def fetch(as_of: datetime, universe: tuple[str, ...]) -> list[NewsItem]:
    rules = (load_news_sources().get("gdelt") or {}).get("rules") or []
    wanted = {t.upper() for t in universe}
    items: list[NewsItem] = []
    for rule in rules:
        tickers = tuple(t for t in rule["tickers"] if t.upper() in wanted)
        if not tickers:
            continue
        query = f"({rule['query']}) sourcelang:english"
        params = urllib.parse.urlencode({
            "query": query, "mode": "artlist", "format": "json",
            "maxrecords": 75, "timespan": "48h", "sort": "datedesc"})
        request = urllib.request.Request(
            f"{_DOC_URL}?{params}",
            headers={"User-Agent": "qlab-news/0.1 (+https://github.com/qlab)"})
        response = urllib.request.urlopen(request, timeout=_TIMEOUT_S)
        try:
            payload = json.loads(response.read())
        finally:
            response.close()
        for art in payload.get("articles", []):
            if str(art.get("language", "")).lower() != "english":
                continue
            seen = datetime.strptime(art["seendate"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            if seen >= as_of or seen < as_of - timedelta(hours=48):
                continue
            items.append(NewsItem(
                source=str(art.get("domain") or "gdelt"),
                published=seen.isoformat(),
                headline=str(art.get("title") or "").strip(),
                summary="",
                url=str(art.get("url") or ""),
                tickers=tickers,
                provider="gdelt",
            ))
    return items
```

`configs/news_sources.yaml`, append:

```yaml
gdelt:
  rules:
    - {query: "gold OR bullion", tickers: [GLD]}
    - {query: "crude oil OR OPEC", tickers: [USO, GSG, DBC]}
    - {query: "treasury yields OR federal reserve", tickers: [TLT, IEF, BNDW]}
    - {query: "emerging markets debt", tickers: [EMB, EEM]}
    - {query: "real estate investment trust", tickers: [VNQ, RWO]}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_news_gdelt.py -q`
Expected: 1 passed

- [ ] **Step 5: Commit**

```bash
git add qlab/news/providers/gdelt.py configs/news_sources.yaml tests/test_news_gdelt.py
git commit -m "feat(news): GDELT as the many-publisher secondary source"
```

### Task 6: The owner reads the stack, archives per member, and news-check reports each

**Files:**
- Modify: `qlab/ui/server.py:2918-2945` (`news_provider_for`), `:2564-2600` (`fetch_desk_news`), `archive_desk_news`
- Modify: `qlab/news/check.py:15-83`, `qlab/autopilot/cli.py` (`news-check --provider`)
- Test: extend `tests/test_ui.py`, `tests/test_news.py`

**Interfaces:**
- `news_provider_for(offline) -> tuple[str, ...]` (a stack; offline is `("synthetic",)`); `fetch_desk_news` uses `fetch_news_stacked` and returns `{"items", "outcomes", "providers", ...}`; `archive_desk_news` loops members, one `ArchiveBatch` and one `news_archive` event each; the snapshot's news block gains `"providers"` and `"outcomes"`.
- `check_news(universe, provider=None)` returns `{"members": {name: report}}` plus the existing top-level fields for a stack of one.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ui.py (append)
def test_the_desk_reads_a_stack_and_archives_each_member(session, monkeypatch):
    from qlab.news import feed
    from qlab.news.feed import NewsItem
    monkeypatch.setenv("QLAB_NEWS_PROVIDERS", "one,two")

    def mk(provider, src):
        return lambda a, u: [NewsItem(source=src, published="2026-08-27T12:00:00+00:00",
                                      headline=f"{src} story", summary="", url=f"https://x/{src}",
                                      tickers=("SPY",), provider=provider)]

    monkeypatch.setitem(feed.PROVIDERS, "one", mk("one", "SEC EDGAR"))
    monkeypatch.setitem(feed.PROVIDERS, "two", mk("two", "reuters.com"))
    assert session.news_provider_for(False) == ("one", "two")
    window = session.fetch_desk_news(False)
    assert window["outcomes"] == {"one": "ok", "two": "ok"}
    result = session.archive_desk_news(window)
    events = [e for e in session.registry.read_events_of_kind("news_archive", 10)]
    assert {e["payload"]["provider"] for e in events} == {"one", "two"}
    assert result["stored"] == 2
```

```python
# tests/test_news.py (append)
def test_news_check_reports_each_member_of_a_stack(monkeypatch):
    from qlab.news import check, feed
    from qlab.news.feed import NewsItem
    monkeypatch.setitem(feed.PROVIDERS, "good", lambda a, u: [NewsItem(
        source="BLS", published="2026-08-27T12:00:00+00:00", headline="h", summary="",
        url="https://bls.gov/x", tickers=("TIP",), provider="good")])
    monkeypatch.setitem(feed.PROVIDERS, "bad", lambda a, u: (_ for _ in ()).throw(RuntimeError("down")))
    report = check.check_news(["TIP"], provider="good,bad")
    assert report["members"]["good"]["ok"] is True
    assert report["members"]["bad"]["ok"] is False and "down" in report["members"]["bad"]["error"]
    assert report["ok"] is True, "one living member is a record"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_ui.py -k stack -q; .venv/bin/python -m pytest tests/test_news.py -k members -q`
Expected: FAIL — `news_provider_for` returns a `str`; `check_news` has no `members`.

- [ ] **Step 3: Implement**

`news_provider_for`: return `("synthetic",)` when offline; else `parse_provider_stack(None)` when either env is set; else `("alpaca",)` when a credential resolves; else `("synthetic",)`. Every caller that compared the old string compares `stack[0]` or joins — grep `news_provider_for(` and update each site (there are three: `fetch_desk_news`, `compose_desk_read`'s label, and `news_check`'s footer).

`fetch_desk_news`: replace the `fetch_news(...)` call with `fetch_news_stacked(as_of, universe, providers, lookback_hours=...)` and return `{"items": window.items, "outcomes": window.outcomes, "providers": list(window.providers), "provider": ",".join(window.providers), "as_of": ..., "error": None}`; a raised `RuntimeError` (every member dead) keeps the existing error path.

`archive_desk_news(window)`: group `window["items"]` by `item.provider`, build and record one `ArchiveBatch` per group with `provider=<member>`, record one `news_archive` event per group carrying `outcome: window["outcomes"][member]`, and return `{"stored": total, "per_provider": {...}}`.

`check_news`: parse `provider` with `parse_provider_stack`; for each member run the existing single-provider body (extract it as `_check_one(name, universe, lookback_hours, now, creds) -> dict`) into `report["members"][name]`; `report["ok"] = any(m["ok"] for m in members.values())`; keep the top-level fields as the first member's for a stack of one. `render()` prints one block per member.

`cli.py`: the `news-check` parser's `--provider` help becomes `"a provider, or a comma-separated stack"`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_ui.py tests/test_news.py tests/test_news_stack.py -q`
Expected: all passed

- [ ] **Step 5: Docs and commit**

Update `docs/news-setup.md`: the provider table gains `edgar`, `macro`, `gdelt`; a "Stacking providers" section shows `QLAB_NEWS_PROVIDERS=alpaca,edgar,macro,gdelt` and `QLAB_EDGAR_CONTACT`; the "Checking it" section shows the per-member output. README extras table: `news` row reads "the RSS, EDGAR, macro and GDELT providers (stdlib; feedparser only for RSS)".

```bash
git add qlab/ui/server.py qlab/news/check.py qlab/autopilot/cli.py docs/news-setup.md README.md tests/test_ui.py tests/test_news.py
git commit -m "feat(news): the owner reads a provider stack and archives each member; news-check reports per member"
```

- [ ] **Step 6: Stream D full run**

Run: `.venv/bin/python -m pytest -q -p no:warnings` — expected: all passed. Then restart the owner (`qlab --restart=runtime --yes`), set `QLAB_NEWS_PROVIDERS=alpaca,edgar,macro,gdelt` and `QLAB_EDGAR_CONTACT` in `.env`, run `qlab news-check`, and record the live per-member outcome in `planning-docs/2026-08-28-primary-sources-completion.md` (invariant 11: a member that does not answer is recorded as such).

---

## Stream E — the qualitative matrix

### Task 7: The matrix

**Files:**
- Create: `qlab/news/matrix.py`
- Modify: `qlab/ui/server.py` (`qualitative_matrix()` method, `GET /api/research/qualitative`, run kind `qualitative_matrix`)
- Test: `tests/test_qualitative_matrix.py`, extend `tests/test_ui.py`

**Interfaces:**
- Produces: `build_matrix(claims: list[Claim], universe: list[str], as_of: str, upcoming: list[dict]) -> QualitativeMatrix` with `.rows: dict[str, MatrixRow]`, `.as_of`, `.window_hash`; `MatrixRow(ticker, coverage, publishers, corroborated, primary_docs, days_to_next_release: int | None, claim_keys: tuple[str, ...])`; `QualitativeMatrix.to_dict()`.
- Consumes: `Claim` from `qlab.news.grounding` (fields `key, tickers, sources, corroboration, tier, corroborated`), `macro.upcoming()`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_qualitative_matrix.py
from qlab.news.grounding import Claim
from qlab.news.matrix import build_matrix


def claim(key, tickers, sources, tier="secondary"):
    return Claim(key=key, headline=key, tickers=tuple(tickers), sources=tuple(sources),
                 item_hashes=(key,), corroboration=len(set(sources)),
                 earliest_published="2026-08-27T12:00:00+00:00", tier=tier)


def test_the_matrix_counts_record_properties_and_carries_no_sign():
    claims = [
        claim("fed-8k", ["TLT", "BNDW"], ["SEC EDGAR"], tier="primary"),
        claim("gold-rally", ["GLD"], ["reuters.com", "ft.com"]),
        claim("gold-take", ["GLD"], ["benzinga"]),
    ]
    m = build_matrix(claims, ["TLT", "BNDW", "GLD", "USO"], "2026-08-28",
                     upcoming=[{"name": "FOMC", "days_ahead": 7, "tickers": ["TLT"]}])
    assert m.rows["GLD"].coverage == 2
    assert m.rows["GLD"].publishers == 3
    assert m.rows["GLD"].corroborated == 1
    assert m.rows["GLD"].primary_docs == 0
    assert m.rows["TLT"].primary_docs == 1 and m.rows["TLT"].corroborated == 1
    assert m.rows["TLT"].days_to_next_release == 7
    assert m.rows["USO"].coverage == 0 and m.rows["USO"].days_to_next_release is None
    assert m.rows["GLD"].claim_keys == ("gold-rally", "gold-take")
    # No column is signed: the shape of the row is the whole contract.
    assert set(m.rows["GLD"].to_dict()) == {
        "ticker", "coverage", "publishers", "corroborated", "primary_docs",
        "days_to_next_release", "claim_keys"}


def test_the_window_hash_is_a_function_of_the_claims_alone():
    a = build_matrix([claim("k", ["SPY"], ["x"])], ["SPY"], "2026-08-28", upcoming=[])
    b = build_matrix([claim("k", ["SPY"], ["x"])], ["SPY"], "2026-08-29", upcoming=[])
    assert a.window_hash == b.window_hash
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_qualitative_matrix.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'qlab.news.matrix'`

- [ ] **Step 3: Implement**

```python
"""The qualitative matrix: what the record says about each name, as counts.

Rows are the universe; columns are properties of the *record* — how much
coverage, from how many publishers, how much of it corroborated, how many
primary documents, how far to the next scheduled release. Nothing here has a
sign. ``qualitative.py`` states the reason and it holds twice as hard for a
table that reaches the optimizer: a signed column is a return forecast with a
qualitative name. Every row carries the claim keys it was counted from, so a
cell is traceable to archive rows.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class MatrixRow:
    ticker: str
    coverage: int
    publishers: int
    corroborated: int
    primary_docs: int
    days_to_next_release: int | None
    claim_keys: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class QualitativeMatrix:
    as_of: str
    window_hash: str
    rows: dict[str, MatrixRow]

    def to_dict(self) -> dict:
        return {"as_of": self.as_of, "window_hash": self.window_hash,
                "rows": {k: r.to_dict() for k, r in self.rows.items()}}


def build_matrix(claims, universe, as_of: str, upcoming) -> QualitativeMatrix:
    rows: dict[str, MatrixRow] = {}
    ahead: dict[str, int] = {}
    for event in upcoming:
        for t in event.get("tickers", []):
            ahead[t] = min(ahead.get(t, 10**6), int(event["days_ahead"]))
    for ticker in universe:
        mine = [c for c in claims if ticker in c.tickers]
        publishers = {s for c in mine for s in c.sources}
        rows[ticker] = MatrixRow(
            ticker=ticker,
            coverage=len(mine),
            publishers=len(publishers),
            corroborated=sum(1 for c in mine if c.corroborated),
            primary_docs=sum(1 for c in mine if c.tier == "primary"),
            days_to_next_release=ahead.get(ticker),
            claim_keys=tuple(sorted(c.key for c in mine)),
        )
    material = "|".join(sorted(c.key for c in claims))
    return QualitativeMatrix(
        as_of=as_of,
        window_hash=hashlib.sha256(material.encode()).hexdigest()[:16],
        rows=rows,
    )
```

Owner: `UISession.qualitative_matrix(offline) -> dict` composes `ground(...)` over the cached desk window (the same call `desk_read` makes), calls `build_matrix(grounded.claims, universe, as_of, upcoming(...))`, logs a run of kind `qualitative_matrix` with `spec={"matrix": m.to_dict()}` **only when the window hash changed since the last logged matrix** (one row per window), and returns the dict. Route: `GET /api/research/qualitative` → `session.qualitative_matrix(off)`. Add the matrix to `atlas_context` under `"qualitative_matrix"` (rows only, no keys — the reasoner reads counts, not archive ids).

Test in `tests/test_ui.py`:

```python
def test_the_qualitative_matrix_is_served_and_logged_once_per_window(session):
    status, out = handle_api(session, "GET", "/api/research/qualitative", {}, {})
    assert status == 200 and set(out["rows"]) == set(session.mandate.universe_whitelist)
    first = [r for r in session.registry.list_runs(20) if r["kind"] == "qualitative_matrix"]
    handle_api(session, "GET", "/api/research/qualitative", {}, {})
    again = [r for r in session.registry.list_runs(20) if r["kind"] == "qualitative_matrix"]
    assert len(again) == len(first) == 1
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_qualitative_matrix.py tests/test_ui.py -k "matrix or qualitative" -q`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add qlab/news/matrix.py qlab/ui/server.py tests/test_qualitative_matrix.py tests/test_ui.py
git commit -m "feat(news): the qualitative matrix — record properties per name, no sign, one row per window"
```

### Task 8: Matrix to views, by rule

**Files:**
- Create: `qlab/research/matrix_views.py`
- Modify: `qlab/mcp/quant_lab.py:1233-1260` (`research.apply_views` provenance gate accepts `source_claims`), `_verify_view_provenance`
- Test: `tests/test_matrix_views.py`, extend `tests/test_views_wiring.py`

**Interfaces:**
- Produces: `views_from_matrix(matrix: QualitativeMatrix, baseline: dict[str, MatrixRow] | None, sleeves: dict[str, list[str]]) -> list[dict]` — view dicts in the exact schema `_validated_risk_views` accepts (`{"type": "tail", "ticker", "direction", "confidence", "source_claims": [...]}` and `{"type": "corr", "ticker_a", "ticker_b", "target_corr", "confidence", "source_claims"}`), confidence `<= 0.5`, at most three views.
- `_verify_view_provenance(views, excerpt, claim_keys: set[str] | None)` — a view carrying `source_claims` verifies against `claim_keys`; one carrying `source_quote` verifies against `excerpt` as today.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_matrix_views.py
from qlab.news.matrix import MatrixRow, QualitativeMatrix
from qlab.research.matrix_views import views_from_matrix


def row(t, coverage=0, publishers=0, corroborated=0, primary=0, keys=()):
    return MatrixRow(t, coverage, publishers, corroborated, primary, None, tuple(keys))


def matrix(rows):
    return QualitativeMatrix("2026-08-28", "w", {r.ticker: r for r in rows})


def test_excess_corroborated_primary_documents_fatten_the_tail_with_capped_confidence():
    now = matrix([row("TLT", 6, 3, 4, 4, keys=("a", "b", "c", "d"))])
    base = {"TLT": row("TLT", 2, 2, 1, 1)}
    views = views_from_matrix(now, base, sleeves={"rates": ["TLT", "IEF"]})
    tail = [v for v in views if v["type"] == "tail"]
    assert len(tail) == 1 and tail[0]["ticker"] == "TLT" and tail[0]["direction"] == "fatter"
    assert 0 < tail[0]["confidence"] <= 0.5
    assert tail[0]["source_claims"] == ["a", "b", "c", "d"]


def test_no_rule_emits_a_return_view_and_quiet_records_emit_nothing():
    quiet = matrix([row("GLD", 1, 1, 0, 0, keys=("k",))])
    assert views_from_matrix(quiet, None, sleeves={}) == []
    loud = matrix([row(t, 5, 4, 3, 3, keys=("k",)) for t in ["A", "B", "C", "D", "E"]])
    views = views_from_matrix(loud, None, sleeves={})
    assert len(views) <= 3
    assert {v["type"] for v in views} <= {"tail", "corr", "vol"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_matrix_views.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'qlab.research.matrix_views'`

- [ ] **Step 3: Implement**

```python
"""Matrix cells to risk views, by explicit bounded rule.

Two rules, both about *risk shape*, neither about direction, because the
view types cannot carry direction and the matrix has none to give:

* **Attention with documents behind it fattens the tail.** A name whose
  corroborated primary-document count exceeds its trailing baseline gets a
  ``fatter`` tail view, confidence scaled to the excess and capped at 0.5 —
  half of what the extractor role may claim, because a rule is not a
  reading.
* **Concentrated coverage couples a name to its sleeve.** When one name
  carries most of a sleeve's coverage, a correlation view raises its
  correlation to the sleeve's other names toward 0.6, capped at 0.4
  confidence — coverage concentration is what the absorption ratio sees from
  the price side, stated from the record side.

At most three views, the largest excess first: the KL budget downstream is
finite and a rule that emitted ten would spend it on noise.
"""

from __future__ import annotations

from qlab.news.matrix import MatrixRow, QualitativeMatrix

MAX_VIEWS = 3
TAIL_CAP = 0.5
CORR_CAP = 0.4
_CORR_TARGET = 0.6


def views_from_matrix(matrix: QualitativeMatrix, baseline, sleeves) -> list[dict]:
    candidates: list[tuple[float, dict]] = []
    base = baseline or {}
    for ticker, r in matrix.rows.items():
        b = base.get(ticker)
        prior = b.primary_docs if b else 0
        excess = r.primary_docs - prior
        if excess >= 2 and r.corroborated >= excess:
            confidence = min(TAIL_CAP, 0.15 * excess)
            candidates.append((excess, {
                "type": "tail", "ticker": ticker, "direction": "fatter",
                "confidence": round(confidence, 3), "source_claims": list(r.claim_keys)}))
    for sleeve, members in sleeves.items():
        rows = [matrix.rows[m] for m in members if m in matrix.rows]
        total = sum(x.coverage for x in rows)
        if total < 4:
            continue
        top = max(rows, key=lambda x: x.coverage)
        if top.coverage / total >= 0.75:
            for other in rows:
                if other.ticker == top.ticker:
                    continue
                candidates.append((top.coverage / total, {
                    "type": "corr", "ticker_a": top.ticker, "ticker_b": other.ticker,
                    "target_corr": _CORR_TARGET,
                    "confidence": round(min(CORR_CAP, top.coverage / total - 0.5), 3),
                    "source_claims": list(top.claim_keys)}))
    candidates.sort(key=lambda c: c[0], reverse=True)
    return [v for _, v in candidates[:MAX_VIEWS]]
```

Provenance gate in `quant_lab.py`: extend `_verify_view_provenance(canonical_views, excerpt, claim_keys=None)` — for a view with `source_claims`, every key must be in `claim_keys` (the current archive's claim keys, read via `st.registry`'s newest `qualitative_matrix` run); a view with neither field is refused: `"a view must carry source_quote or source_claims"`. `_validated_risk_views` accepts `source_claims: list[str]` as an alternative to `source_quote` in all three schemas.

Add to `tests/test_views_wiring.py`:

```python
def test_a_view_may_cite_archive_claims_instead_of_an_excerpt(session_or_state):
    # Build one qualitative_matrix run with claim key "k1", then apply a tail
    # view citing ["k1"] with dry=True — provenance_verified is True — and one
    # citing ["nope"], which is refused with "not in the archive".
    ...
```

(Write this test against the existing `test_views_wiring.py` fixtures — copy the fixture the file already uses for `research_apply_views` and assert `result["provenance_verified"] is True` for the good key and `pytest.raises(ValueError, match="not in the archive")` for the bad one.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_matrix_views.py tests/test_views_wiring.py tests/test_views.py -q`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add qlab/research/matrix_views.py qlab/mcp/quant_lab.py tests/test_matrix_views.py tests/test_views_wiring.py
git commit -m "feat(research): matrix cells to bounded risk views, provenance by archive claim"
```

### Task 9: Conditioned moments, through the catalog

**Files:**
- Modify: `qlab/core/moments.py` (`condition(ms: MomentSet, probabilities) -> MomentSet`), `qlab/mcp/quant_lab.py` (`moments.condition` tool; `research.qualitative_matrix` read tool), `qlab/algorithms/catalog.py` (entry), `qlab/governance/referee.py` (lineage check), `configs/specs/ablation_v1.yaml` (arm), `qlab/state/registry.py` (moment set lineage columns if absent)
- Modify: `agents/atlas.md`, `agents/moments-analyst.md` (tool lists), then `python -m qlab.agents.loader sync`
- Test: `tests/test_moments_condition.py`, extend `tests/test_referee.py`, `tests/test_algorithms.py`

**Interfaces:**
- `condition(ms, probabilities) -> MomentSet` — same `mean` object as `ms.mean` (asserted equal), covariance from `conditioned_moments(panel, probabilities)`, `provenance={"parent": ms.id, "views_run_id": ...}`.
- Tool `moments.condition(moment_set_id: str, views_run_id: str) -> {"moment_set_id", "kl_total", "parent"}` — refuses when the views run's `kl_total` exceeds its recorded `kl_budget`, and refuses on the **operational** path unless `catalog["views_conditioned_min_variance"].stage == "operational"` (the same in-code stage check `algorithms.solve` uses).
- Catalog: `AlgorithmSpec(id="views_conditioned_min_variance", category="allocation", stage="research", ...)`.
- Referee: a solve whose moment set has `provenance.views_run_id` FAILS unless that run exists and `kl_total <= kl_budget`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_moments_condition.py
import numpy as np

from qlab.core.moments import MomentSet, condition


def test_conditioning_moves_the_covariance_and_pins_every_mean():
    rng = np.random.default_rng(7)
    panel = rng.normal(size=(120, 3))
    ms = MomentSet.from_panel(panel, ["A", "B", "C"])         # use the file's constructor
    p = np.full(120, 1 / 120)
    p[:20] *= 3.0
    p /= p.sum()
    out = condition(ms, p, panel=panel, views_run_id="v1")
    assert np.allclose(out.mean, ms.mean), "a view can never move a mean"
    assert not np.allclose(out.cov, ms.cov)
    assert out.provenance["parent"] == ms.id and out.provenance["views_run_id"] == "v1"
```

```python
# tests/test_algorithms.py (append)
def test_views_conditioned_min_variance_is_visible_and_not_agent_runnable():
    from qlab.algorithms.catalog import CATALOG
    spec = CATALOG["views_conditioned_min_variance"]
    assert spec.stage == "research" and not spec.agent_runnable
```

```python
# tests/test_referee.py (append)
def test_a_conditioned_moment_set_without_its_views_run_fails_the_referee(registry_fixture):
    # Log a moment set whose provenance names a views_run_id that is not in
    # the registry; run the referee over a solution built on it; assert FAIL
    # with "views run" in the verdict's reasons.
    ...
```

(Fill the referee test from the file's existing verdict fixtures: it constructs a solution + moment set, calls the referee, and asserts on `verdict["status"]` and `verdict["reasons"]`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_moments_condition.py tests/test_algorithms.py tests/test_referee.py -q`
Expected: FAIL — `ImportError: cannot import name 'condition'`; `KeyError: 'views_conditioned_min_variance'`

- [ ] **Step 3: Implement**

`qlab/core/moments.py` — beside `estimate_moments`:

```python
def condition(ms: "MomentSet", probabilities, *, panel, views_run_id: str) -> "MomentSet":
    """A moment set whose covariance is the views-tilted one and whose mean is ``ms``'s.

    The mean is copied, not recomputed: ``conditioned_moments`` returns a
    tilted mean too, and the entire safety of the qualitative lane is that no
    view can move one. Re-asserted here as arithmetic rather than trusted.
    """
    from qlab.core.views import conditioned_moments

    _, cov = conditioned_moments(np.asarray(panel, dtype=float), np.asarray(probabilities))
    return replace(ms, id=f"{ms.id}:v:{views_run_id}", cov=cov,
                   provenance={**(ms.provenance or {}), "parent": ms.id,
                               "views_run_id": views_run_id})
```

(If `MomentSet` has no `provenance` field, add `provenance: dict = field(default_factory=dict)` and a `provenance VARCHAR` column to `moment_sets` with an `ALTER TABLE … ADD COLUMN IF NOT EXISTS` migration in `registry.py`, following the `atlas_tasks.origin` migration pattern.)

`quant_lab.py` — two tools beside `research_apply_views`:

```python
    def research_qualitative_matrix(as_of: str, universe: str = "core") -> dict:
        """The newest qualitative matrix for the window: counts per name, no sign."""
        st.budget.charge("research.qualitative_matrix")
        row = next((r for r in st.registry.list_runs(50) if r.get("kind") == "qualitative_matrix"), None)
        if row is None:
            return {"status": "never_built", "rows": {}}
        return {"status": "ok", **(row.get("spec") or {}).get("matrix", {})}

    def moments_condition(moment_set_id: str, views_run_id: str) -> dict:
        """Condition a moment set on a persisted views run. Research-stage until promoted."""
        st.budget.charge("moments.condition")
        from qlab.algorithms.catalog import CATALOG
        from qlab.core.moments import condition
        views = st.registry.get_run(views_run_id)
        if views is None or views.get("kind") != "views":
            raise ValueError(f"{views_run_id!r} is not a persisted views run")
        spec = views.get("spec") or {}
        if float(spec.get("kl_total", 0.0)) > float(spec.get("kl_budget", 0.0)):
            raise ValueError("the views run exceeds its own KL budget; relax a view")
        if CATALOG["views_conditioned_min_variance"].stage != "operational":
            raise PermissionError(
                "views-conditioned moments are research-stage: visible, not "
                "runnable in a governed solve until the catalog promotes them")
        ms = st.registry.get_moment_set(moment_set_id)
        out = condition(ms, np.asarray(spec["probabilities"]), panel=st.panel_for(ms),
                        views_run_id=views_run_id)
        return {"moment_set_id": st.registry.log_moment_set(out),
                "parent": moment_set_id, "kl_total": spec.get("kl_total")}
```

(`research_apply_views` with `persist=True` must persist `probabilities`, `kl_total` and `kl_budget` in its run spec — add those three keys where the run is logged.)

Catalog entry, beside `regime_min_variance`:

```python
    AlgorithmSpec(
        "views_conditioned_min_variance", "Views-conditioned minimum variance",
        "allocation", "research",
        "Minimum variance on a covariance tilted by archive-derived risk views "
        "(mean pinned). Research until the ablation shows it earns its place.",
        agent_tool=None),
```

Referee (`qlab/governance/referee.py`): where the moment set is read for the checked solution, add:

```python
    views_run_id = (ms.provenance or {}).get("views_run_id")
    if views_run_id:
        run = registry.get_run(views_run_id)
        if run is None or run.get("kind") != "views":
            reasons.append(f"conditioned on views run {views_run_id!r}, which is not in the registry")
        elif float((run.get("spec") or {}).get("kl_total", 0)) > float((run.get("spec") or {}).get("kl_budget", 0)):
            reasons.append("conditioned on a views run over its KL budget")
```

Ablation: add to `configs/specs/ablation_v1.yaml` an arm `views_conditioned_min_variance` with the same walk-forward settings as `regime_min_variance` and `views_source: qualitative_matrix`.

Agents: add `mcp__qlab__research.qualitative_matrix` to `agents/atlas.md` and `agents/moments-analyst.md` tool lists; do **not** add `moments.condition` to any role until the catalog entry is operational. Run `python -m qlab.agents.loader sync`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_moments_condition.py tests/test_algorithms.py tests/test_referee.py tests/test_views_wiring.py -q`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add qlab/core/moments.py qlab/mcp/quant_lab.py qlab/algorithms/catalog.py qlab/governance/referee.py configs/specs/ablation_v1.yaml qlab/state/registry.py agents .claude/agents .bob/personas tests
git commit -m "feat(research): views-conditioned moments through the catalog, research-stage, referee-checked"
```

- [ ] **Step 6: Stream E full run and the record**

Run: `.venv/bin/python -m pytest -q -p no:warnings` — expected: all passed. Run the ablation once offline: `qlab batch configs/specs/ablation_v1.yaml --offline`, and write `planning-docs/2026-08-28-qualitative-matrix-completion.md` with the arm's out-of-sample result beside HRP/ERC — **whatever it is**. Promotion to `operational` is a separate, evidence-bearing decision; this plan ends with the evidence gathered, not the promotion made.

---

## Self-review

- **Spec coverage:** D0 (contract, look-ahead, tiers, archive, fail-loud) → Tasks 2–6; D1 stack → Task 2 & 6; D2 installable/shareable → Task 1 (entry points, wheel data), Tasks 3–5 (stdlib, keyless, config as data); D3 providers → Tasks 3, 4, 5; E0 matrix → Task 7; E1 rules and provenance → Task 8; E2 catalog path, referee, ablation → Task 9. The N-PORT, LLM goal gate and signed-column exclusions have no task, by design.
- **Placeholders:** the referee test in Task 9 and the provenance test in Task 8 are sketched against fixtures the executor must read from the existing files — each names the file, the assertion, and the expected sentence.
- **Type consistency:** `NewsItem`, `StackedWindow`, `MatrixRow`, `QualitativeMatrix`, `condition()` signatures match across tasks; `source_claims` is spelled identically in Tasks 8 and 9; the stack env var is `QLAB_NEWS_PROVIDERS` everywhere.
