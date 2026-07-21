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
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import duckdb

from qlab.core.types import Decision, MomentSet, Objective, SolveResult, _jsonable
from qlab.paths import state_path


WORKFORCE_PHASES = ("analyst", "challenger", "optimizer", "referee", "reporter")
# Dependency DAG, not a strict line: challenger and optimizer both consume only
# the analyst's outputs and never each other's, so they may run concurrently.
# The referee — the gate that lets a result be used — still waits for both, and
# the reporter waits for the referee. Every real dependency (and "the judgment
# is defended before the result is used") is preserved; only the optimizer's
# artificial wait on the challenger is dropped, which is what enables the
# parallel stage. Prerequisites must be a subset of the lower-seq phases so the
# seq-ordered completion bookkeeping below stays valid.
_WORKFORCE_DEPS = {
    "analyst": (),
    "challenger": ("analyst",),
    "optimizer": ("analyst",),
    "referee": ("challenger", "optimizer"),
    "reporter": ("referee",),
}
_WORKFORCE_REQUIRED_ARTIFACTS = {
    "analyst": ("moment_set_id", "objective_id", "decision_id"),
    "challenger": ("challenger_view",),
    "optimizer": ("targets", "algorithm_id"),
    "referee": ("verdict", "verdict_id", "targets"),
    "reporter": ("recommendation",),
}


def targets_hash(targets: dict[str, float]) -> str:
    """Canonical content hash of a target-weights dict.

    THE canonical definition — both verdict writers (the referee) and verdict
    checkers (``execute_plan``) must import this, so a PASS can never be
    silently applied to a different set of targets than the one reviewed.
    """
    key = ",".join(f"{k}:{float(v):.6f}" for k, v in sorted(targets.items()))
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
        """Flatten positions and reset the account to starting capital (for demos)."""
        self.con.execute("DELETE FROM positions")
        self.con.execute("DELETE FROM orders")
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
        """Create one durable, phase-ordered Claude workforce run."""
        if not phases or len(set(phases)) != len(phases):
            raise ValueError("workflow phases must be non-empty and unique")
        unknown = set(phases) - set(WORKFORCE_PHASES)
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
        if phase not in WORKFORCE_PHASES:
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
        for dependency in _WORKFORCE_DEPS.get(phase, ()):
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
                key for key in _WORKFORCE_REQUIRED_ARTIFACTS[phase]
                if key not in artifacts or artifacts[key] in (None, "", {})
            ]
            if missing:
                raise ValueError(
                    f"phase {phase!r} cannot complete without artifacts {missing}"
                )
            if phase == "optimizer" and not isinstance(artifacts["targets"], dict):
                raise ValueError("optimizer artifact 'targets' must be an object")
            if phase == "referee":
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
                next_step = next(
                    (candidate for candidate in steps
                     if int(candidate["seq"]) == int(step["seq"]) + 1),
                    None,
                )
                workflow_status = "complete" if next_step is None else "running"
                current_phase = phase if next_step is None else next_step["phase"]
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
                         "legs", "request", "result", "artifacts") and isinstance(v, str):
                    try:
                        d[k] = json.loads(v)
                    except Exception:
                        pass
            out.append(d)
        return out


def _agent_for_phase(phase: str) -> str:
    return {
        "analyst": "moments-analyst",
        "challenger": "challenger",
        "optimizer": "optimization-runner",
        "referee": "referee",
        "reporter": "reporter",
    }[phase]
