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

    def __init__(self, registry, windows, held, *, blocked=False):
        self.registry = registry
        self.windows = list(windows)
        self.held = list(held)
        self.blocked = blocked
        self.book_reads = 0
        self.scans = 0
        registry.matrix_runs = self._counted(registry.matrix_runs)
        self.atlas = AtlasSupervisor(
            registry, coordinator_available=lambda: True,
            config=AtlasConfig(),
            id_gen=(lambda c=itertools.count(1): f"task-{next(c)}"))
        self._served = 0

    def _counted(self, scan):
        """Count only the RULE's scans: the stub's own log dedupe is not one."""
        def wrapped(*a, **k):
            if k.get("limit") != 1:
                self.scans += 1
            return scan(*a, **k)
        return wrapped

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
        self.book_reads += 1
        if self.blocked:
            return {"blocked": True, "reason": "live data is unavailable"}
        return {"blocked": False, "positions": [
            # A flat row and a short one are both "not held": only a positive
            # quantity is a name the desk is carrying.
            {"ticker": "FLAT", "qty": 0.0, "weight": 0.0},
            {"ticker": "SHORT", "qty": -4.0, "weight": -0.2},
            *({"ticker": t, "qty": 10.0, "weight": 0.5} for t in self.held)]}

    def atlas_observe(self, offline, **handed):
        return self.atlas.observe(_facts(), trading_date="2026-08-31")


def _tick(session):
    return build_owner_tick(session, threading.Lock(), offline=True)


def _held_tasks(result):
    return [t for t in result.get("created_tasks") or []
            if t.get("trigger") == "held_record_change"]


def _run(reg, windows, held=("ACWI",), ticks=2, *, fresh=False, blocked=False):
    """Drive the tick over a list of windows, one window per tick.

    ``fresh`` builds a new tick closure per call, which is what an owner
    RESTART looks like: the per-process "already minted this window" cache is
    gone and only the registry's dedupe key can refuse a second task.
    """
    session = _Session(reg, windows, held, blocked=blocked)
    # Kept on the session so a test can drive the SAME closure again: a fresh
    # one has an empty cache and would prove nothing about it.
    session.tick = tick = _tick(session)
    out = []
    for _ in range(ticks):
        out.append((_tick(session) if fresh else tick)())
    return session, out


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
    """The held name in the SAME window is the control: the rule did fire."""
    _, results = _run(reg, [
        _Window("w1", "2026-08-30", {"ACWI": _row("ACWI", primary_docs=0),
                                     "BNDW": _row("BNDW", primary_docs=0)}),
        _Window("w2", "2026-08-31", {"ACWI": _row("ACWI", primary_docs=1),
                                     "BNDW": _row("BNDW", primary_docs=2)}),
    ], held=("ACWI",))
    fired = _held_tasks(results[1])
    assert [t["ticker"] for t in fired] == ["ACWI"], fired


def test_the_same_window_twice_mints_no_second_task(reg):
    _, results = _run(reg, [
        _Window("w1", "2026-08-30", {"ACWI": _row("ACWI", primary_docs=0)}),
        _Window("w2", "2026-08-31", {"ACWI": _row("ACWI", primary_docs=1)}),
        # The third tick serves the same window again: the record has not
        # moved, so the desk has nothing new to say about it.
        _Window("w2", "2026-08-31", {"ACWI": _row("ACWI", primary_docs=1)}),
    ], ticks=3, fresh=True)
    assert len(_held_tasks(results[1])) == 1
    assert _held_tasks(results[2]) == []


def test_the_first_window_ever_fires_nothing(reg):
    """A record is only ever a CHANGE against one before it.

    The second window is the control: the same counts that fired nothing on
    the first window fire once a baseline exists, so this test distinguishes
    "declined" from "the rule never ran".
    """
    _, results = _run(reg, [
        _Window("w1", "2026-08-30", {"ACWI": _row("ACWI", primary_docs=4,
                                                  corroborated=9)}),
        _Window("w2", "2026-08-31", {"ACWI": _row("ACWI", primary_docs=5,
                                                  corroborated=9)}),
    ])
    assert _held_tasks(results[0]) == []
    assert len(_held_tasks(results[1])) == 1


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


