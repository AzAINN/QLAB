"""DuckDB registry — the single embedded source of truth.

One local file (``.lab/registry.duckdb``), no server process. It doubles as the
research registry (runs / moment_sets / objectives / solutions / backtests /
decisions) *and* the trader's book (account / positions / plans / orders), so the
backtest and the live paper loop share one provenance trail (research-plan
§2.1, §8).

Design commitments:

* **Everything is a run.** ``log_run`` content-hashes the spec; re-logging the
  same spec is idempotent (caching + provenance for free).
* **Trial counting is a schema feature.** ``trial_count`` powers deflated Sharpe.
* **Per-instance isolation.** Pass ``path=':memory:'`` for a throwaway DB — this
  is how tests stay order-independent (research-plan §10 revision).
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import duckdb

from qlab.core.types import Decision, MomentSet, Objective, SolveResult, _jsonable
from qlab.paths import state_path


WORKFORCE_PHASES = ("analyst", "challenger", "optimizer", "referee", "reporter")
WORKFLOW_RESUMABLE_STATUSES = frozenset({
    "interrupted", "failed", "blocked",
})
WORKFLOW_TERMINAL_STATUSES = frozenset({
    "complete", "abandoned",
})
# Dependency DAG for the standard pipeline. The bounded debate makes the
# challenger a true upstream of the optimizer: an amendment recorded in the
# challenger phase's artifacts must be able to replace the optimizer's inputs,
# so the optimizer may not start until the debate has settled. (Panels get
# their own instance DAG where branch optimizers depend only on their own
# analysts and the judge is the join point.)
_WORKFORCE_DEPS = {
    "analyst": (),
    "challenger": ("analyst",),
    "optimizer": ("analyst", "challenger"),
    "referee": ("challenger", "optimizer"),
    "reporter": ("referee",),
    "news-analyst": (),
}
_WORKFORCE_REQUIRED_ARTIFACTS = {
    # regime + regime_summary are required so the run always carries the analyst's
    # five-level regime call and the news-driven reasoning the desk surfaces.
    "analyst": ("moment_set_id", "objective_id", "decision_id",
                "regime", "regime_summary"),
    "challenger": ("challenger_view",),
    "optimizer": ("targets", "algorithm_id"),
    "judge": ("winner_phase", "winning_targets", "evidence"),
    "referee": ("verdict", "verdict_id", "targets"),
    "reporter": ("recommendation",),
    # Atlas's qualitative helper. It reads a grounded window it is handed and
    # produces a view; it has no dependencies and reaches no gate, because it
    # never produces targets and so can never approach the approval path.
    "news-analyst": ("news_view",),
}
_MAX_PANEL_VARIANTS = 5

# The book an unqualified account call means. Callers that know their venue pass
# it; this keeps the simulator — which is what every offline test and the demo
# run against — working without threading a book through every call site.
DEFAULT_BOOK = "simulated_paper"

# Which statuses an approval may hold *before* it is moved to each status. The
# terminal ones — rejected, expired, consumed, invalidated — appear in no
# right-hand side, which is what makes them terminal: a spent or refused
# approval can never be revived to authorise a fill.
_APPROVAL_TRANSITIONS: dict[str, frozenset[str]] = {
    # A challenge re-opens a decision that has not been acted on yet.
    "pending": frozenset({"pending"}),
    "approved": frozenset({"pending"}),
    "rejected": frozenset({"pending"}),
    "expired": frozenset({"pending"}),
    "consumed": frozenset({"approved"}),
    # Invalidation says the plan this covered has drifted, which can happen
    # while the decision is still outstanding — a pending approval whose book
    # moved must not remain approvable.
    "invalidated": frozenset({"pending", "approved"}),
}


def _phase_type(phase: str) -> str:
    """'analyst-3' → 'analyst'; panel branches share their base type's rules."""
    base, dash, suffix = phase.rpartition("-")
    if dash and suffix.isdigit():
        return base
    return phase


def validate_phase_graph(
    phases: tuple[str, ...],
    deps: dict[str, tuple[str, ...]] | None = None,
) -> None:
    """Raise unless every phase in `phases` could actually run to completion.

    Naming every phase correctly is not enough. A phase whose dependency is
    absent from the set can never start, so the workflow sits ``running``
    forever -- which is exactly how a reduced graph like
    ``(analyst, challenger, referee, reporter)`` orphans its referee. Rejecting
    that at creation turns a silent deadlock into a loud error.

    `deps` is the dependency map to validate against; panels pass their instance
    DAG, where branch optimizers depend on their own analysts.
    """
    if not phases or len(set(phases)) != len(phases):
        raise ValueError("workflow phases must be non-empty and unique")
    unknown = {
        phase for phase in phases
        if _phase_type(phase) not in _WORKFORCE_REQUIRED_ARTIFACTS
    }
    if unknown:
        raise ValueError(f"unknown workforce phases: {sorted(unknown)}")

    deps_map = deps if deps is not None else _WORKFORCE_DEPS
    present = set(phases)
    for phase in phases:
        for dependency in deps_map.get(phase, ()):
            if dependency not in present:
                raise ValueError(
                    f"phase {phase!r} depends on {dependency!r}, which the "
                    f"declared graph omits; it could never start"
                )


def panel_phases(n_variants: int) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    """Phases + dependency DAG for a tournament of analyst variants.

    Each branch runs its own analyst → optimizer; the judge joins every
    branch and picks a winner on evidence; the referee gates the winner and
    the reporter closes. The judgment-is-defended property is preserved by
    the judge's completion contract instead of a per-branch challenger.
    """
    if not 2 <= n_variants <= _MAX_PANEL_VARIANTS:
        raise ValueError(
            f"panel needs 2..{_MAX_PANEL_VARIANTS} variants, got {n_variants}")
    analysts = tuple(f"analyst-{i}" for i in range(1, n_variants + 1))
    optimizers = tuple(f"optimizer-{i}" for i in range(1, n_variants + 1))
    phases = (*analysts, *optimizers, "judge", "referee", "reporter")
    deps: dict[str, tuple[str, ...]] = {phase: () for phase in analysts}
    for analyst, optimizer in zip(analysts, optimizers):
        deps[optimizer] = (analyst,)
    deps["judge"] = optimizers
    deps["referee"] = ("judge",)
    deps["reporter"] = ("referee",)
    return phases, deps


def targets_hash(targets: dict[str, float]) -> str:
    """Canonical content hash of a target-weights dict.

    THE canonical definition — both verdict writers (the referee) and verdict
    checkers (``execute_plan``) must import this, so a PASS can never be
    silently applied to a different set of targets than the one reviewed.
    """
    # Full float precision via repr: rounding to a few decimals would let two
    # economically-distinct weight vectors collide onto one hash and satisfy a
    # PASS created for the other. The hash must be exact.
    key = ",".join(f"{k}:{float(v)!r}" for k, v in sorted(targets.items()))
    return hashlib.sha256(key.encode()).hexdigest()[:16]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id VARCHAR PRIMARY KEY, kind VARCHAR, spec JSON, created_at VARCHAR);
CREATE TABLE IF NOT EXISTS moment_sets (
    hash VARCHAR PRIMARY KEY, as_of VARCHAR, n INTEGER, tickers JSON,
    summary JSON, created_at VARCHAR);
CREATE TABLE IF NOT EXISTS objectives (
    hash VARCHAR PRIMARY KEY, form VARCHAR, tickers JSON, params JSON,
    created_at VARCHAR);
CREATE TABLE IF NOT EXISTS solutions (
    solution_id VARCHAR PRIMARY KEY, run_id VARCHAR, arm_id VARCHAR,
    objective_hash VARCHAR, objective_form VARCHAR, solver VARCHAR,
    objective_value DOUBLE, wall_clock_s DOUBLE, weights JSON,
    diagnostics JSON, created_at VARCHAR);
CREATE TABLE IF NOT EXISTS backtests (
    bt_id VARCHAR PRIMARY KEY, run_id VARCHAR, arm_id VARCHAR,
    metrics JSON, artifact_hash VARCHAR, created_at VARCHAR);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id VARCHAR PRIMARY KEY, as_of VARCHAR, kind VARCHAR, choice JSON,
    rationale VARCHAR, challenger_view VARCHAR, realized_outcome JSON,
    reflection VARCHAR, created_at VARCHAR);
-- Keyed by book: the high-water mark is per-venue, and one shared row turns a
-- venue switch into a fabricated drawdown that trips the kill switch.
CREATE TABLE IF NOT EXISTS account (
    book VARCHAR PRIMARY KEY, cash DOUBLE, high_water_mark DOUBLE,
    halted BOOLEAN, updated_at VARCHAR);
CREATE TABLE IF NOT EXISTS positions (
    ticker VARCHAR PRIMARY KEY, qty DOUBLE, avg_price DOUBLE, updated_at VARCHAR);
CREATE TABLE IF NOT EXISTS plans (
    plan_id VARCHAR PRIMARY KEY, decision_id VARCHAR, state VARCHAR,
    targets JSON, pre_trade JSON, created_at VARCHAR);
CREATE TABLE IF NOT EXISTS orders (
    client_order_id VARCHAR PRIMARY KEY, plan_id VARCHAR, ticker VARCHAR,
    side VARCHAR, notional DOUBLE, state VARCHAR, created_at VARCHAR);
CREATE TABLE IF NOT EXISTS events (
    event_id VARCHAR PRIMARY KEY, ts VARCHAR, kind VARCHAR, payload JSON);
CREATE SEQUENCE IF NOT EXISTS verdict_seq;
CREATE TABLE IF NOT EXISTS verdicts (
    verdict_id VARCHAR PRIMARY KEY, decision_id VARCHAR, verdict VARCHAR,
    reasons JSON, source VARCHAR, created_at VARCHAR, targets_hash VARCHAR,
    seq BIGINT);
CREATE TABLE IF NOT EXISTS workflows (
    workflow_id VARCHAR PRIMARY KEY, kind VARCHAR, status VARCHAR,
    current_phase VARCHAR, request JSON, result JSON,
    created_at VARCHAR, updated_at VARCHAR);
CREATE TABLE IF NOT EXISTS workflow_steps (
    step_id VARCHAR PRIMARY KEY, workflow_id VARCHAR, seq INTEGER,
    phase VARCHAR, agent VARCHAR, status VARCHAR, summary VARCHAR,
    artifacts JSON, started_at VARCHAR, completed_at VARCHAR,
    updated_at VARCHAR);
CREATE TABLE IF NOT EXISTS data_permits (
    permit_id VARCHAR PRIMARY KEY, snapshot_id VARCHAR, purpose VARCHAR,
    provider VARCHAR, feed VARCHAR, as_of VARCHAR, permit JSON,
    eligible_for_execution BOOLEAN, created_at VARCHAR);
CREATE TABLE IF NOT EXISTS atlas_state (
    manager_id VARCHAR PRIMARY KEY, mode VARCHAR, state VARCHAR,
    current_task_id VARCHAR, last_wake_reason VARCHAR, last_brief_at VARCHAR,
    blocked_reason VARCHAR, coordinator_session_id VARCHAR, updated_at VARCHAR);
CREATE TABLE IF NOT EXISTS atlas_tasks (
    task_id VARCHAR PRIMARY KEY, dedupe_key VARCHAR UNIQUE, trigger_kind VARCHAR,
    trigger_payload JSON, template_id VARCHAR, status VARCHAR, workflow_id VARCHAR,
    conclusion JSON, error VARCHAR, attempt_count INTEGER,
    created_at VARCHAR, started_at VARCHAR, completed_at VARCHAR, updated_at VARCHAR);
CREATE TABLE IF NOT EXISTS authority_grants (
    grant_id VARCHAR PRIMARY KEY, mode VARCHAR, allowed_universe JSON,
    max_notional DOUBLE, max_turnover DOUBLE, max_orders INTEGER,
    allowed_policy VARCHAR, valid_from VARCHAR, expires_at VARCHAR,
    revoked_at VARCHAR, revoked_reason VARCHAR, granted_by VARCHAR,
    created_at VARCHAR);
