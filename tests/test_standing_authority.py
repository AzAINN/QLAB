"""The beat that books what a live grant already covers — the production caller.

``UISession.book_under_grant`` is the automatic half of the desk's one booking
gate: a persisted grant stands in for the per-plan human confirmation and for
nothing else. But a gate nothing calls books nothing (invariant 10, and this
whole stream exists because ``qlab/governance/authority.py`` shipped tested and
unwired), so what is under test here is the CALL SITE — one attempt per owner
tick, inside a lock phase beside ``atlas_observe``, never in the released
reasoner phase, and never at the cost of the tick's own life.

Everything runs against ``Registry(":memory:")``. The behavioural tests drive a
real ``UISession``, so the refusals asserted are the sentences the owner
actually composes rather than a stub's; the wiring tests (one book per tick,
which lock phase, a fault contained) use a narrow stub, because what they pin
is the shape of the tick and not the shape of a grant. No owner process is
started and ``.lab/registry.duckdb`` is never opened.
"""

from __future__ import annotations

import threading
from datetime import date

import pytest

import qlab.ui.server as ui_server
from qlab.operator import heartbeat as hb
from qlab.operator.heartbeat import AtlasHeartbeat, build_owner_tick
from qlab.state.registry import Registry
from qlab.ui.server import UISession, handle_api


@pytest.fixture
def session():
    return UISession(offline_default=True, registry=Registry(":memory:"))


@pytest.fixture
def reg():
    r = Registry(":memory:")
    yield r
    r.close()


# --- the desk's own question, and the authority over it ---------------------


def _checked_plan(session, tilt: float = 0.0) -> str:
    """A referee-PASSed persisted checked plan — the only executable shape.

    Plans are content-addressed, so identical targets yield the same plan_id;
    ``tilt`` perturbs them when a test needs a genuinely different plan.
    """
    from qlab.core.types import Decision

    tickers = session.mandate.universe_whitelist
    even = 1.0 / len(tickers)
    targets = {ticker: even for ticker in tickers}
    if tilt:
        targets[tickers[0]] = even + tilt
        targets[tickers[-1]] = even - tilt
    decision_id = session.registry.log_decision(Decision(
        as_of=date.today(), kind="rebalance_gate",
        choice={"targets": targets}, rationale="configured HRP policy",
    ))
    session.registry.log_verdict(
        decision_id, "PASS", ["within mandate"], source="referee-agent",
        targets=targets)
    _, preview = handle_api(
        session, "POST", "/api/rebalance_preview", {},
        {"offline": True, "decision_id": decision_id, "targets": targets})
    assert preview["accepted"] is True
    return preview["plan_id"]


def _proposal(session, tilt: float = 0.0) -> tuple[str, str]:
    """The desk's open question: a checked plan with a pending request on it."""
    plan_id = _checked_plan(session, tilt=tilt)
    _, created = handle_api(
        session, "POST", "/api/approvals", {},
        {"plan_id": plan_id, "offline": True})
    return plan_id, created["approval_id"]


def _grant(session, **over) -> dict:
    """A persisted grant covering the desk's own even-weight rebalance."""
    from qlab.governance.authority import build_grant

    fields = {
        "allowed_universe": list(session.mandate.universe_whitelist),
        "max_notional": 100_000.0,
        "max_turnover": 2.0,
        "max_orders": 50,
        "max_books_per_day": 2,
        "allowed_policy": session.mandate.operational_policy,
        "granted_by": "operator",
        "ttl_days": 7,
    }
    fields.update(over)
    grant = build_grant(**fields)
    session.registry.create_authority_grant(grant)
    return grant


def _events(session, kind: str, limit: int = 50) -> list[dict]:
    return [event["payload"]
            for event in session.registry.read_events_of_kind(kind, limit)]


def _reasons(session) -> list[str]:
    refusals = _events(session, ui_server.GRANT_REFUSED_EVENT)
    assert refusals, "a refusal that says nothing is a refusal nobody can audit"
    return refusals[-1]["reasons"]


def _tick(session, lock=None):
    return build_owner_tick(session, lock or threading.Lock(), offline=True)


# --- 1. one beat, one book --------------------------------------------------