def test_the_trigger_is_not_one_the_unattended_beat_may_start():
    """Two sets, two facts, and they are deliberately not each other's
    complement.

    (a) Outside `_WORKFLOW_TRIGGERS`: an unapproved watch never spends the
    daily autonomous budget. (e) Inside `_UNBOUNDED_TRIGGERS`: the mint has no
    bound per window — one task per held name whose record moved — and
    `portfolio_watch` runs a Claude coordinator with WebSearch/WebFetch, so the
    beat must not start it at all until the mint site is bounded."""
    from qlab.operator.atlas import _UNBOUNDED_TRIGGERS, _WORKFLOW_TRIGGERS

    assert "held_record_change" not in _WORKFLOW_TRIGGERS
    assert "held_record_change" in _UNBOUNDED_TRIGGERS
    # The two sets are disjoint by construction: a kind the budget charges for
    # is a kind the beat starts, and a kind in both would be charged and never
    # spent.
    assert _UNBOUNDED_TRIGGERS.isdisjoint(_WORKFLOW_TRIGGERS)


def _research_session():
    from qlab.ui.server import UISession

    session = UISession(offline_default=True, registry=Registry(":memory:"))
    session.atlas.set_mode("research")
    return session


def test_a_bounded_brief_or_alert_still_starts_unattended():
    """The regression pin. `owner_startup`/`data_recovered` (-> desk_brief),
    `kill_switch` (-> risk_event) and `new_research_run` (-> research_review)
    fire once per condition, and the beat has always started them — they sit
    outside `_WORKFLOW_TRIGGERS` because briefs and alerts are not workflow
    launches and do not spend the daily budget, NOT because they are
    unattended work. Gating the beat on the budget set would silence all four.
    """
    from datetime import date

    today = date.today().isoformat()
    for kind, template in (("owner_startup", "desk_brief"),
                           ("kill_switch", "risk_event")):
        session = _research_session()
        session.atlas_workflow_runner = lambda task, template_id: {
            "template_id": template_id, "action_taken": True}
        session.coordinator_status = lambda: {"driving": False,
                                              "workflow_id": ""}
        session.registry.create_atlas_task(
            f"task-{kind}", f"{kind}|{today}|ACWI|s", kind, {}, template)

        started = session.atlas_run_startable(True, limit=5)

        assert [e["task_id"] for e in started] == [f"task-{kind}"], kind
        assert started[0].get("started") is True, kind


def test_a_minted_task_survives_a_beat_still_queued():
    """The beat sees it, passes over it, and registers no workflow.

    Remove ``held_record_change`` from ``_UNBOUNDED_TRIGGERS`` and this task
    starts: one uncounted coordinator per moved held name per window."""
    from datetime import date

    session = _research_session()
    today = date.today().isoformat()
    session.registry.create_atlas_task(
        "task-held", f"held_record_change|{today}|ACWI|w1->w2",
        "held_record_change", {"ticker": "ACWI", "reason": "primary +1"},
        "portfolio_watch")
    # The premise: this task is startable on every axis except its kind, so
    # `started == []` below cannot pass for some unrelated refusal.
    entry = next(e for e in session.atlas.startable_tasks(session.atlas_facts(True))
                 if e["task_id"] == "task-held")
    assert entry["startable"] is True, entry.get("reason")
    assert entry["origin"] == "trigger"

    started = session.atlas_run_startable(True, limit=5)

    assert started == []
    task = session.registry.get_atlas_task("task-held")
    assert task["status"] == "queued"
    assert not task.get("workflow_id")
    assert session.registry.list_workflows(limit=10) == []


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


# --- what the operator is told ----------------------------------------------


def test_the_minted_entry_carries_what_the_chat_line_has_to_say(reg):
    """Five names moving in one window must not read as five identical lines.

    The entry is the announcement's only source of a subject: `created_tasks`
    is what `announce_desk_work` walks, and a line naming only the trigger kind
    is the same sentence for every name. This pins the contract from the mint
    side; the chat line that consumes it is `announce_desk_work`'s half.
    """
    _, results = _run(reg, [
        _Window("w1", "2026-08-30", {"ACWI": _row("ACWI", primary_docs=0),
                                     "BNDW": _row("BNDW", corroborated=0)}),
        _Window("w2", "2026-08-31", {"ACWI": _row("ACWI", primary_docs=1),
                                     "BNDW": _row("BNDW", corroborated=2)}),
    ], held=("ACWI", "BNDW"))
    fired = _held_tasks(results[1])
    assert [t["ticker"] for t in fired] == ["ACWI", "BNDW"]
    assert [t["reason"] for t in fired] == [
        "ACWI: primary +1, corroborated +0",
        "BNDW: primary +0, corroborated +2"]


