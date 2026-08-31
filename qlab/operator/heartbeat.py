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
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import date

# A quiet desk should not spin: the default cadence is slow enough to be cheap
# and fast enough that a human watching the rail sees it breathing.
DEFAULT_INTERVAL_S = 30.0
MIN_INTERVAL_S = 5.0

# The trigger this module mints on its own, and what makes a record "changed".
# One primary document is a filing that did not exist last window; two more
# corroborated claims is a story that stopped being one publisher's take. A
# single new corroborated claim is not, deliberately: the desk should not wake
# a human for every second report of the same thing.
HELD_RECORD_TRIGGER = "held_record_change"
PRIMARY_DOCS_DELTA = 1
CORROBORATED_DELTA = 2
# How many logged desk windows back to look for the previous one. The newest
# run IS this window, so a scan of one would always find nothing to compare
# against; a few more cover a registry where a window was logged twice in a day.
MATRIX_SCAN = 6


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


def held_names(session, offline: bool) -> tuple[set[str], str]:
    """What the book is actually carrying, and why the answer may be empty.

    ``live_portfolio.positions`` with a POSITIVE quantity, which is the same
    definition BOOK draws its ribbon from and the matrix pane marks held names
    by. The registry's own position rows are deliberately not a fallback: a
    flat live book is a flat desk, not a missing answer, and reading the stale
    row would fire a trigger about a name the desk closed last week.

    Returns ``(held, fault)``; a non-empty fault means the book could not be
    read at all, which is not the same fact as holding nothing.
    """
    book = session.live_portfolio(offline)
    if not isinstance(book, dict):
        return set(), "the live book did not answer with a position list"
    if book.get("blocked"):
        return set(), (f"the live book is blocked: "
                       f"{str(book.get('reason') or 'no reason given')[:200]}")
    held: set[str] = set()
    for position in book.get("positions") or []:
        if not isinstance(position, Mapping):
            continue
        ticker = str(position.get("ticker") or "").strip()
        try:
            qty = float(position.get("qty") or 0.0)
        except (TypeError, ValueError):
            continue
        if ticker and qty > 0.0:
            held.add(ticker)
    return held, ""


def held_record_changes(current: Mapping, previous: Mapping,
                        held: Iterable[str]) -> list[dict]:
    """Held names whose record moved between two windows, with the delta said.

    Rises only. A record that thins out is not a reason to open research about
    a name — a story ending is the absence of news, and the fewer claims are
    already visible on the matrix. Both windows must carry the name: a name
    that entered the universe between them has no baseline, and treating a
    missing row as zero would mint a trigger for the whole record at once.
    """
    changes: list[dict] = []
    for ticker in sorted(set(held)):
        now, before = current.get(ticker), previous.get(ticker)
        if not isinstance(now, Mapping) or not isinstance(before, Mapping):
            continue
        primary = _count(now, "primary_docs") - _count(before, "primary_docs")
        corroborated = (_count(now, "corroborated")
                        - _count(before, "corroborated"))
        if primary < PRIMARY_DOCS_DELTA and corroborated < CORROBORATED_DELTA:
            continue
        changes.append({
            "ticker": ticker,
            "primary_docs_delta": primary,
            "corroborated_delta": corroborated,
            "reason": (f"{ticker}: primary {primary:+d}, "
                       f"corroborated {corroborated:+d}"),
        })
    return changes


