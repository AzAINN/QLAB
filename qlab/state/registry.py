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
}
_WORKFORCE_REQUIRED_ARTIFACTS = {
    "analyst": ("moment_set_id", "objective_id", "decision_id"),
    "challenger": ("challenger_view",),
    "optimizer": ("targets", "algorithm_id"),
    "judge": ("winner_phase", "winning_targets", "evidence"),
    "referee": ("verdict", "verdict_id", "targets"),
    "reporter": ("recommendation",),
}
_MAX_PANEL_VARIANTS = 5


def _phase_type(phase: str) -> str:
    """'analyst-3' → 'analyst'; panel branches share their base type's rules."""
    base, dash, suffix = phase.rpartition("-")
    if dash and suffix.isdigit():
        return base
    return phase


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
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY, cash DOUBLE, high_water_mark DOUBLE,
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
-- `book` is the broker the equity belongs to. Two books' equity levels can
-- never compose one return series, so every mark carries its own; the
-- idempotency key stays (ts, source).
CREATE TABLE IF NOT EXISTS equity_marks (
    ts VARCHAR, source VARCHAR, book VARCHAR, equity DOUBLE, cash DOUBLE,
    PRIMARY KEY (ts, source));
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        self.con.execute(_SCHEMA)
        self.con.execute("ALTER TABLE backtests ADD COLUMN IF NOT EXISTS objective VARCHAR")
        # existing dev DBs may predate these columns; _SCHEMA alone won't add
        # them to an already-created table, hence the explicit ALTERs here.
        self.con.execute("ALTER TABLE verdicts ADD COLUMN IF NOT EXISTS targets_hash VARCHAR")
        self.con.execute("ALTER TABLE verdicts ADD COLUMN IF NOT EXISTS seq BIGINT")
        self.con.execute("ALTER TABLE plans ADD COLUMN IF NOT EXISTS legs VARCHAR")

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
        self.con.execute(
            "UPDATE decisions SET realized_outcome=?, reflection=? WHERE decision_id=?",
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
    ) -> list[dict]:
        """Return reflected decisions nearest to a regime fingerprint.

        The two percentile fields are already normalized to ``[0, 1]``, so
        their mean absolute difference is the numeric distance. A matching
        regime label adds a small bonus; the resulting ``similarity_score`` is
        normalized back to ``[0, 1]``. Older decisions without a complete,
        valid fingerprint are not comparable and are skipped.
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

        scored: list[tuple[float, dict]] = []
        for row in self._rows(q, params):
            choice = row.get("choice") or {}
            outcome = row.get("realized_outcome") or {}

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
    def init_account(self, cash: float) -> None:
        self.con.execute(
            "INSERT INTO account VALUES (1,?,?,?,?) ON CONFLICT DO NOTHING",
            [cash, cash, False, _now()],
        )

    def get_account(self) -> dict:
        r = self._rows("SELECT * FROM account WHERE id=1", [])
        return r[0] if r else {}

    def get_positions(self) -> dict[str, dict]:
        rows = self._rows("SELECT * FROM positions", [])
        return {r["ticker"]: r for r in rows}

    def apply_fill(self, ticker: str, dqty: float, price: float, cash_delta: float) -> None:
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
            "UPDATE account SET cash = cash + ?, updated_at=? WHERE id=1",
            [cash_delta, _now()])

    def set_halt(self, halted: bool) -> None:
        self.con.execute("UPDATE account SET halted=?, updated_at=? WHERE id=1",
                         [halted, _now()])

    def reset_book(self, cash: float) -> None:
        """Discard the book: positions, orders, marks, and the account state.

        The equity marks go with it. Keeping them would splice the discarded
        book's equity level onto the fresh one, and the first mark after a reset
        would read as a market loss the size of the discarded gains — a
        fabricated return that then propagates into max_drawdown, ann_vol and
        cvar_95. A reset is already a destructive wipe; the history goes too.
        """
        self.con.execute("DELETE FROM positions")
        self.con.execute("DELETE FROM orders")
        self.con.execute("DELETE FROM equity_marks")
        self.con.execute(
            "UPDATE account SET cash=?, high_water_mark=?, halted=FALSE, updated_at=? "
            "WHERE id=1", [cash, cash, _now()])

    def update_high_water_mark(self, equity: float) -> None:
        self.con.execute(
            "UPDATE account SET high_water_mark = GREATEST(high_water_mark, ?), "
            "updated_at=? WHERE id=1", [equity, _now()])

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
            "INSERT INTO orders VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            [client_order_id, plan_id, ticker, side, notional, state, _now()])

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
        if kind == "panel":
            variants = request.get("variants")
            if not isinstance(variants, list) or not all(
                    isinstance(v, dict) for v in variants):
                raise ValueError("panel requires request['variants']: list[dict]")
            phases, deps = panel_phases(len(variants))
            request["_deps"] = {phase: list(d) for phase, d in deps.items()}
        if not phases or len(set(phases)) != len(phases):
            raise ValueError("workflow phases must be non-empty and unique")
        unknown = {
            phase for phase in phases
            if _phase_type(phase) not in _WORKFORCE_REQUIRED_ARTIFACTS
        }
        if unknown:
            raise ValueError(f"unknown workforce phases: {sorted(unknown)}")

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
        started_at = step.get("started_at") or now
        completed_at = now if status in {"done", "failed", "blocked"} else None
        with self.transaction():
            self.con.execute(
                "UPDATE workflow_steps SET status=?, summary=?, artifacts=?, "
                "started_at=COALESCE(started_at, ?), completed_at=?, updated_at=? "
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
            # The reviewed decision is the winning branch's own analyst decision.
            winner = str((judge_step.get("artifacts") or {}).get("winner_phase", ""))
            branch = winner.rpartition("-")[2]
            expected_decision = self._phase_decision_id(by_phase, f"analyst-{branch}")
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
                         "permit") and isinstance(v, str):
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
    }[_phase_type(phase)]