def test_a_held_name_the_mandate_does_not_watch_is_named(reg):
    """Silence about a name the desk owns and the matrix cannot see is a lie.

    The matrix's rows are the mandate universe. A position outside it has no
    row in either window, so the comparison skips it — correctly, there is
    nothing to compare — but the operator has to learn that the desk is
    carrying something its qualitative record does not cover.
    """
    session, results = _run(reg, [
        _Window("w1", "2026-08-30", {"ACWI": _row("ACWI", primary_docs=0)}),
        _Window("w2", "2026-08-31", {"ACWI": _row("ACWI", primary_docs=1)}),
    ], held=("ACWI", "TQQQ"))
    assert [t["ticker"] for t in _held_tasks(results[1])] == ["ACWI"]
    named = reg.read_events_of_kind("held_record_change_unwatched", limit=10)
    assert named, "a held name with no matrix row must be named"
    assert named[-1]["payload"]["tickers"] == ["TQQQ"]
    assert named[-1]["payload"]["window_hash"] == "w2"


# --- the cases the design's own choices exist for ---------------------------


def test_two_windows_on_one_day_fire_once_against_the_true_previous(reg):
    """Why the search is 'newest DIFFERING hash' and not 'as_of before today'.

    News moves intraday. Both windows carry the same date, so a strictly
    earlier `as_of` would find neither and the rise would be invisible.
    """
    _, results = _run(reg, [
        _Window("w1", "2026-08-31", {"ACWI": _row("ACWI", primary_docs=1)}),
        _Window("w2", "2026-08-31", {"ACWI": _row("ACWI", primary_docs=2)}),
    ])
    fired = _held_tasks(results[1])
    assert len(fired) == 1
    # Against w1 (primary 1 -> 2), not against nothing (which would read +2).
    assert fired[0]["reason"] == "ACWI: primary +1, corroborated +0"


def test_an_unchanged_window_on_a_later_day_mints_no_second_task(reg):
    """Why the dedupe key carries the WINDOW's date and not the clock.

    An unchanged window is never re-logged, so a key stamped with today would
    mint the same finding again every morning. The third window here is the
    same window served on a later day, through a fresh tick closure — an owner
    restart — so only the registry's key can refuse it.
    """
    session, results = _run(reg, [
        _Window("w1", "2026-08-30", {"ACWI": _row("ACWI", primary_docs=0)}),
        _Window("w2", "2026-08-31", {"ACWI": _row("ACWI", primary_docs=1)}),
        _Window("w2", "2026-09-04", {"ACWI": _row("ACWI", primary_docs=1)}),
    ], ticks=3, fresh=True)
    assert len(_held_tasks(results[1])) == 1
    assert _held_tasks(results[2]) == []
    task = reg.get_atlas_task(_held_tasks(results[1])[0]["task_id"])
    # The window's own date, from the logged run — never 2026-09-04.
    assert task["dedupe_key"] == "held_record_change|2026-08-31|ACWI|w1->w2"


def test_a_blocked_book_is_a_fault_and_never_a_flat_desk(reg):
    _, results = _run(reg, [
        _Window("w1", "2026-08-30", {"ACWI": _row("ACWI", primary_docs=0)}),
        _Window("w2", "2026-08-31", {"ACWI": _row("ACWI", primary_docs=3)}),
    ], blocked=True)
    assert _held_tasks(results[1]) == []
    skipped = reg.read_events_of_kind("held_record_change_skipped", limit=10)
    assert skipped, "an unreadable book must not read as holding nothing"
    assert "blocked" in str(skipped[-1]["payload"]["reason"])


def test_a_mint_that_raises_lands_on_its_own_key(reg):
    session = _Session(reg, [
        _Window("w1", "2026-08-30", {"ACWI": _row("ACWI", primary_docs=0)}),
        _Window("w2", "2026-08-31", {"ACWI": _row("ACWI", primary_docs=3)}),
    ], ("ACWI",))

    def _broken(offline):
        raise RuntimeError("the broker connection died mid-tick")

    session.live_portfolio = _broken
    tick = _tick(session)
    tick()
    result = tick()

    assert _held_tasks(result) == []
    assert "broker connection died" in result["held_record_error"]
    # And the tick itself survived: the observe's own answer is still there.
    assert result["state"]
    assert reg.read_events_of_kind("held_record_change_failed", limit=5)