def test_one_beat_books_the_proposal_a_live_grant_covers(session):
    grant = _grant(session)
    plan_id, approval_id = _proposal(session)

    result = _tick(session)()

    assert result["authority_booked"]["booked"] is True
    assert result["authority_booked"]["approval_id"] == approval_id
    assert session.registry.get_approval_request(
        approval_id)["status"] == "consumed"
    assert session.registry.list_orders(50) != []
    booked = _events(session, ui_server.GRANT_BOOKED_EVENT)
    assert [row["grant_id"] for row in booked] == [grant["grant_id"]]
    assert booked[0]["plan_id"] == plan_id


def test_a_beat_with_nothing_on_the_desk_books_nothing_and_says_nothing(session):
    """The quiet case. A beat writing a row every 30 s to say the desk has no
    open question would bury the refusals that mean something."""
    _grant(session)

    result = _tick(session)()

    assert "authority_booked" not in result
    assert _events(session, ui_server.GRANT_REFUSED_EVENT) == []
    assert _events(session, ui_server.GRANT_BOOKED_EVENT) == []


# --- 2. the second beat does not book it again ------------------------------


def test_a_second_beat_does_not_book_the_same_plan_again(session):
    _grant(session)
    _proposal(session)
    tick = _tick(session)
    tick()
    filled = session.registry.list_orders(200)
    assert filled != []

    tick()

    assert session.registry.list_orders(200) == filled
    assert len(_events(session, ui_server.GRANT_BOOKED_EVENT)) == 1


def test_a_beat_never_re_books_a_plan_whose_legs_are_already_at_the_broker(
        session, monkeypatch):
    """The double-book guard, reached rather than assumed.

    After the first book the approval is consumed, so `current_proposal` offers
    nothing and the next beat is quiet — which proves the queue is empty, not
    that the beat would refuse. Hold the booked plan in front of it and it
    refuses by name, because a plan whose legs may be at the broker is resumed
    by a human, never by a grant.
    """
    import qlab.governance.proposal as proposal_module

    _grant(session)
    plan_id, approval_id = _proposal(session)
    tick = _tick(session)
    tick()
    filled = session.registry.list_orders(200)
    booked = session.registry.get_approval_request(approval_id)

    monkeypatch.setattr(
        proposal_module, "current_proposal",
        lambda registry: {"plan_id": plan_id, "approval_id": approval_id,
                          "targets_hash": booked["targets_hash"],
                          "status": "approved"})

    tick()

    assert session.registry.list_orders(200) == filled
    assert len(_events(session, ui_server.GRANT_BOOKED_EVENT)) == 1
    assert any("not 'checked'" in reason for reason in _reasons(session))


# --- 3. no grant, a suspended grant, a spent day: nothing, and it says why ---


def test_a_beat_with_no_grant_books_nothing_and_says_why(session):
    _, approval_id = _proposal(session)

    result = _tick(session)()

    assert "authority_booked" not in result
    assert session.registry.list_orders(50) == []
    assert session.registry.get_approval_request(
        approval_id)["status"] == "pending"
    assert _reasons(session) == ["no standing authority grant"]


def test_a_suspended_grant_books_nothing_and_names_the_anomaly(session):
    grant = _grant(session)
    _proposal(session)
    # The kill switch, on this desk's own book — `Registry.set_halt`'s default
    # is the simulated broker's name, which is the book an offline desk trades.
    session.registry.set_halt(True)

    result = _tick(session)()

    assert "authority_booked" not in result
    assert session.registry.list_orders(50) == []
    refusal = _events(session, ui_server.GRANT_REFUSED_EVENT)[-1]
    assert refusal["anomalies"] == ["account is halted"]
    assert "grant suspended by anomaly: account is halted" in refusal["reasons"]
    # A suspension is not a revocation: the authority survives the pause.
    assert session.registry.list_authority_grants(1)[0]["revoked_at"] is None
    assert session.registry.list_authority_grants(1)[0]["grant_id"] == (
        grant["grant_id"])