CREATE TABLE IF NOT EXISTS debates (
    debate_id VARCHAR PRIMARY KEY, workflow_id VARCHAR,
    original_decision_id VARCHAR, status VARCHAR, max_rounds INTEGER,
    panel_snapshot_id VARCHAR, material_claims JSON, adjudication JSON,
    created_at VARCHAR, updated_at VARCHAR);
CREATE TABLE IF NOT EXISTS debate_turns (
    turn_id VARCHAR PRIMARY KEY, debate_id VARCHAR, round INTEGER, role VARCHAR,
    claim_id VARCHAR, position VARCHAR, argument VARCHAR, evidence_refs JSON,
    created_at VARCHAR);
CREATE TABLE IF NOT EXISTS reflection_lessons (
    lesson_id VARCHAR PRIMARY KEY, decision_id VARCHAR, outcome_hash VARCHAR,
    lesson JSON, prompt_version VARCHAR, model_record_id VARCHAR,
    stale BOOLEAN, created_at VARCHAR);
CREATE TABLE IF NOT EXISTS model_invocations (
    invocation_id VARCHAR PRIMARY KEY, role VARCHAR, requested_tier VARCHAR,
    resolved_model VARCHAR, backend VARCHAR, status VARCHAR, latency_ms DOUBLE,
    tokens BIGINT, fallback_reason VARCHAR, created_at VARCHAR);
CREATE TABLE IF NOT EXISTS approval_requests (
    approval_id VARCHAR PRIMARY KEY, task_id VARCHAR, plan_id VARCHAR,
    plan_digest VARCHAR, decision_id VARCHAR, targets_hash VARCHAR,
    data_permit_id VARCHAR, broker VARCHAR, book_revision VARCHAR,
    expected_cost JSON, summary JSON, status VARCHAR, challenge_digest VARCHAR,
    expires_at VARCHAR, decided_at VARCHAR, consumed_at VARCHAR,
    invalidated_reason VARCHAR, created_at VARCHAR);
-- `book` is the broker the equity belongs to. Two books' equity levels can
-- never compose one return series, so every mark carries its own; the
-- idempotency key stays (ts, source).
-- The book is part of the identity: two books legitimately hold an equity at
-- the same timestamp from the same source, and they are different facts.
CREATE TABLE IF NOT EXISTS equity_marks (
    ts VARCHAR, source VARCHAR, book VARCHAR, equity DOUBLE, cash DOUBLE,
    PRIMARY KEY (ts, source, book));

-- The news archive. Headlines were fetched, grounded, read and discarded, so
-- "what did the record say about X" had nothing to answer from.
--
-- `body_text`, never `summary`: _rows json.loads any column literally named
-- summary, so a body of '2024' would read back as the integer 2024.
--
-- Every timestamp is CHECKed against the canonical shape because every
-- point-in-time read is a lexicographic string compare — one row stored as
-- '2026-07-31T12:00:00Z' or with microseconds silently sorts wrong forever.
--
-- search_text is GENERATED here or never: DuckDB has no STORED generated
-- columns and refuses ALTER TABLE ADD of a generated one, so it cannot be
-- added later. strip_accents is what lets 'Nestle' match 'Nestlé'.
CREATE TABLE IF NOT EXISTS news_items (
    item_hash VARCHAR PRIMARY KEY,
    published VARCHAR NOT NULL CHECK (published LIKE '____-__-__T__:__:__+00:00'),
    first_seen VARCHAR NOT NULL CHECK (first_seen LIKE '____-__-__T__:__:__+00:00'),
    last_seen VARCHAR NOT NULL CHECK (last_seen LIKE '____-__-__T__:__:__+00:00'),
    seen_count BIGINT NOT NULL,
    provider VARCHAR NOT NULL,
    source VARCHAR NOT NULL,
    source_tier VARCHAR NOT NULL,
    headline VARCHAR NOT NULL,
    body_text VARCHAR,
    url VARCHAR,
    synthetic BOOLEAN NOT NULL,
    search_text VARCHAR GENERATED ALWAYS AS (
        lower(strip_accents(headline || ' ' || coalesce(body_text, '')))) VIRTUAL);

-- Insert-only union rather than a JSON column on news_items. content_hash does
-- not cover tickers, and the mandate universe changes over time, so the same
-- story re-fetched after a universe change maps to a different ticker set —
-- last-write-wins would erase the earlier mapping.
CREATE TABLE IF NOT EXISTS news_item_tickers (
    item_hash VARCHAR, ticker VARCHAR, in_universe BOOLEAN,
    PRIMARY KEY (item_hash, ticker));

-- What cited what. cited_by_kind is deliberately restricted to tables that
-- exist; a kind naming no table would leave cited_by_id resolving to nothing.
CREATE TABLE IF NOT EXISTS news_citations (
    item_hash VARCHAR, cited_by_kind VARCHAR, cited_by_id VARCHAR,
    created_at VARCHAR,
    PRIMARY KEY (item_hash, cited_by_kind, cited_by_id));
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _control_summary(current: str, label: str, reason: str) -> str:
    """Append one bounded lifecycle note without erasing the phase evidence."""
    note = f"{label}: {reason.strip()}"
    existing = current.strip()
    if not existing:
        return note[:4000]
    if note in existing:
        return existing[:4000]
    room = max(0, 4000 - len(note) - 2)
    return f"{existing[:room].rstrip()}\n\n{note}"[:4000]


def _j(obj: Any) -> str:
    return json.dumps(_jsonable(obj), sort_keys=True, separators=(",", ":"))


def _u(s: Any) -> Any:
    if s is None:
        return None
    return json.loads(s) if isinstance(s, str) else s