def test_an_exhausted_scan_says_so_rather_than_reading_as_a_first_window(reg):
    """Finding no previous window in a bounded scan is not 'nothing changed'."""
    from qlab.operator import heartbeat

    session = _Session(reg, [
        _Window("w1", "2026-08-30", {"ACWI": _row("ACWI", primary_docs=0)}),
        _Window("w2", "2026-08-31", {"ACWI": _row("ACWI", primary_docs=3)}),
    ], ("ACWI",))
    tick = _tick(session)
    tick()
    # A scan that comes back FULL and all of one window: the previous window is
    # further back than the scan reaches, which is a different fact from there
    # being no previous window at all.
    real = reg.matrix_runs
    reg.matrix_runs = (lambda *a, **k: real(
        *a, **{**k, "limit": 1}) * heartbeat.MATRIX_SCAN)
    result = tick()

    assert _held_tasks(result) == []
    skipped = reg.read_events_of_kind("held_record_change_skipped", limit=10)
    assert skipped, "an exhausted scan must be named, not silently dropped"
    assert "scan" in str(skipped[-1]["payload"]["reason"])


def test_an_unchanged_window_costs_the_tick_nothing(reg):
    """A quiet desk must not pay for the book and the scan every 30 seconds."""
    session, results = _run(reg, [
        _Window("w1", "2026-08-30", {"ACWI": _row("ACWI", primary_docs=0)}),
        _Window("w2", "2026-08-31", {"ACWI": _row("ACWI", primary_docs=1)}),
        _Window("w2", "2026-08-31", {"ACWI": _row("ACWI", primary_docs=1)}),
        _Window("w2", "2026-08-31", {"ACWI": _row("ACWI", primary_docs=1)}),
    ], ticks=4)
    assert len(_held_tasks(results[1])) == 1
    # The book is read every tick — a position can be opened while the news
    # window stands still, and no cheaper tell for that exists — but the two
    # repeats of the second window cost no registry scan and no mint at all.
    assert session.book_reads == 4, session.book_reads
    assert session.scans == 2, session.scans


def test_a_position_opened_while_the_window_stands_still_is_evaluated(reg):
    """The cache is about an unchanged QUESTION, not an unchanged window.

    A name bought this morning has never been compared against anything. Keyed
    on the window hash alone, the rule skipped the whole mint until the next
    distinct window arrived — on a quiet tape, potentially a full day of a new
    holding nobody had looked at.
    """
    session, results = _run(reg, [
        _Window("w1", "2026-08-30", {"ACWI": _row("ACWI", primary_docs=0),
                                     "BNDW": _row("BNDW", corroborated=0)}),
        _Window("w2", "2026-08-31", {"ACWI": _row("ACWI", primary_docs=1),
                                     "BNDW": _row("BNDW", corroborated=2)}),
    ], held=("ACWI",))
    assert [t["ticker"] for t in _held_tasks(results[1])] == ["ACWI"]

    # Same window, new position. The record of the name the desk just bought
    # moved between those two windows and nothing has said so.
    session.held.append("BNDW")
    third = session.tick()
    assert [t["ticker"] for t in _held_tasks(third)] == ["BNDW"]
    assert _held_tasks(third)[0]["reason"] == "BNDW: primary +0, corroborated +2"


def test_the_exhausted_scan_event_carries_the_numbers_it_names(reg):
    """A free-text reason is not a field. The scan's shape is structured."""
    from qlab.operator import heartbeat

    session = _Session(reg, [
        _Window("w1", "2026-08-30", {"ACWI": _row("ACWI", primary_docs=0)}),
        _Window("w2", "2026-08-31", {"ACWI": _row("ACWI", primary_docs=3)}),
    ], ("ACWI",))
    tick = _tick(session)
    tick()
    real = reg.matrix_runs
    reg.matrix_runs = (lambda *a, **k: real(
        *a, **{**k, "limit": 1}) * heartbeat.MATRIX_SCAN)
    tick()

    payload = reg.read_events_of_kind(
        "held_record_change_skipped", limit=10)[-1]["payload"]
    assert payload["examined"] == heartbeat.MATRIX_SCAN
    assert payload["scan_limit"] == heartbeat.MATRIX_SCAN