def mint_held_record_tasks(session, matrix: Mapping, *, offline: bool,
                           book: tuple[set[str], str] | None = None) -> list[dict]:
    """Queue one ``held_record_change`` task per held name whose record moved.

    Runs beside the matrix log, on the window that log just wrote, because that
    is the only place in the owner where both windows are knowable: the tick
    logs one row per window, so "the previous window" is the newest earlier
    desk-stamped run and nothing else in the process holds it.

    Queued, never started. This mints exactly what a trigger mints — a task row
    with ``origin="trigger"`` and a template from ``TRIGGER_TEMPLATE`` — so the
    one-coordinator-at-a-time rule and ``check_startable`` apply at START time,
    through the same gate every other trigger goes through. Nothing here widens
    authority: ``portfolio_watch`` creates no plan.

    A window that arrived broken is skipped and SAYS so. Comparing a window
    whose feed failed against a healthy one measures the outage, not the
    record, and a silent skip would look exactly like a quiet tape.

    ``book`` is :func:`held_names`' answer when the caller has already read it
    — the tick has, because the held set is half of its cache key — so one
    tick never reads the book twice.
    """
    from qlab.news.matrix import DESK_MATRIX_SOURCE
    from qlab.operator.templates import TRIGGER_TEMPLATE

    registry = session.registry
    current_hash = str(matrix.get("window_hash") or "")
    current_rows = matrix.get("rows")
    fault = _window_fault(matrix)
    if not fault and (not current_hash or not isinstance(current_rows, Mapping)):
        fault = "this window carries no rows to compare"
    if fault:
        registry.record_event(
            HELD_RECORD_TRIGGER + "_skipped",
            {"reason": fault, "window_hash": current_hash})
        return []

    # The window's own date, not today's: an unchanged window is not re-logged,
    # so keying the dedupe on the clock would re-mint the same finding daily.
    as_of = str(matrix.get("as_of") or "")
    previous: Mapping = {}
    runs = registry.matrix_runs(source=DESK_MATRIX_SOURCE, limit=MATRIX_SCAN)
    for run in runs:
        spec = run.get("spec")
        logged = spec.get("matrix") if isinstance(spec, Mapping) else None
        if not isinstance(logged, Mapping):
            continue
        if str(logged.get("window_hash") or "") == current_hash:
            as_of = str(logged.get("as_of") or as_of)
            continue
        previous = logged
        break
    if not previous:
        if len(runs) >= MATRIX_SCAN:
            # A FULL scan that found no other window is not the first window
            # ever — the previous one is further back than this scan reaches,
            # and the two facts must not be reported by the same silence.
            registry.record_event(
                HELD_RECORD_TRIGGER + "_skipped",
                {"reason": (f"no earlier window in a scan of {MATRIX_SCAN} "
                            f"desk matrices; the previous window is further "
                            f"back than this rule looks"),
                 # The numbers as fields, not only inside the sentence: a
                 # reader deciding whether MATRIX_SCAN is too small cannot
                 # parse prose to find out how full the scan was.
                 "examined": len(runs), "scan_limit": MATRIX_SCAN,
                 "window_hash": current_hash})
        return []          # the first window ever has nothing to have changed

    held, book_fault = book if book is not None else held_names(
        session, offline)
    if book_fault:
        registry.record_event(HELD_RECORD_TRIGGER + "_skipped",
                              {"reason": book_fault,
                               "window_hash": current_hash})
        return []
    # A position outside the mandate universe has no row in either window, so
    # the comparison below passes over it — correctly, there is nothing to
    # compare. Saying nothing about it would be the lie: the desk is carrying
    # a name its own qualitative record does not cover.
    unwatched = sorted(set(held) - set(current_rows))
    if unwatched:
        registry.record_event(
            HELD_RECORD_TRIGGER + "_unwatched",
            {"tickers": unwatched, "window_hash": current_hash})
    previous_hash = str(previous.get("window_hash") or "")
    previous_rows = previous.get("rows")
    changes = held_record_changes(
        current_rows, previous_rows if isinstance(previous_rows, Mapping) else {},
        held)

    template_id = TRIGGER_TEMPLATE.get(HELD_RECORD_TRIGGER)
    created: list[dict] = []
    for change in changes:
        payload = dict(change, window_hash=current_hash,
                       previous_window_hash=previous_hash, as_of=as_of)
        # The existing trigger key shape — kind|trading date|scope|state — so
        # the age rule and the budget scan read it the way they read every
        # other one. The scope is the ticker and the state is the window pair,
        # which is what "one task per ticker per window" means.
        dedupe = (f"{HELD_RECORD_TRIGGER}|{as_of}|{change['ticker']}|"
                  f"{previous_hash}->{current_hash}")
        task_id = uuid.uuid4().hex[:16]
        if not registry.create_atlas_task(
                task_id, dedupe, HELD_RECORD_TRIGGER, payload, template_id):
            continue
        registry.record_event(
            "atlas_task", {"task_id": task_id, "trigger": HELD_RECORD_TRIGGER,
                           "action": "workflow", "template_id": template_id,
                           "source": "lookup"})
        created.append({"task_id": task_id, "trigger": HELD_RECORD_TRIGGER,
                        "action": "workflow", "ticker": change["ticker"],
                        "reason": change["reason"]})
    return created