def test_a_spent_daily_budget_books_nothing_and_says_so(session):
    _grant(session, max_books_per_day=1)
    _proposal(session)
    tick = _tick(session)
    tick()
    assert len(_events(session, ui_server.GRANT_BOOKED_EVENT)) == 1
    filled = session.registry.list_orders(200)

    _proposal(session, tilt=0.05)      # the desk asks a second question today
    tick()

    assert session.registry.list_orders(200) == filled
    assert len(_events(session, ui_server.GRANT_BOOKED_EVENT)) == 1
    assert any("at its ceiling of 1" in reason for reason in _reasons(session))


# --- 4. a fault inside the book must not take the desk's autonomy with it ----


class _Narrow:
    """The surface `build_owner_tick` touches, plus the book under test.

    Deliberately narrow: everything else the tick reaches for is looked up with
    `getattr`, so a session that does not carry it is the same as the feature
    being off — which is what a test stub is.
    """

    def __init__(self, registry, *, book=None, reasoner=False, watch=None):
        self.registry = registry
        self.books: list[bool] = []
        self.judged = 0
        self._book = book
        self._reasoner = reasoner
        self._watch = watch

    def atlas_observe(self, offline, **handed):
        return {"state": "idle", "created_tasks": []}

    def book_under_grant(self, offline):
        self.books.append(offline)
        if self._watch is not None:
            self._watch("book")
        return self._book(offline) if self._book is not None else None

    # -- the reasoner split, when a test asks for it
    def atlas_judgment_request(self, offline):
        return {"facts": {"universe": []}, "triggers": []} if self._reasoner else {}

    def atlas_judge(self, request):
        self.judged += 1
        if self._watch is not None:
            self._watch("judge")
        return {}


def test_an_exception_inside_the_book_leaves_the_tick_alive(session, monkeypatch):
    """The most important guarantee in this task: a beat that dies takes the
    desk's autonomy with it. The fault is recorded where an operator can see
    it, and everything the tick does after the book still happens."""
    _grant(session)
    _proposal(session)

    def _explode(offline):
        raise RuntimeError("the broker vanished mid-plan")

    monkeypatch.setattr(session, "book_under_grant", _explode)
    beat = AtlasHeartbeat(_tick(session))

    result = beat.tick_once()

    assert result is not None
    assert "the broker vanished mid-plan" in result["authority_error"]
    assert "authority_booked" not in result
    # The tick RAN ON — these are the two seams that come after the book.
    assert "announced" in result and "driven" in result
    assert (beat.status()["ticks"], beat.status()["errors"]) == (1, 0)
    failed = _events(session, hb.BOOK_FAILED_EVENT)
    assert len(failed) == 1
    assert "the broker vanished mid-plan" in failed[0]["error"]


def test_a_beat_that_faulted_still_books_on_the_next_one(session, monkeypatch):
    """Containment, not a latch: the autonomy is still there next beat."""
    _grant(session)
    _proposal(session)
    real = session.book_under_grant
    calls: list[bool] = []

    def _flaky(offline):
        calls.append(offline)
        if len(calls) == 1:
            raise RuntimeError("a transient fault")
        return real(offline)

    monkeypatch.setattr(session, "book_under_grant", _flaky)
    tick = _tick(session)
    tick()
    assert session.registry.list_orders(50) == []

    result = tick()

    assert result["authority_booked"]["booked"] is True
    assert session.registry.list_orders(50) != []


def test_a_registry_that_cannot_record_the_fault_still_leaves_the_tick_alive(reg):
    """An error handler must never take the loop down — the rule `tick_once`
    already states about its own `on_error`."""
    def _explode(offline):
        raise RuntimeError("the broker vanished mid-plan")

    session = _Narrow(reg, book=_explode)

    def _no_events(kind, payload):
        raise RuntimeError("the events table is gone")

    reg.record_event = _no_events
    result = build_owner_tick(session, threading.Lock(), offline=True)()

    assert "the broker vanished mid-plan" in result["authority_error"]


# --- 5. at most one book per tick, in a lock phase --------------------------


def test_the_beat_attempts_at_most_one_book_per_tick(reg):
    session = _Narrow(reg)
    tick = build_owner_tick(session, threading.Lock(), offline=True)

    tick()
    tick()

    assert session.books == [True, True]


def _held(lock) -> bool:
    """True when somebody holds ``lock``.

    The owner's dispatch lock is a plain ``threading.Lock`` — not reentrant —
    so a caller that already holds it cannot take it again, which is exactly
    what makes this readable from inside the tick.
    """
    if lock.acquire(blocking=False):
        lock.release()
        return False
    return True


