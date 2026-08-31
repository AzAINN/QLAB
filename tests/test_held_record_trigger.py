"""The `held_record_change` trigger: a held name's record moving is news.

The qualitative matrix is logged once per window on the owner's tick. This is
the rule that reads two of those windows and says when the change is worth a
human's attention: a name the book actually holds gaining a primary document,
or two corroborated claims. Everything here runs against an in-memory registry
with a stub session — no owner, no network, no `.lab/registry.duckdb`.
"""

from __future__ import annotations

import itertools
import threading

import pytest

from qlab.news.matrix import DESK_MATRIX_SOURCE
from qlab.operator.atlas import AtlasConfig, AtlasSupervisor
from qlab.operator.heartbeat import build_owner_tick
from qlab.state.registry import Registry


@pytest.fixture
def reg():
    r = Registry(":memory:")
    yield r
    r.close()


def _facts(**over):
    facts = {
        "universe": ["ACWI", "BNDW"],
        "data": {"provider": "synthetic", "blocked": False,
                 "eligible_for_paper_proposal": True},
        "portfolio": {"equity": 10000.0, "drawdown": 0.01,
                      "drawdown_tier": "none", "halted": False,
                      "gross_exposure": 1.0, "drift": 0.0},
        "regime": {"robust_state": "calm", "flip": False},
        "open_workflows": 0, "pending_approvals": 0,
    }
    facts.update(over)
    return facts


def _row(ticker: str, *, coverage: int = 3, publishers: int = 2,
         corroborated: int = 0, primary_docs: int = 0) -> dict:
    return {"ticker": ticker, "coverage": coverage, "publishers": publishers,
            "corroborated": corroborated, "primary_docs": primary_docs,
            "days_to_next_release": None, "claim_keys": []}


class _Window:
    """One logged desk window: its hash, its date, its rows, its faults."""

    def __init__(self, window_hash: str, as_of: str, rows: dict, **errors):
        self.window_hash = window_hash
        self.as_of = as_of
        self.rows = rows
        self.errors = errors


class _Session:
    """The narrow surface `build_owner_tick` actually touches."""

    def __init__(self, registry, windows, held):
        self.registry = registry
        self.windows = list(windows)
        self.held = list(held)
        self.atlas = AtlasSupervisor(
            registry, coordinator_available=lambda: True,
            config=AtlasConfig(),
            id_gen=(lambda c=itertools.count(1): f"task-{next(c)}"))
        self._served = 0

    # the tick's news half
    def fetch_desk_news(self, offline):
        return {"items": []}

    def compose_desk_read(self, offline, prefetched_news=None):
        return {}

    def qualitative_matrix(self, offline):
        """Log one row per window, exactly as the owner's method does."""
        window = self.windows[min(self._served, len(self.windows) - 1)]
        self._served += 1
        matrix = {"as_of": window.as_of, "window_hash": window.window_hash,
                  "rows": window.rows}
        payload = dict(matrix, provider="synthetic", **window.errors)
        found = self.registry.matrix_runs(source=DESK_MATRIX_SOURCE, limit=1)
        last = ((found[0].get("spec") or {}).get("matrix") or {}).get(
            "window_hash") if found else None
        if last != window.window_hash:
            payload["run_id"] = self.registry.log_run(
                "qualitative_matrix",
                {"source": DESK_MATRIX_SOURCE, "matrix": matrix})
        else:
            payload["run_id"] = found[0]["run_id"]
        return payload

    def live_portfolio(self, offline):
        return {"blocked": False, "positions": [
            {"ticker": t, "qty": 10.0, "weight": 0.5} for t in self.held]}

    def atlas_observe(self, offline, **handed):
        return self.atlas.observe(_facts(), trading_date="2026-08-31")


def _tick(session):
    return build_owner_tick(session, threading.Lock(), offline=True)


def _held_tasks(result):
    return [t for t in result.get("created_tasks") or []
            if t.get("trigger") == "held_record_change"]


def _run(reg, windows, held=("ACWI",), ticks=2):
    session = _Session(reg, windows, held)
    tick = _tick(session)
    return session, [tick() for _ in range(ticks)]


# --- the rule ----------------------------------------------------------------


def test_a_new_primary_document_on_a_held_name_mints_one_trigger(reg):
    session, results = _run(reg, [
        _Window("w1", "2026-08-30", {"ACWI": _row("ACWI", primary_docs=0),
                                     "BNDW": _row("BNDW")}),
        _Window("w2", "2026-08-31", {"ACWI": _row("ACWI", primary_docs=1),
                                     "BNDW": _row("BNDW")}),
    ])

    assert _held_tasks(results[0]) == []
    created = _held_tasks(results[1])
    assert len(created) == 1, created
    assert created[0]["action"] == "workflow"
    task = reg.get_atlas_task(created[0]["task_id"])
    assert task["template_id"] == "portfolio_watch"
    assert task["trigger_kind"] == "held_record_change"
    assert task["status"] == "queued"
    assert task["trigger_payload"]["reason"] == (
        "ACWI: primary +1, corroborated +0")
    assert task["trigger_payload"]["ticker"] == "ACWI"