def _window_fault(matrix: Mapping) -> str:
    """The reason this window cannot be compared, or ``""``."""
    for key in ("news_error", "calendar_error"):
        value = str(matrix.get(key) or "").strip()
        if value:
            return f"{key}: {value[:200]}"
    return ""


def _count(row: Mapping, column: str) -> int:
    try:
        return int(row.get(column) or 0)
    except (TypeError, ValueError):
        return 0


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

    # The last QUESTION the held-record rule finished examining, for the life
    # of this owner process: the window and the held set together. The window
    # alone is not the question — a position opened while the news window
    # stands still is a name nothing has ever compared, and keying on the hash
    # left it unexamined until the next distinct window, which on a quiet tape
    # can be the next day. The book is therefore read every tick (there is no
    # cheaper tell for a position change) and the registry scan and the mint
    # are what an unchanged question saves. The registry's dedupe key is still
    # what makes a second task impossible — this only stops paying to ask.
    # Written under the same dispatch lock every other tick state is.
    examined: tuple[str, frozenset[str]] | None = None

    def tick() -> dict:
        # Network work is deliberately outside the owner dispatch lock. The
        # returned payload contains no registry state; grounding and composition
        # happen below while the one-writer boundary is held.
        fetch_news = getattr(session, "fetch_desk_news", None)
        prefetched_news = fetch_news(offline) if callable(fetch_news) else None
        live_autonomous = autonomous
        # This tick's qualitative window, carried from the log below to the
        # observe: the held-record rule needs the window the log just wrote,
        # and the two run in different closures of the same lock phase.
        matrix_payload: dict | None = None

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
            # Before the observe, for the reap's own reason: expiry marks a
            # week-idle workflow `stale`, which is a resolved state, so the
            # reconciliation inside `atlas_observe` frees the task bound to it
            # on this same tick rather than the next one. Guarded the same way
            # — a desk that cannot retire old work must still observe.
            expired, expire_error = None, ""
            expire = getattr(session, "expire_stale_atlas_work", None)
            if callable(expire):
                try:
                    expired = expire()
                except Exception as exc:
                    expire_error = str(exc)[:200]
            # `handed` is empty unless the reasoner ran, so a desk with the
            # flag off calls exactly what it called before.
            result = session.atlas_observe(offline, **handed)
            # Beside the observe rather than inside it: the supervisor's
            # triggers are evaluated from owner FACTS, and the qualitative
            # window is not one of them — it is a per-window record only this
            # tick holds. The tasks join `created_tasks` because everything
            # downstream — the announcement, the autonomous start below — reads
            # a trigger task from that list and from the registry row, not from
            # where it was minted.
            nonlocal examined
            window_hash = str((matrix_payload or {}).get("window_hash") or "")
            if matrix_payload is not None:
                try:
                    held = held_names(session, offline)
                    question = (window_hash, frozenset(held[0]))
                    if question != examined:
                        result["created_tasks"] = list(
                            result.get("created_tasks") or []) + \
                            mint_held_record_tasks(
                                session, matrix_payload, offline=offline,
                                book=held)
                        # Only after it returned: a question the rule threw on
                        # has not been examined, and the next tick must ask it
                        # again.
                        examined = question
                except Exception as exc:
                    # Its own key, never the value's, for the reason the reap
                    # error above carries one: a field that changes JSON type
                    # mid-run poisons the tick for a typed client.
                    result["held_record_error"] = str(exc)[:200]
                    session.registry.record_event(
                        HELD_RECORD_TRIGGER + "_failed",
                        {"error": str(exc)[:400]})
            if expired is not None:
                result["expired"] = expired
            if expire_error:
                result["expire_error"] = expire_error
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
                            logged = matrix(offline)
                            if isinstance(logged, dict):
                                matrix_payload = logged
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