class Registry:
    def __init__(self, path: str | Path | None = None):
        if path == ":memory:":
            self.path = ":memory:"
        else:
            p = Path(path) if path else state_path("registry.duckdb")
            p.parent.mkdir(parents=True, exist_ok=True)
            self.path = str(p)
        self.con = duckdb.connect(self.path)
        # Both default to TRUE. One stray PRAGMA inside the single-writer
        # process would then download an extension over the network — a silent
        # network fallback (invariant 4) that makes the offline suite pass on a
        # warmed machine and fail on a clean one.
        self.con.execute("SET autoinstall_known_extensions=false")
        self.con.execute("SET autoload_known_extensions=false")
        self.con.execute(_SCHEMA)
        self.con.execute("ALTER TABLE backtests ADD COLUMN IF NOT EXISTS objective VARCHAR")
        # existing dev DBs may predate these columns; _SCHEMA alone won't add
        # them to an already-created table, hence the explicit ALTERs here.
        self.con.execute("ALTER TABLE verdicts ADD COLUMN IF NOT EXISTS targets_hash VARCHAR")
        self.con.execute("ALTER TABLE verdicts ADD COLUMN IF NOT EXISTS seq BIGINT")
        self.con.execute("ALTER TABLE plans ADD COLUMN IF NOT EXISTS legs VARCHAR")
        # Order lifecycle: broker-truth fill accounting (P3).
        self.con.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS filled_qty DOUBLE")
        self.con.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS avg_fill_price DOUBLE")
        self.con.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS fee DOUBLE")
        self.con.execute("ALTER TABLE equity_marks ADD COLUMN IF NOT EXISTS book VARCHAR")
        self._widen_equity_mark_identity()
        self._partition_account_by_book()

    def _partition_account_by_book(self) -> None:
        """Move a pre-book `account` row onto the book key.

        The old table held one row (`id=1`) shared by every venue, so both
        brokers ratcheted one high-water mark. An Alpaca paper account near
        $32.6k set that mark, and the next read of the $10k simulated book
        computed a 69% drawdown, tripped the kill switch, halted the desk and
        blocked the reporter — with nothing having lost money.

        The carried row keeps its cash but its high-water mark is reset to that
        cash, and any halt is cleared. A mark inherited from another venue is
        not a peak this book reached, so carrying it forward would carry the
        false drawdown across the migration — the one thing this must not do.
        """
        columns = {
            row[0] for row in self.con.execute(
                "SELECT column_name FROM duckdb_columns() "
                "WHERE table_name='account'").fetchall()
        }
        if "book" in columns:
            return
        legacy = self.con.execute(
            "SELECT cash, high_water_mark, halted FROM account").fetchall()
        with self.transaction():
            self.con.execute("""
                CREATE TABLE account_rekeyed (
                    book VARCHAR PRIMARY KEY, cash DOUBLE,
                    high_water_mark DOUBLE, halted BOOLEAN,
                    updated_at VARCHAR);
            """)
            if legacy:
                cash = float(legacy[0][0] or 0.0)
                self.con.execute(
                    "INSERT INTO account_rekeyed VALUES (?,?,?,?,?)",
                    [DEFAULT_BOOK, cash, cash, False, _now()])
            self.con.execute("DROP TABLE account")
            self.con.execute("ALTER TABLE account_rekeyed RENAME TO account")

    def _widen_equity_mark_identity(self) -> None:
        """Bring a pre-book equity_marks primary key up to (ts, source, book).

        The `book` column was added by ALTER, which cannot widen the key, so an
        existing database still keys marks on (ts, source). Two books hold an
        equity at the same timestamp from the same source as a matter of
        course — a daily mark, a backfill — and `INSERT OR IGNORE` silently
        discarded the second one, reporting `{"backfilled": 0}`: identical to
        "already up to date", for a book whose series was in fact empty.

        DuckDB cannot alter a primary key in place, so the table is rebuilt.
        This is a no-op on a database created from the current schema.
        """
        pk = self.con.execute(
            "SELECT constraint_column_names FROM duckdb_constraints() "
            "WHERE table_name='equity_marks' AND constraint_type='PRIMARY KEY'"
        ).fetchall()
        if not pk or "book" in list(pk[0][0]):
            return
        with self.transaction():
            self.con.execute("""
                CREATE TABLE equity_marks_rekeyed (
                    ts VARCHAR, source VARCHAR, book VARCHAR,
                    equity DOUBLE, cash DOUBLE,
                    PRIMARY KEY (ts, source, book));
            """)
            # DISTINCT because the old key permitted only one row per
            # (ts, source) anyway; this is a widening, never a merge.
            self.con.execute(
                "INSERT INTO equity_marks_rekeyed "
                "SELECT DISTINCT ts, source, COALESCE(book, ''), equity, cash "
                "FROM equity_marks")
            self.con.execute("DROP TABLE equity_marks")
            self.con.execute(
                "ALTER TABLE equity_marks_rekeyed RENAME TO equity_marks")

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> "Registry":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Commit a small registry mutation atomically, rolling back on error."""
        self.con.execute("BEGIN TRANSACTION")
        try:
            yield
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        else:
            self.con.execute("COMMIT")

    # -- runs ---------------------------------------------------------------
    def log_run(self, kind: str, spec: dict) -> str:
        run_id = hashlib.sha256(_j({"kind": kind, "spec": spec}).encode()).hexdigest()[:16]
        self.con.execute(
            "INSERT INTO runs VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
            [run_id, kind, _j(spec), _now()],
        )
        return run_id

    # -- research objects ---------------------------------------------------
    def log_moment_set(self, ms: MomentSet) -> str:
        h = ms.content_hash()
        self.con.execute(
            "INSERT INTO moment_sets VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            [h, str(ms.as_of), ms.n, _j(ms.tickers), _j(ms.summary()), _now()],
        )
        return h

    def log_objective(self, obj: Objective) -> str:
        h = obj.content_hash()
        params = {"skew_lambda": obj.skew_lambda, "kurt_lambda": obj.kurt_lambda,
                  "risk_aversion": obj.risk_aversion, "extra": _jsonable(obj.extra)}
        self.con.execute(
            "INSERT INTO objectives VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
            [h, obj.form, _j(obj.tickers), _j(params), _now()],
        )
        return h

    def log_solution(
        self, run_id: str, arm_id: str, result: SolveResult,
        objective_hash: str = "", objective_form: str = "",
    ) -> str:
        sid = uuid.uuid4().hex[:16]
        self.con.execute(
            "INSERT INTO solutions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [sid, run_id, arm_id, objective_hash, objective_form, result.solver,
             float(result.objective_value), float(result.wall_clock_s),
             _j(dict(zip(result.weights.tickers, result.weights.values))),
             _j(result.diagnostics), _now()],
        )
        return sid

    def log_backtest(self, run_id: str, arm_id: str, metrics: dict,
                     artifact_hash: str = "", objective: str = "") -> str:
        bid = uuid.uuid4().hex[:16]
        self.con.execute(
            "INSERT INTO backtests (bt_id, run_id, arm_id, metrics, artifact_hash, "
            "objective, created_at) VALUES (?,?,?,?,?,?,?)",
            [bid, run_id, arm_id, _j(metrics), artifact_hash, objective, _now()],
        )
        return bid

    # -- decisions + reflection loop ---------------------------------------
    def log_decision(self, decision: Decision) -> str:
        did = decision.decision_id or decision.content_hash()
        self.con.execute(
            "INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            [did, str(decision.as_of), decision.kind, _j(decision.choice),
             decision.rationale, decision.challenger_view,
             _j(decision.realized_outcome), decision.reflection, _now()],
        )
        return did

    def update_reflection(self, decision_id: str, realized_outcome: dict,
                          reflection: str) -> None:
        # Write-once: a resolved outcome is immutable, so a re-resolution is a
        # silent no-op rather than an overwrite (the same pending idiom as
        # ``pending_decisions`` — SQL NULL or the JSON literal ``null``).
        self.con.execute(
            "UPDATE decisions SET realized_outcome=?, reflection=? "
            "WHERE decision_id=? AND (realized_outcome IS NULL "
            "OR CAST(realized_outcome AS VARCHAR) = 'null')",
            [_j(realized_outcome), reflection, decision_id],
        )

    def attach_challenger_view(self, decision_id: str, challenger_view: str) -> None:
        """Attach the adversarial view after the analyst's initial judgment."""
        view = challenger_view.strip()
        if not view:
            raise ValueError("challenger_view must not be empty")
        exists = self._rows(
            "SELECT decision_id FROM decisions WHERE decision_id=?", [decision_id]
        )
        if not exists:
            raise KeyError(f"unknown decision_id {decision_id!r}")
        self.con.execute(
            "UPDATE decisions SET challenger_view=? WHERE decision_id=?",
            [view, decision_id],
        )
        self.record_event("challenger_view_attached", {"decision_id": decision_id})

    def recent_decisions(self, kind: str | None = None, limit: int = 10) -> list[dict]:
        q = "SELECT * FROM decisions"
        params: list = []
        if kind:
            q += " WHERE kind=?"
            params.append(kind)
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return self._rows(q, params)

    def recall_similar_decisions(
        self,
        fingerprint: dict,
        kind: str | None = None,
        limit: int = 10,
        *,
        as_of: str | None = None,
        min_similarity: float = 0.0,
    ) -> list[dict]:
        """Return reflected decisions nearest to a regime fingerprint.

        The two percentile fields are already normalized to ``[0, 1]``, so
        their mean absolute difference is the numeric distance. A matching
        regime label adds a small bonus; the resulting ``similarity_score`` is
        normalized back to ``[0, 1]``. Older decisions without a complete,
        valid fingerprint are not comparable and are skipped.

        Point-in-time (no look-ahead): when ``as_of`` is given, a candidate is
        recallable only if it was BOTH decided and fully resolved strictly
        before ``as_of`` — a decision from the query's future, or one whose
        outcome window closes on/after ``as_of``, would leak future information
        into the recall and is excluded. ``min_similarity`` drops weak matches
        so unrelated regimes are not forced into context.
        """
        numeric_fields = ("vol_percentile", "turbulence_percentile")
        query_values: dict[str, float] = {}
        for field in numeric_fields:
            try:
                value = float(fingerprint[field])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"fingerprint.{field} must be a numeric percentile"
                ) from exc
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(
                    f"fingerprint.{field} must be finite and within [0, 1]"
                )
            query_values[field] = value

        query_label = str(fingerprint.get("regime_label", "")).strip().lower()
        if not query_label:
            raise ValueError("fingerprint.regime_label must not be empty")
        try:
            limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("limit must be an integer") from exc
        if limit <= 0:
            return []

        q = (
            "SELECT * FROM decisions "
            "WHERE reflection IS NOT NULL AND TRIM(reflection) <> ''"
        )
        params: list = []
        if kind:
            q += " AND kind=?"
            params.append(kind)
        q += " ORDER BY created_at DESC, decision_id DESC"

        as_of_key = str(as_of).strip() if as_of else None

        scored: list[tuple[float, dict]] = []
        for row in self._rows(q, params):
            choice = row.get("choice") or {}
            outcome = row.get("realized_outcome") or {}

            # Point-in-time guard: exclude any candidate decided on/after the
            # query date, or whose outcome window closes on/after it. Either
            # would recall information from the query's own future.
            if as_of_key is not None:
                if str(row.get("as_of") or "") >= as_of_key:
                    continue
                window_end = str(outcome.get("window_end") or "")
                if not window_end or window_end >= as_of_key:
                    continue

            candidate_values: dict[str, float] = {}
            comparable = True
            for field in numeric_fields:
                raw = choice.get(field, outcome.get(field))
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    comparable = False
                    break
                if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                    comparable = False
                    break
                candidate_values[field] = value
            if not comparable:
                continue

            candidate_label = str(
                choice.get("regime_label")
                or choice.get("regime")
                or outcome.get("regime_label")
                or outcome.get("regime_call")
                or ""
            ).strip().lower()
            if not candidate_label:
                continue

            numeric_distance = sum(
                abs(query_values[field] - candidate_values[field])
                for field in numeric_fields
            ) / len(numeric_fields)
            label_bonus = 0.25 if candidate_label == query_label else 0.0
            similarity = (1.0 - numeric_distance + label_bonus) / 1.25
            if similarity < min_similarity:
                continue
            recalled = dict(row)
            recalled["similarity_score"] = float(similarity)
            scored.append((similarity, recalled))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [row for _score, row in scored[:limit]]

    def get_decision(self, decision_id: str) -> dict | None:
        rows = self._rows(
            "SELECT * FROM decisions WHERE decision_id=?", [decision_id]
        )
        return rows[0] if rows else None

    def pending_decisions(self) -> list[dict]:
        """Return unresolved judgments in chronological decision order.

        ``log_decision`` stores a missing outcome as the JSON literal ``null``;
        older registries may contain SQL ``NULL`` instead. Both represent the
        same pending state.
        """
        return self._rows(
            "SELECT * FROM decisions "
            "WHERE realized_outcome IS NULL "
            "OR CAST(realized_outcome AS VARCHAR) = 'null' "
            "ORDER BY as_of, created_at",
            [],
        )

    def _ensure_applied_views_table(self) -> None:
        """Create the calibration ledger without requiring a schema migration."""
        self.con.execute(
            """
            CREATE TABLE IF NOT EXISTS applied_views (
                view_id VARCHAR PRIMARY KEY,
                as_of VARCHAR,
                universe VARCHAR,
                view_kind VARCHAR,
                horizon_days INTEGER,
                view_payload JSON,
                pre_view_baseline JSON,
                run_id VARCHAR,
                score JSON,
                resolved_at VARCHAR,
                created_at VARCHAR
            )
            """
        )
        # Keep the lazy schema safe for a developer database created by an
        # earlier P4.7 checkout with only the persistence columns.
        for column in (
            "view_kind VARCHAR",
            "horizon_days INTEGER",
            "score JSON",
            "resolved_at VARCHAR",
            "created_at VARCHAR",
        ):
            self.con.execute(
                f"ALTER TABLE applied_views ADD COLUMN IF NOT EXISTS {column}"
            )

    def log_applied_view(
        self,
        as_of,
        universe: str,
        view_payload: dict,
        pre_view_baseline,
        run_id: str,
    ) -> str:
        """Persist one applied risk view for later realized calibration."""
        from numbers import Integral, Real

        if not isinstance(universe, str) or not universe.strip():
            raise ValueError("universe must be a non-empty string")
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        if not isinstance(view_payload, dict):
            raise TypeError("view_payload must be a dict")
        if not all(isinstance(field, str) for field in view_payload):
            raise TypeError("view_payload field names must be strings")

        payload = dict(view_payload)
        view_kind = payload.get("type")
        baseline_fields = {
            "vol": "pre_view_vol",
            "corr": "pre_view_corr",
            "tail": "pre_view_tail_mass",
        }
        if view_kind not in baseline_fields:
            raise ValueError("view_payload.type must be vol, corr, or tail")

        raw_horizon = payload.get("horizon_days", payload.get("horizon", 21))
        if isinstance(raw_horizon, bool) or not isinstance(raw_horizon, Integral):
            raise TypeError("view horizon_days must be an integer")
        horizon_days = int(raw_horizon)
        if horizon_days < 2:
            raise ValueError("view horizon_days must be at least 2")

        if isinstance(pre_view_baseline, dict):
            baseline = dict(pre_view_baseline)
            if not all(isinstance(field, str) for field in baseline):
                raise TypeError("pre_view_baseline field names must be strings")
        else:
            if (
                isinstance(pre_view_baseline, bool)
                or not isinstance(pre_view_baseline, Real)
            ):
                raise TypeError("pre_view_baseline must be numeric or a dict")
            value = float(pre_view_baseline)
            if not math.isfinite(value):
                raise ValueError("pre_view_baseline must be finite")
            baseline = {baseline_fields[view_kind]: value}

        try:
            normalized_as_of = datetime.fromisoformat(
                str(as_of).replace("Z", "+00:00")
            ).date().isoformat()
        except (TypeError, ValueError) as exc:
            raise ValueError("as_of must be an ISO date or datetime") from exc

        self._ensure_applied_views_table()
        identity = {
            "as_of": normalized_as_of,
            "universe": universe.strip(),
            "view_payload": payload,
            "pre_view_baseline": baseline,
            "run_id": run_id.strip(),
            "horizon_days": horizon_days,
        }
        view_id = hashlib.sha256(_j(identity).encode()).hexdigest()[:16]
        self.con.execute(
            """
            INSERT INTO applied_views (
                view_id, as_of, universe, view_kind, horizon_days,
                view_payload, pre_view_baseline, run_id, score,
                resolved_at, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT DO NOTHING
            """,
            [
                view_id,
                normalized_as_of,
                universe.strip(),
                view_kind,
                horizon_days,
                _j(payload),
                _j(baseline),
                run_id.strip(),
                None,
                None,
                _now(),
            ],
        )
        return view_id

    def resolve_view_calibration(self, prices) -> dict:
        """Score every elapsed, unresolved risk view exactly once."""
        from dataclasses import asdict

        import numpy as np
        import pandas as pd

        from qlab.news.calibration import (
            ViewScore,
            calibration_summary,
            view_realization,
        )

        if not isinstance(prices, pd.DataFrame):
            raise TypeError("prices must be a pandas DataFrame")

        self._ensure_applied_views_table()
        panel = prices.sort_index()
        index = pd.DatetimeIndex(panel.index)
        if index.has_duplicates:
            raise ValueError("prices index must not contain duplicate dates")

        pending = self.con.execute(
            """
            SELECT view_id, as_of, view_kind, horizon_days, view_payload,
                   pre_view_baseline, run_id
            FROM applied_views
            WHERE score IS NULL
            ORDER BY as_of, created_at, view_id
            """
        ).fetchall()
        for (
            view_id,
            as_of,
            view_kind,
            horizon_days,
            raw_payload,
            raw_baseline,
            run_id,
        ) in pending:
            if horizon_days is None:
                raise ValueError(
                    f"applied view {view_id!r} has no calibration horizon"
                )
            horizon_days = int(horizon_days)
            if horizon_days < 2:
                raise ValueError(
                    f"applied view {view_id!r} has an invalid horizon"
                )

            as_of_ts = pd.Timestamp(as_of)
            future_positions = np.flatnonzero(index > as_of_ts)
            if len(future_positions) < horizon_days:
                continue
            first_future = int(future_positions[0])
            if first_future == 0:
                continue

            payload = _u(raw_payload)
            baseline = _u(raw_baseline)
            if not isinstance(payload, dict) or not isinstance(baseline, dict):
                raise ValueError(
                    f"applied view {view_id!r} has malformed persisted payload"
                )
            if view_kind in {"vol", "tail"}:
                required_tickers = [payload.get("ticker")]
            elif view_kind == "corr":
                required_tickers = [
                    payload.get("ticker_a"),
                    payload.get("ticker_b"),
                ]
            else:
                raise ValueError(
                    f"applied view {view_id!r} has unknown type {view_kind!r}"
                )
            if not all(
                isinstance(ticker, str) and ticker for ticker in required_tickers
            ):
                raise ValueError(
                    f"applied view {view_id!r} has malformed ticker fields"
                )
            missing = [
                ticker for ticker in required_tickers
                if ticker not in panel.columns
            ]
            if missing:
                raise ValueError(
                    f"prices are missing applied-view tickers {missing}"
                )

            realized_positions = np.concatenate(
                ([first_future - 1], future_positions[:horizon_days])
            )
            realized_prices = panel.iloc[realized_positions][required_tickers]
            realized_returns = np.log(
                realized_prices / realized_prices.shift(1)
            ).dropna(how="any")
            if len(realized_returns) < horizon_days:
                continue

            scoring_payload = dict(payload)
            scoring_payload.update(baseline)
            score = view_realization(
                view_kind,
                scoring_payload,
                realized_returns.to_numpy(dtype=float),
                required_tickers,
            )
            score_payload = asdict(score)
            resolved_at = _now()
            self.con.execute(
                """
                UPDATE applied_views
                SET score=?, resolved_at=?
                WHERE view_id=? AND score IS NULL
                """,
                [_j(score_payload), resolved_at, view_id],
            )
            self.record_event(
                "view_calibration_resolved",
                {
                    "view_id": view_id,
                    "run_id": run_id,
                    "score": score_payload,
                },
            )

        stored_scores = []
        for (raw_score,) in self.con.execute(
            """
            SELECT score FROM applied_views
            WHERE score IS NOT NULL
            ORDER BY resolved_at, view_id
            """
        ).fetchall():
            payload = _u(raw_score)
            if not isinstance(payload, dict):
                raise ValueError("applied view has a malformed stored score")
            stored_scores.append(ViewScore(**payload))
        return calibration_summary(stored_scores)

    # -- trial counting (deflated Sharpe) -----------------------------------
    def trial_count(self, objective_form: str | None = None) -> int:
        if objective_form:
            r = self.con.execute(
                "SELECT COUNT(*) FROM solutions WHERE objective_form=?",
                [objective_form],
            ).fetchone()
        else:
            r = self.con.execute("SELECT COUNT(*) FROM solutions").fetchone()
        return int(r[0]) if r else 0

    def backtest_trial_count(self) -> int:
        r = self.con.execute("SELECT COUNT(DISTINCT arm_id) FROM backtests").fetchone()
        return int(r[0]) if r else 0

    def backtest_arm_ids(self, exclude_objectives: tuple[str, ...] = ("sixty_forty",)) -> set[str]:
        """Distinct arm ids counted as DSR trials.

        Excludes benchmark objectives (``exclude_objectives``) and any arm
        whose persisted objective carries the ``:research`` suffix — those are
        ``research_only`` arms (e.g. the vol-target overlay) that get a full
        backtest but can never reach the live trader, so they must not
        inflate the trial count.
        """
        ph = ",".join("?" for _ in exclude_objectives) or "''"
        rows = self.con.execute(
            f"SELECT DISTINCT arm_id FROM backtests "
            f"WHERE COALESCE(objective, '') NOT IN ({ph}) "
            f"AND COALESCE(objective,'') NOT LIKE '%:research'",
            list(exclude_objectives)).fetchall()
        return {r[0] for r in rows}

    # -- reporting ----------------------------------------------------------
    def list_runs(self, limit: int = 20) -> list[dict]:
        return self._rows("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", [limit])

    def report(self, run_id: str) -> dict:
        return {
            "run": self._rows("SELECT * FROM runs WHERE run_id=?", [run_id]),
            "solutions": self._rows(
                "SELECT arm_id, solver, objective_value, wall_clock_s, weights, diagnostics "
                "FROM solutions WHERE run_id=? ORDER BY created_at", [run_id]),
            "backtests": self._rows(
                "SELECT arm_id, metrics FROM backtests WHERE run_id=? ORDER BY created_at",
                [run_id]),
        }

    # -- account / positions ------------------------------------------------
    #
    # The account is partitioned by book for the same reason equity marks are:
    # the high-water mark is per-venue, and sharing one row across books turns a
    # venue switch into a fabricated loss. An Alpaca paper account near $32.6k
    # ratcheted the shared mark, and the next read of the $10k simulated book
    # computed a 69% drawdown — which tripped the kill switch, halted the desk,
    # and blocked the reporter. Nothing had lost money.
    def init_account(self, cash: float, book: str = DEFAULT_BOOK) -> None:
        self.con.execute(
            "INSERT INTO account (book, cash, high_water_mark, halted, updated_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
            [str(book), cash, cash, False, _now()],
        )

    def get_account(self, book: str = DEFAULT_BOOK) -> dict:
        r = self._rows("SELECT * FROM account WHERE book=?", [str(book)])
        return r[0] if r else {}

    def get_positions(self) -> dict[str, dict]:
        rows = self._rows("SELECT * FROM positions", [])
        return {r["ticker"]: r for r in rows}

    def apply_fill(self, ticker: str, dqty: float, price: float,
                   cash_delta: float, book: str = DEFAULT_BOOK) -> None:
        pos = self.get_positions().get(ticker)
        if pos:
            new_qty = pos["qty"] + dqty
            new_avg = price if new_qty == 0 else (
                (pos["qty"] * pos["avg_price"] + dqty * price) / new_qty
                if new_qty != 0 else price)
            self.con.execute(
                "UPDATE positions SET qty=?, avg_price=?, updated_at=? WHERE ticker=?",
                [new_qty, new_avg, _now(), ticker])
        else:
            self.con.execute("INSERT INTO positions VALUES (?,?,?,?)",
                             [ticker, dqty, price, _now()])
        self.con.execute(
            "UPDATE account SET cash = cash + ?, updated_at=? WHERE book=?",
            [cash_delta, _now(), str(book)])

    def set_halt(self, halted: bool, book: str = DEFAULT_BOOK) -> None:
        """Halt or release one book. A halt on one venue is not a halt on all."""
        self.con.execute("UPDATE account SET halted=?, updated_at=? WHERE book=?",
                         [halted, _now(), str(book)])

    def reset_book(self, cash: float, book: str) -> None:
        """Discard one book: positions, orders, its marks, and the account state.

        That book's equity marks go with it. Keeping them would splice the
        discarded book's equity level onto the fresh one, and the first mark
        after a reset would read as a market loss the size of the discarded
        gains — a fabricated return that then propagates into max_drawdown,
        ann_vol and cvar_95. A reset is already a destructive wipe; that
        history goes too.

        Only *that book's* marks, though. The delete used to be unqualified,
        so resetting the simulated paper book also destroyed the realized
        equity curve of every other book — including an Alpaca account's
        backfilled history, which no reset here can rebuild. `positions` and
        `orders` are not book-partitioned because they only ever hold the
        simulated book: the Alpaca adapter reads its positions from Alpaca.
        """
        if not str(book).strip():
            raise ValueError(
                "reset_book requires the book being reset; an unqualified "
                "wipe would take every book's history with it")
        self.con.execute("DELETE FROM positions")
        self.con.execute("DELETE FROM orders")
        self.con.execute("DELETE FROM equity_marks WHERE book=?", [str(book)])
        # Resetting one book must not clear another book's halt or reset its
        # peak — that is the same cross-book leak the partitioning exists for.
        self.con.execute(
            "UPDATE account SET cash=?, high_water_mark=?, halted=FALSE, updated_at=? "
            "WHERE book=?", [cash, cash, _now(), str(book)])

    def update_high_water_mark(self, equity: float,
                               book: str = DEFAULT_BOOK) -> None:
        """Ratchet one book's high-water mark. GREATEST never lowers it.

        Scoped to the book because the mark is what drawdown is measured
        against: a mark set by a different venue is not a peak this book ever
        reached, and the difference reads as a loss it never took.
        """
        self.con.execute(
            "UPDATE account SET high_water_mark = GREATEST(high_water_mark, ?), "
            "updated_at=? WHERE book=?", [equity, _now(), str(book)])

    # -- order plans (state machine) ---------------------------------------
    def create_plan(self, plan_id: str, decision_id: str, targets: dict,
                    pre_trade: dict, legs: list | None = None) -> None:
        self.con.execute(
            "INSERT INTO plans (plan_id, decision_id, state, targets, "
            "pre_trade, created_at, legs) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT DO NOTHING",
            [plan_id, decision_id, "proposed", _j(targets), _j(pre_trade),
             _now(), _j(legs or [])])

    def set_plan_state(self, plan_id: str, state: str) -> None:
        self.con.execute("UPDATE plans SET state=? WHERE plan_id=?", [state, plan_id])

    def get_plan(self, plan_id: str) -> dict | None:
        r = self._rows("SELECT * FROM plans WHERE plan_id=?", [plan_id])
        return r[0] if r else None

    def list_plans(self, limit: int = 20) -> list[dict]:
        """Return the newest structured rebalance proposals."""
        return self._rows(
            "SELECT * FROM plans ORDER BY created_at DESC LIMIT ?", [limit])

    def add_order(self, client_order_id: str, plan_id: str, ticker: str,
                  side: str, notional: float, state: str = "submitted") -> None:
        self.con.execute(
            "INSERT INTO orders (client_order_id, plan_id, ticker, side, "
            "notional, state, created_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT DO NOTHING",
            [client_order_id, plan_id, ticker, side, notional, state, _now()])

    def apply_order_transition(self, client_order_id: str, state: str, *,
                               filled_qty: float | None = None,
                               avg_fill_price: float | None = None,
                               fee: float | None = None) -> None:
        """Record a broker-truth order transition with fill accounting.

        The owner applies transitions produced by the trade-update supervisor;
        this is the single writer. Only supplied fields are updated, so an
        acknowledgement (no fill data) advances state without clobbering fills.
        """
        sets = ["state=?"]
        params: list = [state]
        if filled_qty is not None:
            sets.append("filled_qty=?")
            params.append(float(filled_qty))
        if avg_fill_price is not None:
            sets.append("avg_fill_price=?")
            params.append(float(avg_fill_price))
        if fee is not None:
            sets.append("fee=?")
            params.append(float(fee))
        params.append(client_order_id)
        self.con.execute(
            f"UPDATE orders SET {', '.join(sets)} WHERE client_order_id=?", params)

    def get_order(self, client_order_id: str) -> dict | None:
        """Return one execution leg by its stable idempotency key."""
        rows = self._rows(
            "SELECT * FROM orders WHERE client_order_id=?",
            [client_order_id],
        )
        return rows[0] if rows else None

    def update_order_state(self, client_order_id: str, state: str) -> None:
        """Advance the locally recorded state of an existing execution leg."""
        self.con.execute(
            "UPDATE orders SET state=? WHERE client_order_id=?",
            [state, client_order_id],
        )

    def list_orders(self, limit: int = 50) -> list[dict]:
        """Return recent order records for the audit surface."""
        return self._rows(
            "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", [limit])

    def log_equity_mark(self, ts: str, equity: float, cash: float | None,
                        source: str, book: str = "") -> bool:
        """One equity observation per (ts, source); duplicates are no-ops.

        ``book`` names the broker the equity belongs to. An empty book is an
        unattributed observation: it is stored, but no book will ever claim it
        as part of its return series.
        """
        row = self.con.execute(
            "INSERT OR IGNORE INTO equity_marks (ts, source, book, equity, cash) "
            "VALUES (?, ?, ?, ?, ?)",
            [str(ts), str(source), str(book), float(equity),
             None if cash is None else float(cash)],
        ).fetchone()
        return bool(row and row[0])

    def equity_marks(self, limit: int = 5000,
                     book: str | None = None) -> list[dict]:
        """Newest ``limit`` marks oldest-first; one book only when given."""
        where = "" if book is None else "WHERE book = ? "
        params: list = [] if book is None else [str(book)]
        rows = self._rows(
            f"SELECT ts, source, book, equity, cash FROM equity_marks {where}"
            "ORDER BY ts DESC LIMIT ?", [*params, int(limit)])
        return list(reversed(rows))

    def count_equity_marks(self, book: str | None = None) -> int:
        """Total marks recorded — the honest denominator behind a capped read."""
        where = "" if book is None else "WHERE book = ?"
        params: list = [] if book is None else [str(book)]
        rows = self._rows(
            f"SELECT COUNT(*) AS n FROM equity_marks {where}", params)
        return int(rows[0]["n"]) if rows else 0

    def count_orders_on(self, day_iso: str) -> int:
        """Orders created on a given UTC date (for the cumulative daily cap)."""
        rows = self._rows(
            "SELECT COUNT(*) AS n FROM orders WHERE substr(created_at,1,10)=?",
            [day_iso[:10]])
        return int(rows[0]["n"]) if rows else 0

    # -- referee verdicts -----------------------------------------------------
    def log_verdict(self, decision_id: str, verdict: str, reasons: list[str],
                    source: str = "deterministic", targets: dict | None = None) -> str:
        vid = uuid.uuid4().hex[:16]
        th = targets_hash(targets) if targets else ""
        self.con.execute(
            "INSERT INTO verdicts (verdict_id, decision_id, verdict, reasons, "
            "source, created_at, targets_hash, seq) "
            "VALUES (?,?,?,?,?,?,?, nextval('verdict_seq'))",
            [vid, decision_id, verdict, _j(reasons), source, _now(), th])
        self.record_event("referee_verdict",
                          {"decision_id": decision_id, "verdict": verdict})
        return vid

    def get_verdict(self, decision_id: str) -> dict | None:
        # ORDER BY seq (not created_at): timestamps can collide within the
        # same tight loop, but the sequence is monotonic and gives us a
        # reliable "latest" regardless.
        r = self._rows("SELECT * FROM verdicts WHERE decision_id=? "
                       "ORDER BY seq DESC LIMIT 1", [decision_id])
        return r[0] if r else None

    def verdicts_for(self, decision_ids: list[str]) -> dict[str, dict]:
        """Latest verdict per decision id, for surfacing on the audit trail.

        Returns ``{decision_id: {"verdict", "source", "reasons"}}`` for every
        id that has a verdict; decisions with none are simply absent. Latest is
        the highest ``seq`` (monotonic — timestamps can collide in a tight
        referee loop). Empty input short-circuits without touching the DB.
        """
        if not decision_ids:
            return {}
        placeholders = ",".join("?" for _ in decision_ids)
        rows = self._rows(
            f"SELECT * FROM verdicts WHERE decision_id IN ({placeholders}) "
            f"ORDER BY seq DESC",
            list(decision_ids),
        )
        out: dict[str, dict] = {}
        for row in rows:
            did = row["decision_id"]
            if did not in out:  # first row per id = highest seq = latest
                out[did] = {
                    "verdict": row["verdict"],
                    "source": row["source"],
                    "reasons": row["reasons"],
                }
        return out

    # -- agent workforce ---------------------------------------------------
    def start_workflow(
        self,
        kind: str,
        request: dict,
        phases: tuple[str, ...] = WORKFORCE_PHASES,
    ) -> dict:
        """Create one durable, phase-ordered Claude workforce run.

        ``kind="panel"`` builds a tournament from ``request["variants"]`` —
        a list of analyst stances (window/shrinkage/regime dicts) — with its
        instance-specific dependency DAG persisted in the request under
        ``_deps`` so resumption re-reads the same graph.
        """
        request = dict(request)
        # "_deps" is a registry-owned key: only the panel builder writes it.
        # A caller-supplied value could reorder or orphan the gate phases.
        request.pop("_deps", None)
        deps: dict[str, tuple[str, ...]] | None = None
        if kind == "panel":
            variants = request.get("variants")
            if not isinstance(variants, list) or not all(
                    isinstance(v, dict) for v in variants):
                raise ValueError("panel requires request['variants']: list[dict]")
            phases, deps = panel_phases(len(variants))
            request["_deps"] = {phase: list(d) for phase, d in deps.items()}
        # Dependency closure is checked here, not just phase names: a graph that
        # omits a dependency would be created happily and then deadlock.
        validate_phase_graph(phases, deps)

        workflow_id = uuid.uuid4().hex[:16]
        now = _now()
        with self.transaction():
            self.con.execute(
                "INSERT INTO workflows VALUES (?,?,?,?,?,?,?,?)",
                [workflow_id, kind, "running", phases[0], _j(request), _j({}), now, now],
            )
            for seq, phase in enumerate(phases):
                self.con.execute(
                    "INSERT INTO workflow_steps VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [f"{workflow_id}:{phase}", workflow_id, seq, phase,
                     _agent_for_phase(phase), "queued", "", _j({}), None, None, now],
                )
        self.record_event("workflow_started", {
            "workflow_id": workflow_id, "kind": kind, "phases": list(phases),
        })
        return self.get_workflow(workflow_id) or {}

    def interrupt_workflow(
        self,
        workflow_id: str,
        reason: str = "coordinator stopped before the workflow completed",
    ) -> dict:
        """Freeze a live workflow in a resumable, visibly non-running state.

        Interruption is an owner control, not an agent phase update. Every
        currently-working branch is frozen together so a panel cannot leave one
        worker animated after its shared coordinator has gone away.
        """
        reason = reason.strip()
        if not reason:
            raise ValueError("workflow interruption requires a reason")
        if len(reason) > 1000:
            raise ValueError("workflow interruption reason exceeds 1000 characters")
        workflow = self.get_workflow(workflow_id)
        if not workflow:
            raise KeyError(f"unknown workflow_id {workflow_id!r}")
        status = str(workflow.get("status") or "")
        if status != "running":
            # Stop is deliberately idempotent across process/result races. A
            # completed, abandoned, or already-paused run must not be rewritten.
            return workflow

        steps = list(workflow.get("steps") or [])
        active = [step for step in steps if step.get("status") == "working"]
        if not active:
            current = str(workflow.get("current_phase") or "")
            active = [
                step for step in steps
                if step.get("phase") == current and step.get("status") != "done"
            ]
        if not active:
            active = [step for step in steps if step.get("status") != "done"][:1]

        now = _now()
        changed: list[str] = []
        with self.transaction():
            for step in active:
                summary = _control_summary(
                    str(step.get("summary") or ""), "Interrupted", reason)
                self.con.execute(
                    "UPDATE workflow_steps SET status='interrupted', summary=?, "
                    "completed_at=?, updated_at=? "
                    "WHERE step_id=? AND status <> 'done'",
                    [summary, now, now, step["step_id"]],
                )
                changed.append(str(step["phase"]))
            current_phase = changed[0] if changed else str(
                workflow.get("current_phase") or "")
            self.con.execute(
                "UPDATE workflows SET status='interrupted', current_phase=?, "
                "updated_at=? WHERE workflow_id=? AND status='running'",
                [current_phase, now, workflow_id],
            )
        self.record_event("workflow_interrupted", {
            "workflow_id": workflow_id,
            "phases": changed,
            "reason": reason,
        })
        return self.get_workflow(workflow_id) or {}

    def resume_workflow(self, workflow_id: str) -> dict:
        """Explicitly reopen one interrupted/failed/blocked workflow.

        Agents cannot call this transition. Requiring an owner-side resume
        fences a surviving orphan process: phase writes alone cannot silently
        turn an interrupted workflow back into a live one.
        """
        workflow = self.get_workflow(workflow_id)
        if not workflow:
            raise KeyError(f"unknown workflow_id {workflow_id!r}")
        status = str(workflow.get("status") or "")
        if status == "running":
            raise RuntimeError(
                f"workflow {workflow_id!r} is already running")
        if status in WORKFLOW_TERMINAL_STATUSES:
            raise RuntimeError(
                f"{status} workflow {workflow_id!r} cannot be resumed")
        if status not in WORKFLOW_RESUMABLE_STATUSES:
            raise RuntimeError(
                f"workflow {workflow_id!r} in state {status!r} cannot be resumed")

        open_steps = [
            step for step in workflow.get("steps", [])
            if step.get("status") != "done"
        ]
        if not open_steps:
            raise RuntimeError(
                f"workflow {workflow_id!r} has no incomplete phase to resume")
        now = _now()
        current_phase = str(open_steps[0]["phase"])
        self.con.execute(
            "UPDATE workflows SET status='running', current_phase=?, result=?, "
            "updated_at=? WHERE workflow_id=?",
            [current_phase, _j({}), now, workflow_id],
        )
        self.record_event("workflow_resumed", {
            "workflow_id": workflow_id,
            "phase": current_phase,
        })
        return self.get_workflow(workflow_id) or {}

    def abandon_workflow(
        self,
        workflow_id: str,
        reason: str = "operator abandoned the incomplete workflow",
    ) -> dict:
        """Permanently close an incomplete workflow while retaining its audit."""
        reason = reason.strip()
        if not reason:
            raise ValueError("workflow abandonment requires a reason")
        if len(reason) > 1000:
            raise ValueError("workflow abandonment reason exceeds 1000 characters")
        workflow = self.get_workflow(workflow_id)
        if not workflow:
            raise KeyError(f"unknown workflow_id {workflow_id!r}")
        status = str(workflow.get("status") or "")
        if status == "abandoned":
            return workflow
        if status == "complete":
            raise RuntimeError(
                f"completed workflow {workflow_id!r} cannot be abandoned")

        now = _now()
        changed: list[str] = []
        with self.transaction():
            for step in workflow.get("steps", []):
                if step.get("status") == "done":
                    continue
                prefix = (
                    "Not run; workflow abandoned"
                    if step.get("status") == "queued"
                    else "Abandoned"
                )
                summary = _control_summary(
                    str(step.get("summary") or ""), prefix, reason)
                self.con.execute(
                    "UPDATE workflow_steps SET status='abandoned', summary=?, "
                    "completed_at=?, updated_at=? "
                    "WHERE step_id=? AND status <> 'done'",
                    [summary, now, now, step["step_id"]],
                )
                changed.append(str(step["phase"]))
            result = {
                "final_summary": f"Workflow abandoned: {reason}",
                "control": {
                    "status": "abandoned",
                    "reason": reason,
                    "at": now,
                },
            }
            self.con.execute(
                "UPDATE workflows SET status='abandoned', result=?, updated_at=? "
                "WHERE workflow_id=? AND status <> 'complete'",
                [_j(result), now, workflow_id],
            )
        self.record_event("workflow_abandoned", {
            "workflow_id": workflow_id,
            "phases": changed,
            "reason": reason,
        })
        return self.get_workflow(workflow_id) or {}

    def interrupt_running_workflows(
        self,
        reason: str,
        *,
        updated_before: str | None = None,
    ) -> list[dict]:
        """Interrupt every matching orphan candidate and return changed rows."""
        if updated_before is None:
            rows = self._rows(
                "SELECT workflow_id FROM workflows WHERE status='running' "
                "ORDER BY created_at",
                [],
            )
        else:
            rows = self._rows(
                "SELECT workflow_id FROM workflows WHERE status='running' "
                "AND updated_at < ? ORDER BY created_at",
                [updated_before],
            )
        return [
            self.interrupt_workflow(str(row["workflow_id"]), reason)
            for row in rows
        ]

    def update_workflow_phase(
        self,
        workflow_id: str,
        phase: str,
        status: str,
        summary: str = "",
        artifacts: dict | None = None,
    ) -> dict:
        """Advance one role-bound phase while enforcing the dependency DAG."""
        phase_type = _phase_type(phase)
        if phase_type not in _WORKFORCE_REQUIRED_ARTIFACTS:
            raise ValueError(f"unknown workforce phase {phase!r}")
        if status not in {"working", "done", "failed", "blocked"}:
            raise ValueError("status must be working, done, failed, or blocked")
        workflow = self.get_workflow(workflow_id)
        if not workflow:
            raise KeyError(f"unknown workflow_id {workflow_id!r}")
        steps = workflow["steps"]
        by_phase = {step["phase"]: step for step in steps}
        if phase not in by_phase:
            raise ValueError(f"phase {phase!r} is not part of workflow {workflow_id}")
        step = by_phase[phase]
        if step["status"] == "done":
            if status == "done":
                # Idempotent replay (e.g. a resumed coordinator retrying its
                # last call) must not revert a complete workflow or wipe its
                # persisted result.
                return workflow
            raise RuntimeError(f"completed phase {phase!r} cannot be reopened")
        workflow_status = str(workflow.get("status") or "")
        if workflow_status != "running":
            raise RuntimeError(
                f"workflow {workflow_id!r} is {workflow_status!r}; "
                "resume it explicitly before updating a phase"
            )
        instance_deps = (workflow.get("request") or {}).get("_deps")
        # Honor a stored DAG only when it covers exactly this workflow's
        # phases; anything else falls back to the static map so a malformed
        # request can never orphan the judge/referee gates.
        deps_map = (
            instance_deps
            if isinstance(instance_deps, dict)
            and set(instance_deps) == set(by_phase)
            else _WORKFORCE_DEPS
        )
        # An open material disagreement blocks the reporter *as a dependency*:
        # the desk does not report while the analyst and challenger are still
        # arguing. Checked with the phase DAG so it cannot be ordered around.
        if _phase_type(phase) == "reporter":
            unresolved = self._rows(
                "SELECT debate_id FROM debates "
                "WHERE workflow_id = ? AND status = 'open'", [workflow_id])
            if unresolved:
                raise RuntimeError(
                    "reporter cannot start with unadjudicated debates: "
                    f"{[row['debate_id'] for row in unresolved]}")
        for dependency in deps_map.get(phase, ()):
            dependency_step = by_phase.get(dependency)
            if dependency_step is None or dependency_step["status"] != "done":
                raise RuntimeError(
                    f"phase {phase!r} cannot start before {dependency!r} is done"
                )

        artifacts = artifacts or {}
        if len(summary) > 4000:
            raise ValueError("workflow phase summary exceeds 4000 characters")
        artifacts_json = _j(artifacts)
        if len(artifacts_json.encode("utf-8")) > 32768:
            raise ValueError("workflow phase artifacts exceed 32 KiB")
        if status == "done":
            missing = [
                key for key in _WORKFORCE_REQUIRED_ARTIFACTS[phase_type]
                if key not in artifacts or artifacts[key] in (None, "", {})
            ]
            if missing:
                raise ValueError(
                    f"phase {phase!r} cannot complete without artifacts {missing}"
                )
            if phase_type == "optimizer" and not isinstance(artifacts["targets"], dict):
                raise ValueError("optimizer artifact 'targets' must be an object")
            if phase_type == "judge":
                self._check_judge_binding(by_phase, artifacts)
            if phase_type == "referee":
                if artifacts["verdict"] != "PASS":
                    raise ValueError(
                        "referee may complete only with PASS; use blocked for FAIL"
                    )
                self._check_referee_binding(by_phase, artifacts)

        now = _now()
        restarting = (
            status == "working"
            and step.get("status") in {
                "interrupted", "failed", "blocked",
            }
        )
        started_at = now if restarting else step.get("started_at") or now
        completed_at = now if status in {"done", "failed", "blocked"} else None
        with self.transaction():
            self.con.execute(
                "UPDATE workflow_steps SET status=?, summary=?, artifacts=?, "
                "started_at=?, completed_at=?, updated_at=? "
                "WHERE step_id=?",
                [status, summary, artifacts_json, started_at, completed_at, now,
                 step["step_id"]],
            )
            if status == "done":
                # Concurrent phases finish out of seq order, so "what is open"
                # is the set of steps still not done — never seq+1, which would
                # report the challenger as current after the optimizer landed
                # first, and could call a run complete with a phase outstanding.
                open_phases = [
                    candidate["phase"] for candidate in steps
                    if candidate["phase"] != phase and candidate["status"] != "done"
                ]
                workflow_status = "complete" if not open_phases else "running"
                current_phase = open_phases[0] if open_phases else phase
            else:
                workflow_status = status if status in {"failed", "blocked"} else "running"
                current_phase = phase
            result = (
                {"final_summary": summary, "artifacts": artifacts}
                if workflow_status == "complete" else {}
            )
            self.con.execute(
                "UPDATE workflows SET status=?, current_phase=?, result=?, updated_at=? "
                "WHERE workflow_id=?",
                [workflow_status, current_phase, _j(result), now, workflow_id],
            )
        self.record_event("workflow_phase", {
            "workflow_id": workflow_id, "phase": phase, "agent": _agent_for_phase(phase),
            "status": status, "summary": summary[:240],
        })
        return self.get_workflow(workflow_id) or {}

    def _check_referee_binding(self, by_phase: dict, artifacts: dict) -> None:
        """Refuse a referee PASS that is not bound to the optimizer's targets.

        The verdict table alone binds a PASS to *some* hash; without this
        check a referee could log a PASS for an in-mandate vector the
        optimizer never produced and the reporter would preview it. The
        workflow is the only place both sides are persisted, so the equality
        is enforced here, not in prompts.
        """
        referee_targets = artifacts.get("targets")
        if not isinstance(referee_targets, dict) or not referee_targets:
            raise ValueError(
                "referee completion requires the reviewed 'targets' object"
            )
        reviewed_hash = targets_hash(referee_targets)
        # In a panel, the judge's evidence-chosen winner is what the referee
        # reviews; in the standard pipeline it is the single optimizer.
        judge_step = by_phase.get("judge")
        if judge_step is not None:
            winning = (judge_step.get("artifacts") or {}).get("winning_targets")
            if (
                not isinstance(winning, dict)
                or targets_hash(winning) != reviewed_hash
            ):
                raise ValueError(
                    "referee 'targets' do not match the judge's winning targets"
                )
            # The reviewed decision is the winning branch's own analyst
            # decision. A panel winner is "optimizer-<branch>"; a flat graph's
            # is bare "optimizer", and rpartition on that yields "optimizer"
            # itself — so this used to look up "analyst-optimizer", find
            # nothing, and drop the expected decision entirely, skipping the
            # check that a PASS must review THIS run's decision.
            winner = str((judge_step.get("artifacts") or {}).get("winner_phase", ""))
            _, dash, branch = winner.rpartition("-")
            analyst_phase = f"analyst-{branch}" if dash else "analyst"
            if analyst_phase not in by_phase:
                raise ValueError(
                    f"judge crowned {winner!r}, whose analyst phase "
                    f"{analyst_phase!r} is not in this workflow; the verdict "
                    "cannot be bound to a decision")
            expected_decision = self._phase_decision_id(by_phase, analyst_phase)
            return self._require_pass_verdict(
                artifacts, reviewed_hash, expected_decision)
        optimizer_step = by_phase.get("optimizer")
        if optimizer_step is not None:
            optimizer_targets = (optimizer_step.get("artifacts") or {}).get("targets")
            if (
                not isinstance(optimizer_targets, dict)
                or targets_hash(optimizer_targets) != reviewed_hash
            ):
                raise ValueError(
                    "referee 'targets' do not match the optimizer's persisted targets"
                )
        expected_decision = self._phase_decision_id(by_phase, "analyst")
        self._require_pass_verdict(artifacts, reviewed_hash, expected_decision)

    @staticmethod
    def _phase_decision_id(by_phase: dict, phase: str) -> str | None:
        step = by_phase.get(phase)
        if step is None:
            return None
        decision_id = (step.get("artifacts") or {}).get("decision_id")
        return str(decision_id) if decision_id else None

    def _require_pass_verdict(self, artifacts: dict, reviewed_hash: str,
                             expected_decision: str | None = None) -> None:
        verdict_rows = self._rows(
            "SELECT * FROM verdicts WHERE verdict_id=?",
            [str(artifacts.get("verdict_id"))],
        )
        if (
            not verdict_rows
            or verdict_rows[0]["verdict"] != "PASS"
            or verdict_rows[0]["targets_hash"] != reviewed_hash
        ):
            raise ValueError(
                "verdict_id must reference a persisted PASS bound to these targets"
            )
        # The PASS must review THIS workflow's decision, not merely some
        # decision that happens to share the targets hash.
        if (expected_decision is not None
                and str(verdict_rows[0]["decision_id"]) != expected_decision):
            raise ValueError(
                "verdict_id belongs to a different decision than the workflow's "
                "analyst decision; re-review this run's decision"
            )

    def _check_judge_binding(self, by_phase: dict, artifacts: dict) -> None:
        """A judge may only crown a winner that a completed branch produced.

        The tournament's honesty rests here: ``winning_targets`` must be the
        verbatim output of the named ``winner_phase`` optimizer, so a judge
        cannot synthesize a "winner" no branch computed.
        """
        winner_phase = str(artifacts.get("winner_phase") or "")
        winner_step = by_phase.get(winner_phase)
        if winner_step is None or _phase_type(winner_phase) != "optimizer":
            raise ValueError(
                f"judge winner_phase {winner_phase!r} must name an optimizer "
                "branch of this workflow"
            )
        if winner_step.get("status") != "done":
            raise ValueError(
                f"judge winner_phase {winner_phase!r} has not completed")
        winning = artifacts.get("winning_targets")
        branch_targets = (winner_step.get("artifacts") or {}).get("targets")
        if (
            not isinstance(winning, dict) or not winning
            or not isinstance(branch_targets, dict)
            or targets_hash(winning) != targets_hash(branch_targets)
        ):
            raise ValueError(
                "judge 'winning_targets' must equal the winning branch's "
                "persisted optimizer targets"
            )

    def get_workflow(self, workflow_id: str) -> dict | None:
        rows = self._rows("SELECT * FROM workflows WHERE workflow_id=?", [workflow_id])
        if not rows:
            return None
        workflow = rows[0]
        workflow["steps"] = self._rows(
            "SELECT * FROM workflow_steps WHERE workflow_id=? ORDER BY seq",
            [workflow_id],
        )
        return workflow

    def list_workflows(self, limit: int = 10) -> list[dict]:
        rows = self._rows(
            "SELECT * FROM workflows ORDER BY created_at DESC LIMIT ?", [limit]
        )
        for workflow in rows:
            workflow["steps"] = self._rows(
                "SELECT * FROM workflow_steps WHERE workflow_id=? ORDER BY seq",
                [workflow["workflow_id"]],
            )
        return rows

    # -- events -------------------------------------------------------------
    def record_event(self, kind: str, payload: dict) -> str:
        eid = uuid.uuid4().hex[:16]
        self.con.execute("INSERT INTO events VALUES (?,?,?,?)",
                         [eid, _now(), kind, _j(payload)])
        return eid

    def record_data_permit(self, permit: dict) -> str:
        """Persist a data permit and return its id.

        The permit is content-addressed, so re-recording the same permit is
        idempotent (INSERT OR REPLACE on the deterministic ``permit_id``).
        """
        permit_id = str(permit["permit_id"])
        self.con.execute(
            "INSERT OR REPLACE INTO data_permits VALUES (?,?,?,?,?,?,?,?,?)",
            [permit_id, permit.get("snapshot_id"), permit.get("purpose"),
             permit.get("provider"), permit.get("feed"), permit.get("as_of"),
             _j(permit), bool(permit.get("eligible_for_execution", False)),
             _now()],
        )
        return permit_id

    def get_data_permit(self, permit_id: str) -> dict | None:
        rows = self._rows(
            "SELECT * FROM data_permits WHERE permit_id = ?", [permit_id])
        return rows[0] if rows else None

    def current_data_permit(self, purpose: str) -> dict | None:
        """The most recently recorded permit for ``purpose`` (or None)."""
        rows = self._rows(
            "SELECT * FROM data_permits WHERE purpose = ? "
            "ORDER BY created_at DESC LIMIT 1",
            [purpose],
        )
        return rows[0] if rows else None

    # -- Atlas supervisor state ---------------------------------------
    def get_atlas_state(self, manager_id: str = "atlas") -> dict | None:
        rows = self._rows(
            "SELECT * FROM atlas_state WHERE manager_id = ?", [manager_id])
        return rows[0] if rows else None

    def save_atlas_state(self, state: dict, manager_id: str = "atlas") -> None:
        """Upsert Atlas's single logical current-state record."""
        self.con.execute(
            "INSERT OR REPLACE INTO atlas_state VALUES (?,?,?,?,?,?,?,?,?)",
            [manager_id, state.get("mode"), state.get("state"),
             state.get("current_task_id"), state.get("last_wake_reason"),
             state.get("last_brief_at"), state.get("blocked_reason"),
             state.get("coordinator_session_id"), _now()])

    def create_atlas_task(self, task_id: str, dedupe_key: str, trigger_kind: str,
                        trigger_payload: dict, template_id: str | None) -> bool:
        """Create a queued task, deduped by its UNIQUE key.

        Returns True if a new task was created, False if the dedupe key already
        exists (the trigger was already handled for this state) — the caller
        must not re-run a deduplicated task.
        """
        existing = self._rows(
            "SELECT task_id FROM atlas_tasks WHERE dedupe_key = ?", [dedupe_key])
        if existing:
            return False
        self.con.execute(
            "INSERT INTO atlas_tasks (task_id, dedupe_key, trigger_kind, "
            "trigger_payload, template_id, status, workflow_id, conclusion, "
            "error, attempt_count, created_at, started_at, completed_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [task_id, dedupe_key, trigger_kind, _j(trigger_payload), template_id,
             "queued", None, None, None, 0, _now(), None, None, _now()])
        return True

    def update_atlas_task(self, task_id: str, *, status: str | None = None,
                        workflow_id: str | None = None,
                        conclusion: dict | None = None,
                        error: str | None = None,
                        bump_attempt: bool = False) -> None:
        sets, params = ["updated_at=?"], [_now()]
        if status is not None:
            sets.append("status=?")
            params.append(status)
            if status == "running":
                sets.append("started_at=?")
                params.append(_now())
            if status in ("completed", "failed", "expired", "invalidated", "canceled"):
                sets.append("completed_at=?")
                params.append(_now())
        if workflow_id is not None:
            sets.append("workflow_id=?")
            params.append(workflow_id)
        if conclusion is not None:
            sets.append("conclusion=?")
            params.append(_j(conclusion))
        if error is not None:
            sets.append("error=?")
            params.append(error)
        if bump_attempt:
            sets.append("attempt_count=attempt_count+1")
        params.append(task_id)
        self.con.execute(
            f"UPDATE atlas_tasks SET {', '.join(sets)} WHERE task_id=?", params)

    def get_atlas_task(self, task_id: str) -> dict | None:
        rows = self._rows("SELECT * FROM atlas_tasks WHERE task_id = ?", [task_id])
        return rows[0] if rows else None

    def list_atlas_tasks(self, limit: int = 50) -> list[dict]:
        return self._rows(
            "SELECT * FROM atlas_tasks ORDER BY created_at DESC LIMIT ?", [limit])

    def count_atlas_tasks_on(self, day_iso: str, trigger_kind: str | None = None) -> int:
        """Autonomous tasks created on a UTC date (for the daily budget)."""
        if trigger_kind is not None:
            rows = self._rows(
                "SELECT COUNT(*) AS n FROM atlas_tasks WHERE substr(created_at,1,10)=? "
                "AND trigger_kind=?", [day_iso[:10], trigger_kind])
        else:
            rows = self._rows(
                "SELECT COUNT(*) AS n FROM atlas_tasks WHERE substr(created_at,1,10)=?",
                [day_iso[:10]])
        return int(rows[0]["n"]) if rows else 0

    # -- standing authority grants (inert unless a human creates one) -------
    def create_authority_grant(self, grant: dict) -> str:
        gid = str(grant["grant_id"])
        self.con.execute(
            "INSERT INTO authority_grants VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [gid, grant.get("mode"), _j(grant.get("allowed_universe") or []),
             grant.get("max_notional"), grant.get("max_turnover"),
             grant.get("max_orders"), grant.get("allowed_policy"),
             grant.get("valid_from"), grant.get("expires_at"), None, None,
             grant.get("granted_by"), _now()])
        self.record_event("authority.granted",
                          {"grant_id": gid, "mode": grant.get("mode"),
                           "expires_at": grant.get("expires_at")})
        return gid

    def get_authority_grant(self, grant_id: str) -> dict | None:
        rows = self._rows(
            "SELECT * FROM authority_grants WHERE grant_id = ?", [grant_id])
        return rows[0] if rows else None

    def list_authority_grants(self, limit: int = 50) -> list[dict]:
        return self._rows(
            "SELECT * FROM authority_grants ORDER BY created_at DESC LIMIT ?",
            [limit])

    def revoke_authority_grant(self, grant_id: str, reason: str,
                               now_iso: str | None = None) -> None:
        """Revoke immediately. Revocation is always available and never expires."""
        self.con.execute(
            "UPDATE authority_grants SET revoked_at = ?, revoked_reason = ? "
            "WHERE grant_id = ? AND revoked_at IS NULL",
            [now_iso or _now(), reason, grant_id])
        self.record_event("authority.revoked",
                          {"grant_id": grant_id, "reason": reason})

    # -- bounded debate (registry-enforced, not prompt policy) --------------
    def create_debate(self, debate: dict) -> str:
        did = str(debate["debate_id"])
        self.con.execute(
            "INSERT INTO debates VALUES (?,?,?,?,?,?,?,?,?,?)",
            [did, debate.get("workflow_id"), debate.get("original_decision_id"),
             debate.get("status", "open"), int(debate.get("max_rounds", 2)),
             debate.get("panel_snapshot_id"),
             _j(debate.get("material_claims") or []), None, _now(), _now()])
        return did

    def get_debate(self, debate_id: str) -> dict | None:
        rows = self._rows("SELECT * FROM debates WHERE debate_id = ?", [debate_id])
        return rows[0] if rows else None

    def list_debates(self, workflow_id: str | None = None) -> list[dict]:
        if workflow_id is not None:
            return self._rows(
                "SELECT * FROM debates WHERE workflow_id = ? "
                "ORDER BY created_at DESC", [workflow_id])
        # `_rows` takes params positionally; omitting them raised a TypeError,
        # so the unfiltered listing had never once run.
        return self._rows("SELECT * FROM debates ORDER BY created_at DESC", [])

    def add_debate_turn(self, turn: dict) -> str:
        tid = str(turn["turn_id"])
        self.con.execute(
            "INSERT INTO debate_turns VALUES (?,?,?,?,?,?,?,?,?)",
            [tid, turn.get("debate_id"), int(turn.get("round", 1)),
             turn.get("role"), turn.get("claim_id"), turn.get("position"),
             turn.get("argument"), _j(turn.get("evidence_refs") or []), _now()])
        self.con.execute(
            "UPDATE debates SET updated_at = ? WHERE debate_id = ?",
            [_now(), turn.get("debate_id")])
        return tid

    def list_debate_turns(self, debate_id: str) -> list[dict]:
        return self._rows(
            "SELECT * FROM debate_turns WHERE debate_id = ? "
            "ORDER BY created_at ASC, turn_id ASC", [debate_id])

    def close_debate(self, debate_id: str, status: str,
                     adjudication: dict) -> None:
        self.con.execute(
            "UPDATE debates SET status = ?, adjudication = ?, updated_at = ? "
            "WHERE debate_id = ?",
            [status, _j(adjudication), _now(), debate_id])

    # -- reflection lessons (advisory language over an immutable outcome) ---
    def record_lesson(self, lesson: dict) -> str:
        """Persist a grounded lesson bound to an outcome hash."""
        lid = str(lesson["lesson_id"])
        self.con.execute(
            "INSERT OR REPLACE INTO reflection_lessons VALUES (?,?,?,?,?,?,?,?)",
            [lid, lesson.get("decision_id"), lesson.get("outcome_hash"),
             _j(lesson), lesson.get("prompt_version"),
             lesson.get("model_record_id"), False, _now()])
        self.record_event("reflection.lesson_generated",
                          {"lesson_id": lid,
                           "decision_id": lesson.get("decision_id")})
        return lid

    def get_lesson(self, decision_id: str) -> dict | None:
        """The most recent lesson for a decision, if any."""
        rows = self._rows(
            "SELECT * FROM reflection_lessons WHERE decision_id = ? "
            "ORDER BY created_at DESC LIMIT 1", [decision_id])
        return rows[0] if rows else None

    def mark_lessons_stale(self, decision_id: str, current_outcome_hash: str) -> int:
        """Mark any lesson written against a superseded outcome as stale.

        Returns how many were marked. Called after an outcome correction so a
        lesson can never be read as describing the current outcome.
        """
        rows = self._rows(
            "SELECT lesson_id FROM reflection_lessons "
            "WHERE decision_id = ? AND outcome_hash <> ? AND stale = FALSE",
            [decision_id, current_outcome_hash])
        for row in rows:
            self.con.execute(
                "UPDATE reflection_lessons SET stale = TRUE WHERE lesson_id = ?",
                [row["lesson_id"]])
            self.record_event("reflection.lesson_stale",
                              {"lesson_id": row["lesson_id"],
                               "decision_id": decision_id})
        return len(rows)

    # -- model invocation audit ---------------------------------------------
    def record_model_invocation(self, record: dict) -> str:
        """Persist which tier was requested and which model actually served it."""
        iid = str(record["invocation_id"])
        # Named columns, not positional. The repo's ALTER TABLE ADD COLUMN
        # idiom (above) appends columns to existing databases, and a positional
        # VALUES list would then write every field one column to the left with
        # no error at all.
        self.con.execute(
            "INSERT OR REPLACE INTO model_invocations (invocation_id, role, "
            "requested_tier, resolved_model, backend, status, latency_ms, "
            "tokens, fallback_reason, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [iid, record.get("role"), record.get("requested_tier"),
             record.get("resolved_model"), record.get("backend"),
             record.get("status"), record.get("latency_ms"),
             record.get("tokens"), record.get("fallback_reason"), _now()])
        return iid

    def list_model_invocations(self, limit: int = 50) -> list[dict]:
        return self._rows(
            "SELECT * FROM model_invocations ORDER BY created_at DESC LIMIT ?",
            [limit])

    # -- approval requests (the human execution gate) -----------------------
    def create_approval_request(self, approval: dict) -> str:
        """Persist a pending approval request bound to an exact plan."""
        aid = str(approval["approval_id"])
        self.con.execute(
            "INSERT INTO approval_requests (approval_id, task_id, plan_id, "
            "plan_digest, decision_id, targets_hash, data_permit_id, broker, "
            "book_revision, expected_cost, summary, status, challenge_digest, "
            "expires_at, decided_at, consumed_at, invalidated_reason, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [aid, approval.get("task_id"), approval.get("plan_id"),
             approval.get("plan_digest"), approval.get("decision_id"),
             approval.get("targets_hash"), approval.get("data_permit_id"),
             approval.get("broker"), approval.get("book_revision"),
             _j(approval.get("expected_cost")), _j(approval.get("summary")),
             "pending", None, approval.get("expires_at"), None, None, None,
             _now()])
        return aid

    def get_approval_request(self, approval_id: str) -> dict | None:
        rows = self._rows(
            "SELECT * FROM approval_requests WHERE approval_id = ?", [approval_id])
        return rows[0] if rows else None

    def list_approval_requests(self, limit: int = 50,
                               status: str | None = None) -> list[dict]:
        if status is not None:
            return self._rows(
                "SELECT * FROM approval_requests WHERE status = ? "
                "ORDER BY created_at DESC LIMIT ?", [status, limit])
        return self._rows(
            "SELECT * FROM approval_requests ORDER BY created_at DESC LIMIT ?",
            [limit])

    def transition_approval(self, approval_id: str, status: str, *,
                            challenge_digest: str | None = None,
                            decided_at: str | None = None,
                            consumed_at: str | None = None,
                            invalidated_reason: str | None = None) -> None:
        """Advance one approval along a legal edge, or refuse.

        This used to be an unguarded UPDATE, which gave the approval lifecycle
        writes but no edges: a consumed, rejected, or expired approval could be
        driven back to ``pending`` by the challenge route and then approved
        again, so neither a rejection nor an expiry was durable and a spent
        approval could authorise a second fill. An unknown id updated nothing
        and reported success.
        """
        legal = _APPROVAL_TRANSITIONS.get(status)
        if legal is None:
            raise ValueError(f"unknown approval status {status!r}")
        current = self.get_approval_request(approval_id)
        if current is None:
            raise KeyError(f"unknown approval_id {approval_id!r}")
        was = str(current.get("status") or "")
        if was not in legal:
            raise PermissionError(
                f"approval {approval_id!r} cannot move {was!r} -> {status!r}; "
                f"only {sorted(legal)} may become {status!r}")
        sets, params = ["status=?"], [status]
        if challenge_digest is not None:
            sets.append("challenge_digest=?")
            params.append(challenge_digest)
        if decided_at is not None:
            sets.append("decided_at=?")
            params.append(decided_at)
        if consumed_at is not None:
            sets.append("consumed_at=?")
            params.append(consumed_at)
        if invalidated_reason is not None:
            sets.append("invalidated_reason=?")
            params.append(invalidated_reason)
        params.append(approval_id)
        self.con.execute(
            f"UPDATE approval_requests SET {', '.join(sets)} "
            "WHERE approval_id=?", params)

    def expire_due_approvals(self, now_iso: str) -> list[str]:
        """Mark pending approvals whose expiry has passed as expired.

        Returns the ids expired. Deterministic given ``now_iso`` so the owner's
        expiry sweep is auditable.
        """
        due = self._rows(
            "SELECT approval_id FROM approval_requests "
            "WHERE status = 'pending' AND expires_at IS NOT NULL "
            "AND expires_at <= ?", [now_iso])
        ids = [r["approval_id"] for r in due]
        for aid in ids:
            self.transition_approval(aid, "expired", decided_at=now_iso)
        return ids

    # -- news archive ------------------------------------------------------

    def record_news_items(self, batch) -> dict:
        """Persist one archive batch. Runs under the owner's dispatch lock.

        One multi-row INSERT rather than executemany: measured at 5.1ms for 100
        rows against a 200k-row table versus 74-218ms row-by-row, and this is on
        the lock every TUI poll waits behind.

        ``seen_count`` counts archive passes that returned the item, and means
        only that. A provider replaying identical text produces the identical
        hash and altered text produces a new row, so the count cannot tell the
        two apart and must not be read as a republication detector.
        """
        rows = list(getattr(batch, "rows", ()) or ())
        edges = list(getattr(batch, "ticker_edges", ()) or ())
        if not rows:
            return {"inserted": 0, "updated": 0, "edges": 0,
                    "total_rows": self._news_row_count()}
        before = self._news_row_count()
        placeholders = ",".join(["(?,?,?,?,?,?,?,?,?,?,?,?)"] * len(rows))
        params: list = []
        for row in rows:
            params.extend([
                row.item_hash, row.published, row.first_seen, row.last_seen,
                row.seen_count, row.provider, row.source, row.source_tier,
                row.headline, row.body_text, row.url, row.synthetic])
        self.con.execute(
            "INSERT INTO news_items (item_hash, published, first_seen, "
            "last_seen, seen_count, provider, source, source_tier, headline, "
            f"body_text, url, synthetic) VALUES {placeholders} "
            "ON CONFLICT (item_hash) DO UPDATE SET "
            "last_seen = excluded.last_seen, "
            "seen_count = news_items.seen_count + 1",
            params)
        if edges:
            edge_ph = ",".join(["(?,?,?)"] * len(edges))
            edge_params: list = []
            for edge in edges:
                edge_params.extend([edge.item_hash, edge.ticker, edge.in_universe])
            self.con.execute(
                "INSERT INTO news_item_tickers (item_hash, ticker, in_universe) "
                f"VALUES {edge_ph} ON CONFLICT DO NOTHING", edge_params)
        after = self._news_row_count()
        inserted = after - before
        return {"inserted": inserted, "updated": len(rows) - inserted,
                "edges": len(edges), "total_rows": after}

    def _news_row_count(self) -> int:
        return int(self.con.execute("SELECT count(*) FROM news_items").fetchone()[0])

    def search_news(self, *, as_of: str, terms=(), tickers=(),
                    knowledge_cutoff: str | None = None,
                    since: str | None = None,
                    include_synthetic: bool = False,
                    limit: int = 25, offset: int = 0) -> list[dict]:
        """Point-in-time search. ``as_of`` is REQUIRED and has no default.

        Defaulting it to now would make every historical query silently a
        present-tense one, which is the failure the whole archive exists to
        avoid. Strict ``published <= as_of`` with no same-day exemption — the
        grounding boundary had one and it leaked twelve hours.

        Synthetic rows are stored but excluded by default: a deterministic
        fixture must never be citable as evidence about a real trade.
        """
        if not as_of:
            raise ValueError(
                "search_news requires an explicit as_of; defaulting it to now "
                "would make every historical query a present-tense one")
        where = ["published <= ?"]
        params: list = [as_of]
        if knowledge_cutoff:
            # When the desk LEARNED it, which differs from when it was
            # published and is the honest bound for "what did we know then".
            where.append("first_seen <= ?")
            params.append(knowledge_cutoff)
        if since:
            where.append("published >= ?")
            params.append(since)
        if not include_synthetic:
            where.append("NOT synthetic")
        for term in terms:
            where.append("contains(search_text, ?)")
            params.append(str(term).lower())
        if tickers:
            marks = ",".join("?" * len(tickers))
            where.append(
                "item_hash IN (SELECT item_hash FROM news_item_tickers "
                f"WHERE ticker IN ({marks}))")
            params.extend(list(tickers))
        params.extend([int(limit), int(offset)])
        return self._rows(
            "SELECT * FROM news_items WHERE " + " AND ".join(where)
            + " ORDER BY published DESC, item_hash LIMIT ? OFFSET ?", params)

    def count_news_matches(self, *, as_of: str, terms=(), tickers=(),
                           include_synthetic: bool = False) -> int:
        """The full match total, which the page length cannot stand in for.

        Every ratio the relevance report computes is over the whole match set;
        computing them from one page would make the answer depend on paging.
        """
        rows = self.search_news(as_of=as_of, terms=terms, tickers=tickers,
                                include_synthetic=include_synthetic,
                                limit=1_000_000, offset=0)
        return len(rows)

    def archive_stats(self) -> dict:
        """Size and span of the archive. Cache this — min/max are unindexed."""
        row = self.con.execute(
            "SELECT count(*), min(published), max(published), "
            "count(*) FILTER (WHERE synthetic) FROM news_items").fetchone()
        total = int(row[0] or 0)
        return {
            "rows": total,
            # None, not "" — an archive with no rows has no span, and an empty
            # string would sort and compare as though it did.
            "begins": row[1],
            "newest_published": row[2],
            "synthetic_rows": int(row[3] or 0),
        }

    def record_news_citation(self, item_hash: str, *, cited_by_kind: str,
                             cited_by_id: str) -> None:
        """Bind a stored record to the thing that cited it.

        ``cited_by_kind`` is restricted to tables that exist. A kind naming no
        table would leave cited_by_id resolving to nothing, which is a dangling
        citation dressed as provenance. An Atlas view is recorded through
        record_event, whose event_id does resolve.
        """
        allowed = {"decision", "workflow_step", "event"}
        if cited_by_kind not in allowed:
            raise ValueError(
                f"cited_by_kind must be one of {sorted(allowed)}; "
                f"{cited_by_kind!r} names no table a citation could resolve to")
        row = self.con.execute(
            "SELECT synthetic FROM news_items WHERE item_hash = ?",
            [item_hash]).fetchone()
        if row is None:
            raise KeyError(f"unknown news item_hash {item_hash!r}")
        if bool(row[0]):
            raise ValueError(
                f"news item {item_hash!r} is synthetic and may never be cited "
                "as evidence")
        self.con.execute(
            "INSERT INTO news_citations VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
            [item_hash, cited_by_kind, cited_by_id, _now()])

    def read_events(self, limit: int = 100, after: str | None = None) -> list[dict]:
        """Read an ordered event window for observer clients.

        ``after`` is an ISO timestamp returned by an earlier call. Initial
        reads return the newest ``limit`` rows in chronological order; cursor
        reads return only newer rows. Event ids remain the client-side
        deduplication key when multiple events share a timestamp.
        """
        limit = max(1, min(int(limit), 500))
        if after:
            return self._rows(
                "SELECT * FROM events WHERE ts > ? ORDER BY ts ASC LIMIT ?",
                [after, limit],
            )
        return self._rows(
            "SELECT * FROM (SELECT * FROM events ORDER BY ts DESC LIMIT ?) "
            "ORDER BY ts ASC",
            [limit],
        )

    # -- internals ----------------------------------------------------------
    def _rows(self, query: str, params: list) -> list[dict]:
        cur = self.con.execute(query, params)
        cols = [d[0] for d in cur.description]
        out = []
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            for k, v in d.items():
                if k in ("spec", "tickers", "summary", "params", "weights",
                         "diagnostics", "metrics", "choice", "realized_outcome",
                         "targets", "pre_trade", "payload", "reasons",
                         "legs", "request", "result", "artifacts",
                         "permit", "trigger_payload", "conclusion",
                         "expected_cost", "lesson", "material_claims",
                         "adjudication", "evidence_refs",
                         "allowed_universe") and isinstance(v, str):
                    try:
                        d[k] = json.loads(v)
                    except Exception:
                        pass
            out.append(d)
        return out


def _agent_for_phase(phase: str) -> str:
    # The judge is the referee agent wearing its comparison hat: it holds the
    # evidence-reading tools and no solver, which is exactly a judge's kit.
    return {
        "analyst": "moments-analyst",
        "challenger": "challenger",
        "optimizer": "optimization-runner",
        "judge": "referee",
        "referee": "referee",
        "reporter": "reporter",
        "news-analyst": "news-analyst",
    }[_phase_type(phase)]
