"""Atlas's heartbeat: the loop that makes the desk manager actually run.

Without this, ``AtlasSupervisor`` is inert — it can evaluate triggers, but only
when something calls it, so it sits in ``starting`` forever and the operator
sees a desk manager that never manages anything.

The heartbeat runs inside the owner process (the single DuckDB writer) and
takes the same dispatch lock the HTTP handlers use, so a tick never races a
request. Everything it does is deterministic: assemble owner facts, evaluate
triggers, persist any new task, publish a state event. No LLM call happens on
a quiet tick — an unchanged desk costs nothing.

The one exception is the reasoner's template judgment, and it is shaped by that
rule rather than against it. It runs only when the operator has switched
``reasoner_enabled`` on AND a trigger would open a genuinely new task, so a
quiet tick still costs nothing; and when it does run, the tick splits in two so
the completion happens with the dispatch lock RELEASED. A model call inside the
lock would hold the snapshot poll, the SSE poll and every approval behind it
for the reasoner's whole timeout.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import date

# A quiet desk should not spin: the default cadence is slow enough to be cheap
# and fast enough that a human watching the rail sees it breathing.
DEFAULT_INTERVAL_S = 30.0
MIN_INTERVAL_S = 5.0


class AtlasHeartbeat:
    """Ticks the supervisor on an interval; owns no state of its own."""

    def __init__(
        self,
        tick: Callable[[], dict],
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        on_error: Callable[[Exception], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.interval_s = max(MIN_INTERVAL_S, float(interval_s))
        self._tick = tick
        self._on_error = on_error
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ticks = 0
        self._errors = 0
        self._last_error = ""
        # None, not "": the client types this field off the wire shape, and a
        # sentinel of a different JSON type than the value poisons the whole
        # snapshot on the first failing tick.
        self._last_error_at: float | None = None
        self._last_tick_at: float | None = None
        self._last_result: dict | None = None
        self._lock = threading.RLock()

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("Atlas heartbeat is already running")
        self._stop.clear()
        thread = threading.Thread(
            target=self._run, name="qlab-atlas-heartbeat", daemon=True)
        self._thread = thread
        thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- the loop -----------------------------------------------------------
    def _run(self) -> None:
        # Tick immediately so the desk leaves 'starting' as soon as the owner
        # is up, rather than after a full interval of looking broken.
        while not self._stop.is_set():
            self.tick_once()
            self._stop.wait(self.interval_s)

    def tick_once(self) -> dict | None:
        """Run one tick. Never raises: a failing tick must not kill the loop."""
        try:
            result = self._tick()
        except Exception as exc:
            with self._lock:
                self._errors += 1
                # The count alone cannot be acted on, and the only other
                # channel was a print to a stdout the launcher sends to
                # DEVNULL. Keep the reason where the snapshot can carry it.
                self._last_error = repr(exc)[:300]
                self._last_error_at = self._clock()
            if self._on_error is not None:
                try:
                    self._on_error(exc)
                except Exception:
                    pass  # an error handler must never take the loop down
            return None
        with self._lock:
            self._ticks += 1
            self._last_tick_at = self._clock()
            self._last_result = result
        return result

    def status(self) -> dict:
        with self._lock:
            return {
                "running": self.running,
                "interval_s": self.interval_s,
                "ticks": self._ticks,
                "errors": self._errors,
                "last_tick_at": self._last_tick_at,
                "last_error": self._last_error,
                "last_error_at": self._last_error_at,
                "last_state": (self._last_result or {}).get("state"),
            }


def build_owner_tick(session, lock, *, offline: bool,
                     autonomous: bool = False) -> Callable[[], dict]:
    """The owner's tick: fetch externally, then compose and observe under lock.

    Bound to ``session`` rather than importing it, so tests drive the same code
    with a stub. The lock is the owner's HTTP dispatch lock — holding it makes a
    tick mutually exclusive with request handling, which is what keeps the
    one-writer rule intact.

    With ``autonomous`` the tick also STARTS work Atlas's mode already permits.
    This flag does not widen authority by one inch: ``check_startable`` still
    refuses anything the mode forbids, so autonomy in Research mode launches
    research and still cannot create a paper plan. It only removes the need for
    a human to press the button on work Atlas was already allowed to do.

    The tick is in two lock phases rather than one when the reasoner is on:
    take the lock, compose the read and gather what needs judging; release it
    and ask the model; take it again and observe. Each phase is internally
    consistent and the owner facts are assembled exactly once and carried
    across, which matters for correctness and not just cost —
    ``_atlas_regime_facts`` records the state it saw, so a second assembly in
    one tick reports no flip.
    """

    def tick() -> dict:
        # Network work is deliberately outside the owner dispatch lock. The
        # returned payload contains no registry state; grounding and composition
        # happen below while the one-writer boundary is held.
        fetch_news = getattr(session, "fetch_desk_news", None)
        prefetched_news = fetch_news(offline) if callable(fetch_news) else None
        live_autonomous = autonomous

        def observe(**handed) -> dict:
            # Before the observe, so one tick closes the whole loop: the reap
            # interrupts a workflow whose coordinator lease has expired,
            # `atlas_observe` reconciles the task it belongs to, and the sweep
            # below then passes over it instead of respawning a coordinator for
            # a run that died half an hour ago. Reaping was reachable ONLY from
            # the snapshot path and `GET /api/workflows`, so an owner running
            # unattended — no TUI, no browser — never reaped at all, and that is
            # exactly the desk the sweep runs on. It self-throttles to a minute.
            reap = getattr(session, "reap_stale_workflows", None)
            reaped, reap_error = None, ""
            if callable(reap):
                try:
                    reaped = reap()
                except Exception as exc:
                    # Its own key, never the value's: a field that changes JSON
                    # type mid-run poisons the tick for a typed client.
                    reap_error = str(exc)[:200]
            # `handed` is empty unless the reasoner ran, so a desk with the
            # flag off calls exactly what it called before.
            result = session.atlas_observe(offline, **handed)
            if reaped is not None:
                result["reaped"] = reaped
            if reap_error:
                result["reap_error"] = reap_error
            result["autonomous_enabled"] = bool(live_autonomous)
            if live_autonomous:
                try:
                    result["autonomous"] = session.atlas_run_startable(offline)
                except Exception as exc:
                    result["autonomous_error"] = str(exc)[:200]
            # What the desk now wants from the operator, said where they ask.
            # After the autonomous run, so a trigger this tick both started
            # and announced reads as one fact, and before the sweep, which
            # only walks what an operator already approved.
            announce = getattr(session, "announce_desk_work", None)
            if callable(announce):
                try:
                    result["announced"] = announce(
                        offline, result.get("created_tasks") or [])
                except Exception as exc:
                    result["announce_error"] = str(exc)[:200]
            # Deliberately NOT under `live_autonomous`. Approving is what
            # started this work; the sweep only walks a workflow the gate has
            # already opened, and the dispatch that opened it could not drive
            # it because the one coordinator slot was taken. Gating this on
            # autonomy would mean a desk with autonomy off leaves the operator's
            # own approvals parked at phase one forever.
            #
            # After `atlas_observe`, whose reconciliation resolves the runs that
            # DID finish — so the sweep only ever sees what is still in flight.
            sweep = getattr(session, "drive_pending_tasks", None)
            if callable(sweep):
                try:
                    result["driven"] = sweep()
                except Exception as exc:
                    result["drive_error"] = str(exc)[:200]
            return result

        with lock:
            # Read the switch each tick so the UI toggle takes effect
            # immediately rather than at the next owner restart.
            live_autonomous = getattr(session, "autonomous", autonomous)
            # The qualitative read is what a human actually looks at; refresh it
            # on the same tick so the rail and the drawer never disagree.
            try:
                if prefetched_news is None:
                    session.refresh_desk_read(offline)
                else:
                    session.compose_desk_read(
                        offline,
                        prefetched_news=prefetched_news,
                    )
                    # Inside the lock the compose already holds, and guarded
                    # separately: an archive fault is worth an event, but it
                    # must not reach mark_desk_read_stale and make a healthy
                    # window look stale.
                    archive = getattr(session, "archive_desk_news", None)
                    if callable(archive):
                        try:
                            archive(prefetched_news)
                        except Exception as exc:
                            session.registry.record_event(
                                "news_archive_failed", {"error": str(exc)[:400]})
                    # The matrix is the per-window qualitative record, and its
                    # only other producers are conditional — a route no shipped
                    # client calls, and a chat that needs a reasoner — so a
                    # stock desk logged none at all. Here it is deterministic:
                    # one row per window, which is the guard the method already
                    # enforces on its own. Guarded separately for the same
                    # reason the archive is: it must not make a healthy window
                    # look stale.
                    matrix = getattr(session, "qualitative_matrix", None)
                    if callable(matrix):
                        try:
                            matrix(offline)
                        except Exception as exc:
                            session.registry.record_event(
                                "qualitative_matrix_failed",
                                {"error": str(exc)[:400]})
            except Exception as exc:
                # A read failure must not stop the supervisor from observing —
                # but the cached read must not keep passing as current either,
                # or a news template runs its precondition against the previous
                # tick's window and reports a finding for evidence it never saw.
                mark_stale = getattr(session, "mark_desk_read_stale", None)
                if callable(mark_stale):
                    mark_stale(str(exc))
            # Facts and the triggers whose template a reasoner may choose.
            # Empty unless the reasoner flag is on, and a session that predates
            # the method (a test stub) is the same as the flag being off.
            request_judgments = getattr(session, "atlas_judgment_request", None)
            try:
                request = (request_judgments(offline)
                           if callable(request_judgments) else {})
            except Exception as exc:
                # The same rule as the desk read a few lines above — "a read
                # failure must not stop the supervisor from observing" — now
                # that the reasoner's path reaches those same reads by another
                # door. Nothing gathered here is worth a tick: the observe
                # below is the deterministic half, and it is what keeps the
                # drawdown tiers and the approval expiry moving. The composer
                # guards itself and records the reason; this is the net under
                # it, for the failure that is not on any list yet.
                request = {}
                note = getattr(session, "note_reasoner_fallback", None)
                if callable(note):
                    try:
                        note(None, f"the reasoner's tick could not be "
                                   f"prepared: {exc!r}")
                    except Exception:
                        pass  # a recorder that throws must not do what it guards
            if not request:
                return observe()

        # Outside the lock, and that placement is the whole reason this tick is
        # split in two: a completion under the owner's dispatch lock would hold
        # every request behind it for the reasoner's timeout. The facts travel
        # with the request because `atlas_facts` consumes a regime flip and must
        # not be assembled twice in one tick.
        judgments = session.atlas_judge(request)
        with lock:
            return observe(facts=request.get("facts"), judgments=judgments)

    return tick


def today_iso() -> str:
    return date.today().isoformat()