def test_the_beat_books_with_the_dispatch_lock_held(reg):
    lock = threading.Lock()
    seen: list[tuple[str, bool]] = []
    session = _Narrow(reg, watch=lambda where: seen.append((where, _held(lock))))

    build_owner_tick(session, lock, offline=True)()

    assert seen == [("book", True)]


def test_the_reasoner_split_books_once_and_in_the_lock_phase(reg):
    """The tick releases the lock to ask the model and takes it again to
    observe. The book belongs in a lock phase — the second one — because the
    reads that decided it have to still be true when the write happens."""
    lock = threading.Lock()
    seen: list[tuple[str, bool]] = []
    session = _Narrow(reg, reasoner=True,
                      watch=lambda where: seen.append((where, _held(lock))))

    build_owner_tick(session, lock, offline=True)()

    assert seen == [("judge", False), ("book", True)]
    assert session.books == [True]


# --- the permit chicken-and-egg: the beat records the first one -------------
#
# On a lane whose policy demands an execution data permit, the only writer of
# a `purpose="execution"` permit inside the owner is the execute gate itself.
# A desk that has never booked therefore has no permit, `_grant_anomalies`
# suspends every grant with "no execution data permit is on record", and
# standing authority can never fire — chicken-and-egg. The beat resolves it by
# MEASURING one, exactly as the gate does, and only when no answer is on
# record at all.


def _demanding_lane(session, monkeypatch, *, eligible=False, blocked=False,
                    raises=False):
    """A data lane whose policy DEMANDS an execution permit before a fill.

    Synthetic but operational, so `market.snapshot` still answers offline and
    the rest of the desk keeps reading real data: `data_health` is the real
    method and the permit it records is a real row, honestly ineligible
    (a synthetic panel is never execution-grade). That is the default here.

    `eligible=True` is the one answer no offline lane can produce, so that case
    — and only for `purpose="execution"` — stands in for the network, still
    recording a real row through `build_permit` so that what un-suspends the
    grant is the column the execute gate reads.

    Returns the list every `purpose="execution"` measurement is appended to.
    """
    from datetime import date as _date

    from qlab.core.data import DataPolicy
    from qlab.data.health import DataHealth
    from qlab.data.permit import build_permit

    policy = DataPolicy(
        mode="operational", provider="synthetic", feed=None,
        allow_network=False, allow_cache=True, allow_synthetic=True,
        require_fresh=False, execution_eligible=True)
    monkeypatch.setattr(session, "data_policy", lambda offline: policy)
    real_health = session.data_health
    calls: list[str] = []

    def _health(offline, purpose="paper_proposal"):
        # `atlas_facts` reads paper-proposal health every tick; only the
        # execution purpose is this feature's, and only it is stood in for.
        if purpose != "execution":
            return real_health(offline, purpose)
        calls.append(purpose)
        if raises:
            raise RuntimeError("the data feed is unreachable")
        if blocked:
            return {"blocked": True, "reason": "no execution-grade data",
                    "reasons": ["no execution-grade data"],
                    "eligible_for_paper_proposal": False,
                    "eligible_for_execution": False}
        if not eligible:
            return real_health(offline, purpose)
        health = DataHealth(
            provider="synthetic", synthetic=True, last_bar=_date.today(),
            reference_session=_date.today(), bar_age_sessions=0, fresh=True,
            provider_matches_policy=True, integrity_verdict="PASS",
            missing_tickers=[], eligible_for_research=True,
            eligible_for_paper_proposal=True,
            eligible_for_execution=True, reasons=[])
        permit = build_permit(
            snapshot_id="sha256:test", purpose=purpose, policy=policy,
            health=health, universe=list(session.mandate.universe_whitelist),
            as_of=_date.today().isoformat())
        session.registry.record_data_permit(permit.to_dict())
        return {"blocked": False, "permit_id": permit.permit_id,
                **health.to_dict()}

    monkeypatch.setattr(session, "data_health", _health)
    return calls