def test_two_more_corroborated_claims_fire_and_one_does_not(reg):
    _, results = _run(reg, [
        _Window("w1", "2026-08-30", {"ACWI": _row("ACWI", corroborated=1)}),
        _Window("w2", "2026-08-31", {"ACWI": _row("ACWI", corroborated=3)}),
    ])
    fired = _held_tasks(results[1])
    assert len(fired) == 1
    assert "corroborated +2" in fired[0]["reason"]

    quiet = Registry(":memory:")
    try:
        _, results = _run(quiet, [
            _Window("w1", "2026-08-30", {"ACWI": _row("ACWI", corroborated=1)}),
            _Window("w2", "2026-08-31", {"ACWI": _row("ACWI", corroborated=2)}),
        ])
        assert _held_tasks(results[1]) == []
    finally:
        quiet.close()


def test_the_same_change_on_an_unheld_name_mints_nothing(reg):
    _, results = _run(reg, [
        _Window("w1", "2026-08-30", {"ACWI": _row("ACWI"),
                                     "BNDW": _row("BNDW", primary_docs=0)}),
        _Window("w2", "2026-08-31", {"ACWI": _row("ACWI"),
                                     "BNDW": _row("BNDW", primary_docs=2)}),
    ], held=("ACWI",))
    assert _held_tasks(results[1]) == []


def test_the_same_window_twice_mints_no_second_task(reg):
    _, results = _run(reg, [
        _Window("w1", "2026-08-30", {"ACWI": _row("ACWI", primary_docs=0)}),
        _Window("w2", "2026-08-31", {"ACWI": _row("ACWI", primary_docs=1)}),
        # The third tick serves the same window again: the record has not
        # moved, so the desk has nothing new to say about it.
        _Window("w2", "2026-08-31", {"ACWI": _row("ACWI", primary_docs=1)}),
    ], ticks=3)
    assert len(_held_tasks(results[1])) == 1
    assert _held_tasks(results[2]) == []


def test_the_first_window_ever_fires_nothing(reg):
    _, results = _run(reg, [
        _Window("w1", "2026-08-31", {"ACWI": _row("ACWI", primary_docs=4,
                                                  corroborated=9)}),
    ], ticks=1)
    assert _held_tasks(results[0]) == []


def test_a_broken_window_is_skipped_and_says_so(reg):
    _, results = _run(reg, [
        _Window("w1", "2026-08-30", {"ACWI": _row("ACWI", primary_docs=0)}),
        _Window("w2", "2026-08-31", {"ACWI": _row("ACWI", primary_docs=3)},
                news_error="feed unavailable: connection refused"),
    ])
    assert _held_tasks(results[1]) == []
    skipped = reg.read_events_of_kind("held_record_change_skipped", limit=10)
    assert skipped, "a broken window must be named, not silently dropped"
    assert "feed unavailable" in str(skipped[-1]["payload"]["reason"])


def test_a_broken_calendar_is_skipped_too(reg):
    _, results = _run(reg, [
        _Window("w1", "2026-08-30", {"ACWI": _row("ACWI", primary_docs=0)}),
        _Window("w2", "2026-08-31", {"ACWI": _row("ACWI", primary_docs=3)},
                calendar_error="the look-ahead calendar has run out"),
    ])
    assert _held_tasks(results[1]) == []
    assert reg.read_events_of_kind("held_record_change_skipped", limit=10)


# --- the template mapping ----------------------------------------------------


def test_the_trigger_maps_to_portfolio_watch_and_starts_in_research():
    from qlab.operator.templates import TRIGGER_TEMPLATE, check_startable

    assert TRIGGER_TEMPLATE["held_record_change"] == "portfolio_watch"
    template = check_startable(
        TRIGGER_TEMPLATE["held_record_change"], "research", _facts())
    assert template.template_id == "portfolio_watch"
    assert template.creates_plan is False


def test_the_minted_task_is_gated_at_start_time_like_every_trigger(reg):
    """Minted queued, never spawned — the mode decides at START, not here."""
    session, results = _run(reg, [
        _Window("w1", "2026-08-30", {"ACWI": _row("ACWI", primary_docs=0)}),
        _Window("w2", "2026-08-31", {"ACWI": _row("ACWI", primary_docs=2)}),
    ])
    task_id = _held_tasks(results[1])[0]["task_id"]

    def entry():
        return next(e for e in session.atlas.startable_tasks(
            _facts(), today="2026-08-31") if e["task_id"] == task_id)

    session.atlas.set_mode("observe")
    refused = entry()
    assert refused["startable"] is False
    assert refused["origin"] == "trigger"

    session.atlas.set_mode("research")
    allowed = entry()
    assert allowed["startable"] is True
    assert allowed["template_id"] == "portfolio_watch"
    assert allowed["creates_plan"] is False