def test_a_lane_that_demands_a_permit_gets_one_and_books_on_the_same_beat(
        session, monkeypatch):
    grant = _grant(session)
    _proposal(session)
    calls = _demanding_lane(session, monkeypatch, eligible=True)

    result = _tick(session)()

    # Twice: the beat measured to un-stick the grant, and the execute gate
    # re-measured at the door. The beat's permit is not what authorises the
    # fill — it is only what stops the grant being suspended for its absence.
    assert calls == ["execution", "execution"]
    assert session.registry.current_data_permit("execution") is not None
    assert result["authority_booked"]["booked"] is True
    assert _events(session, ui_server.GRANT_BOOKED_EVENT)[0]["grant_id"] == (
        grant["grant_id"])
    primed = _events(session, hb.PERMIT_PRIMED_EVENT)
    assert len(primed) == 1
    assert primed[0]["eligible_for_execution"] is True


def test_an_ineligible_permit_is_never_re_measured_into_a_permission(
        session, monkeypatch):
    """The line that keeps this honest. The beat writes the FIRST answer and
    never argues with a recorded one: a permit that says no keeps saying no,
    however many beats run past it."""
    _grant(session)
    _proposal(session)
    calls = _demanding_lane(session, monkeypatch)
    tick = _tick(session)

    tick()
    tick()

    assert calls == ["execution"]          # measured once, never re-asked
    assert session.registry.current_data_permit(
        "execution")["eligible_for_execution"] is False
    assert session.registry.list_orders(50) == []
    assert _events(session, ui_server.GRANT_REFUSED_EVENT)[-1]["anomalies"] == [
        "data is not execution-eligible"]


def test_a_lane_that_demands_no_permit_is_never_asked_for_one(session):
    """Every offline and demo desk. `DataPolicy.demo`/`.test` are not
    execution-eligible, so the execute gate demands no permit there and
    neither may the beat — this costs such a desk nothing at all."""
    asked: list[str] = []
    real = session.data_health

    def _watch(offline, purpose="paper_proposal"):
        asked.append(purpose)
        return real(offline, purpose)

    session.data_health = _watch
    _grant(session)
    _proposal(session)

    _tick(session)()

    assert "execution" not in asked
    assert _events(session, hb.PERMIT_PRIMED_EVENT) == []


def test_a_desk_holding_no_grant_measures_no_execution_permit(
        session, monkeypatch):
    """A desk holding no standing authority must not start taking execution
    snapshots because of a feature it is not using."""
    _proposal(session)
    calls = _demanding_lane(session, monkeypatch)

    _tick(session)()

    assert calls == []
    assert _events(session, hb.PERMIT_PRIMED_EVENT) == []
    assert _reasons(session) == ["no standing authority grant"]


def test_a_desk_asking_nothing_measures_no_execution_permit(
        session, monkeypatch):
    _grant(session)
    calls = _demanding_lane(session, monkeypatch)

    _tick(session)()

    assert calls == []
    assert _events(session, hb.PERMIT_PRIMED_EVENT) == []


def test_a_blocked_lane_records_no_permit_and_the_grant_stays_suspended(
        session, monkeypatch):
    _grant(session)
    _proposal(session)
    calls = _demanding_lane(session, monkeypatch, blocked=True)

    result = _tick(session)()

    assert calls == ["execution"]
    assert session.registry.current_data_permit("execution") is None
    assert "authority_booked" not in result
    assert session.registry.list_orders(50) == []
    assert _events(session, ui_server.GRANT_REFUSED_EVENT)[-1]["anomalies"] == [
        "no execution data permit is on record for this book"]
    primed = _events(session, hb.PERMIT_PRIMED_EVENT)
    assert len(primed) == 1
    assert primed[0]["permit_id"] is None


def test_a_permit_that_cannot_be_measured_leaves_the_tick_and_the_book_alive(
        session, monkeypatch):
    """A prime that throws costs the book its permit, never its attempt: the
    refusal that lands names the desk's real state rather than the beat's."""
    _grant(session)
    _proposal(session)
    _demanding_lane(session, monkeypatch, raises=True)

    result = _tick(session)()

    assert "the data feed is unreachable" in result["authority_permit_error"]
    assert "announced" in result and "driven" in result
    assert _events(session, ui_server.GRANT_REFUSED_EVENT)[-1]["anomalies"] == [
        "no execution data permit is on record for this book"]
