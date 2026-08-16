"""SQLite-backed run, task, event, and evidence persistence.

All scheduling mutations use ``BEGIN IMMEDIATE`` so claiming a ready task is
atomic across processes sharing the same database file.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, Iterable

from .models import FactorySpec
from .state import (
    RunState,
    TaskState,
    validate_run_transition,
    validate_task_transition,
)


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


class StoreError(RuntimeError):
    pass


class IdempotencyConflict(StoreError):
    pass


class LeaseLost(StoreError):
    pass


class UnknownRun(StoreError):
    pass


@dataclass(frozen=True, slots=True)
class ClaimedTask:
    run_id: str
    task_id: str
    attempt: int
    lease_owner: str
    lease_expires_at: float


@dataclass(frozen=True, slots=True)
class CompletionEffect:
    """Compensating actions for a side effect fenced by task completion.

    ``rollback`` restores the state that existed before the effect was applied.
    ``finalize`` removes temporary recovery material after either commit or
    rollback.  The store invokes both while the caller still owns the attempt.
    """

    rollback: Callable[[], None]
    finalize: Callable[[], None]


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    spec_hash TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    state TEXT NOT NULL,
    kill_switch INTEGER NOT NULL DEFAULT 0 CHECK (kill_switch IN (0, 1)),
    failure_reason TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    max_attempts INTEGER NOT NULL,
    max_wall_seconds REAL NOT NULL,
    event_count INTEGER NOT NULL DEFAULT 0,
    event_head_hash TEXT NOT NULL DEFAULT '0000000000000000000000000000000000000000000000000000000000000000'
);
CREATE TABLE IF NOT EXISTS tasks (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL,
    sort_order INTEGER NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    next_attempt_at REAL,
    lease_owner TEXT,
    lease_expires_at REAL,
    result_json TEXT,
    error TEXT,
    started_at REAL,
    finished_at REAL,
    PRIMARY KEY (run_id, task_id)
);
CREATE INDEX IF NOT EXISTS tasks_schedulable
    ON tasks(run_id, state, sort_order, next_attempt_at);
CREATE TABLE IF NOT EXISTS dependencies (
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    dependency_id TEXT NOT NULL,
    PRIMARY KEY (run_id, task_id, dependency_id),
    FOREIGN KEY (run_id, task_id) REFERENCES tasks(run_id, task_id) ON DELETE CASCADE,
    FOREIGN KEY (run_id, dependency_id) REFERENCES tasks(run_id, task_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    task_id TEXT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    event_key TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL,
    UNIQUE(run_id, event_key)
);
CREATE INDEX IF NOT EXISTS events_by_run ON events(run_id, sequence);
CREATE TABLE IF NOT EXISTS receipts (
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    receipt_hash TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (run_id, task_id, attempt),
    FOREIGN KEY (run_id, task_id) REFERENCES tasks(run_id, task_id) ON DELETE CASCADE
);
"""


class FactoryStore:
    def __init__(
        self,
        database: str | Path,
        *,
        clock: Callable[[], float] = time.time,
        create: bool = True,
    ):
        self.path = Path(database)
        if str(database) == ":memory:":
            raise ValueError("use a file-backed SQLite database for atomic worker claims")
        self.clock = clock
        if not create and not self.path.is_file():
            raise FileNotFoundError(f"factory database does not exist: {self.path}")
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, 1}:
                raise StoreError(f"unsupported database schema version: {version}")
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
            if version == 0 and tables:
                raise StoreError(
                    "unversioned non-empty database requires an explicit migration"
                )
            connection.executescript(SCHEMA)
            connection.execute("PRAGMA user_version = 1")
            event_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(events)")
            }
            run_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(runs)")
            }
            if not {"previous_hash", "event_hash"} <= event_columns or not {
                "event_count",
                "event_head_hash",
            } <= run_columns:
                raise StoreError("database schema does not match version 1")

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        task_id: str | None,
        event_type: str,
        payload: object,
        created_at: float,
        event_key: str,
    ) -> None:
        rendered = _canonical(payload)
        existing = connection.execute(
            "SELECT event_type, payload_json FROM events WHERE run_id=? AND event_key=?",
            (run_id, event_key),
        ).fetchone()
        if existing:
            if existing["event_type"] != event_type or existing["payload_json"] != rendered:
                raise IdempotencyConflict(
                    f"event key {event_key!r} was reused with different content"
                )
            return
        anchor = connection.execute(
            "SELECT event_count, event_head_hash FROM runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if anchor is None:
            raise UnknownRun(run_id)
        actual = connection.execute(
            "SELECT COUNT(*) AS count, MAX(sequence) AS last_sequence FROM events WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if actual["count"] != anchor["event_count"]:
            raise StoreError("event-chain anchor count does not match stored events")
        previous_hash = anchor["event_head_hash"]
        material = {
            "run_id": run_id,
            "task_id": task_id,
            "event_type": event_type,
            "payload": payload,
            "created_at": created_at,
            "event_key": event_key,
            "previous_hash": previous_hash,
        }
        event_hash = sha256(_canonical(material).encode("utf-8")).hexdigest()
        connection.execute(
            """
            INSERT INTO events(
                run_id, task_id, event_type, payload_json, created_at, event_key,
                previous_hash, event_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                task_id,
                event_type,
                rendered,
                created_at,
                event_key,
                previous_hash,
                event_hash,
            ),
        )
        connection.execute(
            "UPDATE runs SET event_count=event_count+1, event_head_hash=? WHERE run_id=?",
            (event_hash, run_id),
        )

    def create_run(self, spec: FactorySpec, idempotency_key: str | None = None) -> str:
        spec_json = spec.canonical_json()
        spec_hash = sha256(spec_json.encode("utf-8")).hexdigest()
        key = idempotency_key or f"spec:{spec_hash}"
        if not key or len(key) > 256:
            raise ValueError("idempotency key must contain 1..256 characters")
        run_id = sha256(f"{key}\0{spec_hash}".encode("utf-8")).hexdigest()[:24]
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = self.clock()
            existing = connection.execute(
                "SELECT run_id, spec_hash FROM runs WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing:
                if existing["spec_hash"] != spec_hash:
                    raise IdempotencyConflict(
                        "idempotency key is already bound to a different specification"
                    )
                connection.commit()
                return str(existing["run_id"])
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, idempotency_key, spec_hash, spec_json, state,
                    created_at, max_attempts, max_wall_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    key,
                    spec_hash,
                    spec_json,
                    RunState.CREATED,
                    now,
                    spec.budget.max_attempts,
                    spec.budget.max_wall_seconds,
                ),
            )
            for order, task in enumerate(spec.tasks):
                initial_state = TaskState.READY if not task.depends_on else TaskState.PENDING
                connection.execute(
                    """
                    INSERT INTO tasks(
                        run_id, task_id, sort_order, state, max_attempts
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        task.id,
                        order,
                        initial_state,
                        task.max_attempts or spec.budget.default_max_attempts,
                    ),
                )
            for task in spec.tasks:
                for dependency in task.depends_on:
                    connection.execute(
                        "INSERT INTO dependencies(run_id, task_id, dependency_id) VALUES (?, ?, ?)",
                        (run_id, task.id, dependency),
                    )
            self._event(
                connection,
                run_id=run_id,
                task_id=None,
                event_type="run.created",
                payload={"spec_hash": spec_hash, "task_count": len(spec.tasks)},
                created_at=now,
                event_key="run.created",
            )
            connection.commit()
            return run_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def load_spec(self, run_id: str) -> FactorySpec:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT spec_json FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise UnknownRun(run_id)
        return FactorySpec.from_json(row["spec_json"])

    def start_run(self, run_id: str) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = self.clock()
            row = connection.execute(
                "SELECT state FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise UnknownRun(run_id)
            if row["state"] == RunState.RUNNING:
                connection.commit()
                return
            validate_run_transition(row["state"], RunState.RUNNING)
            connection.execute(
                "UPDATE runs SET state=?, started_at=? WHERE run_id=?",
                (RunState.RUNNING, now, run_id),
            )
            self._event(
                connection,
                run_id=run_id,
                task_id=None,
                event_type="run.started",
                payload={},
                created_at=now,
                event_key="run.started",
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _transition_task(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        task_id: str,
        current: str,
        target: TaskState,
        now: float,
        *,
        payload: dict[str, Any] | None = None,
        event_key: str,
        extra_sql: str = "",
        extra_values: Iterable[object] = (),
    ) -> None:
        validate_task_transition(current, target)
        cursor = connection.execute(
            f"UPDATE tasks SET state=?{extra_sql} WHERE run_id=? AND task_id=? AND state=?",
            (target, *extra_values, run_id, task_id, current),
        )
        if cursor.rowcount != 1:
            raise StoreError(f"task {task_id!r} changed concurrently")
        self._event(
            connection,
            run_id=run_id,
            task_id=task_id,
            event_type="task.transition",
            payload={"from": current, "to": target, **(payload or {})},
            created_at=now,
            event_key=event_key,
        )

    def _refresh_tasks(self, connection: sqlite3.Connection, run_id: str, now: float) -> None:
        due = connection.execute(
            """
            SELECT task_id, state, attempts FROM tasks
            WHERE run_id=? AND state=? AND next_attempt_at<=?
            ORDER BY sort_order
            """,
            (run_id, TaskState.RETRY_WAIT, now),
        ).fetchall()
        for row in due:
            self._transition_task(
                connection,
                run_id,
                row["task_id"],
                row["state"],
                TaskState.READY,
                now,
                event_key=f"task.ready:{row['task_id']}:{row['attempts'] + 1}",
                extra_sql=", next_attempt_at=NULL",
            )

        # Reach a fixed point so dependency propagation is independent of the
        # order tasks appeared in the input document.
        while True:
            changed = False
            pending = connection.execute(
                "SELECT task_id, state FROM tasks WHERE run_id=? AND state=? ORDER BY sort_order",
                (run_id, TaskState.PENDING),
            ).fetchall()
            for row in pending:
                dependency_states = [
                    item["state"]
                    for item in connection.execute(
                        """
                        SELECT dependency.state
                        FROM dependencies AS edge
                        JOIN tasks AS dependency
                          ON dependency.run_id=edge.run_id
                         AND dependency.task_id=edge.dependency_id
                        WHERE edge.run_id=? AND edge.task_id=?
                        """,
                        (run_id, row["task_id"]),
                    ).fetchall()
                ]
                if any(
                    state in {TaskState.FAILED, TaskState.BLOCKED, TaskState.CANCELLED}
                    for state in dependency_states
                ):
                    self._transition_task(
                        connection,
                        run_id,
                        row["task_id"],
                        row["state"],
                        TaskState.BLOCKED,
                        now,
                        payload={"reason": "dependency did not succeed"},
                        event_key=f"task.blocked:{row['task_id']}",
                        extra_sql=", error=?, finished_at=?",
                        extra_values=("dependency did not succeed", now),
                    )
                    changed = True
                elif dependency_states and all(
                    state == TaskState.SUCCEEDED for state in dependency_states
                ):
                    self._transition_task(
                        connection,
                        run_id,
                        row["task_id"],
                        row["state"],
                        TaskState.READY,
                        now,
                        event_key=f"task.ready:{row['task_id']}:1",
                    )
                    changed = True
            if not changed:
                break

    def _recover_expired(self, connection: sqlite3.Connection, run_id: str, now: float) -> None:
        expired = connection.execute(
            """
            SELECT task_id, state, attempts, max_attempts, lease_owner
            FROM tasks
            WHERE run_id=? AND state=? AND lease_expires_at<=?
            ORDER BY sort_order
            """,
            (run_id, TaskState.RUNNING, now),
        ).fetchall()
        for row in expired:
            target = (
                TaskState.FAILED
                if row["attempts"] >= row["max_attempts"]
                else TaskState.RETRY_WAIT
            )
            next_at = now if target == TaskState.RETRY_WAIT else None
            self._transition_task(
                connection,
                run_id,
                row["task_id"],
                row["state"],
                target,
                now,
                payload={"reason": "lease expired", "lease_owner": row["lease_owner"]},
                event_key=f"task.lease_expired:{row['task_id']}:{row['attempts']}",
                extra_sql=(
                    ", lease_owner=NULL, lease_expires_at=NULL, next_attempt_at=?, "
                    "error=?, finished_at=?"
                ),
                extra_values=(
                    next_at,
                    "lease expired",
                    now if target == TaskState.FAILED else None,
                ),
            )

    def claim_ready_task(
        self, run_id: str, worker_id: str, lease_seconds: float
    ) -> ClaimedTask | None:
        if not worker_id or len(worker_id) > 128:
            raise ValueError("worker id must contain 1..128 characters")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = self.clock()
            run = connection.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise UnknownRun(run_id)
            if run["state"] != RunState.RUNNING or run["kill_switch"]:
                connection.commit()
                return None
            if run["started_at"] is not None and now - run["started_at"] >= run["max_wall_seconds"]:
                connection.commit()
                self.activate_kill_switch(run_id, reason="wall-clock budget exhausted")
                return None

            self._recover_expired(connection, run_id, now)
            self._refresh_tasks(connection, run_id, now)
            attempts = connection.execute(
                "SELECT COALESCE(SUM(attempts), 0) AS value FROM tasks WHERE run_id=?",
                (run_id,),
            ).fetchone()["value"]
            if attempts >= run["max_attempts"]:
                connection.commit()
                self.activate_kill_switch(run_id, reason="attempt budget exhausted")
                return None
            row = connection.execute(
                """
                SELECT task_id, state, attempts FROM tasks
                WHERE run_id=? AND state=?
                ORDER BY sort_order, task_id LIMIT 1
                """,
                (run_id, TaskState.READY),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            attempt = int(row["attempts"]) + 1
            expires_at = now + lease_seconds
            self._transition_task(
                connection,
                run_id,
                row["task_id"],
                row["state"],
                TaskState.RUNNING,
                now,
                payload={"attempt": attempt, "worker": worker_id},
                event_key=f"task.claimed:{row['task_id']}:{attempt}",
                extra_sql=(
                    ", attempts=?, lease_owner=?, lease_expires_at=?, started_at=?, "
                    "error=NULL, finished_at=NULL"
                ),
                extra_values=(attempt, worker_id, expires_at, now),
            )
            connection.commit()
            return ClaimedTask(run_id, row["task_id"], attempt, worker_id, expires_at)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def renew_lease(self, claim: ClaimedTask, lease_seconds: float) -> ClaimedTask:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = self.clock()
            expires_at = now + lease_seconds
            cursor = connection.execute(
                """
                UPDATE tasks SET lease_expires_at=?
                WHERE run_id=? AND task_id=? AND state=? AND attempts=?
                  AND lease_owner=? AND lease_expires_at>?
                """,
                (
                    expires_at,
                    claim.run_id,
                    claim.task_id,
                    TaskState.RUNNING,
                    claim.attempt,
                    claim.lease_owner,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLost(f"lease lost for {claim.task_id!r}")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        return ClaimedTask(
            claim.run_id, claim.task_id, claim.attempt, claim.lease_owner, expires_at
        )

    def complete_task(
        self,
        claim: ClaimedTask,
        *,
        succeeded: bool,
        receipt: dict[str, Any],
        receipt_hash: str,
        error: str | None,
        retryable: bool = True,
        retry_base_seconds: float,
        retry_cap_seconds: float,
        before_transition: Callable[[], CompletionEffect | None] | None = None,
    ) -> TaskState:
        if retry_base_seconds < 0 or retry_cap_seconds < retry_base_seconds:
            raise ValueError("retry delays must satisfy 0 <= base <= cap")
        receipt_json = _canonical(receipt)
        calculated_hash = sha256(receipt_json.encode("utf-8")).hexdigest()
        if receipt_hash != calculated_hash:
            raise ValueError("receipt hash does not match canonical receipt JSON")
        connection = self._connect()
        effect: CompletionEffect | None = None
        committed = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = self.clock()
            row = connection.execute(
                "SELECT * FROM tasks WHERE run_id=? AND task_id=?",
                (claim.run_id, claim.task_id),
            ).fetchone()
            existing = connection.execute(
                """
                SELECT receipt_hash FROM receipts
                WHERE run_id=? AND task_id=? AND attempt=?
                """,
                (claim.run_id, claim.task_id, claim.attempt),
            ).fetchone()
            if existing:
                if existing["receipt_hash"] != receipt_hash:
                    raise IdempotencyConflict("attempt receipt already exists with another hash")
                connection.commit()
                if row is None:
                    raise StoreError(f"task {claim.task_id!r} disappeared")
                return TaskState(row["state"])
            if (
                row is None
                or row["state"] != TaskState.RUNNING
                or row["attempts"] != claim.attempt
                or row["lease_owner"] != claim.lease_owner
            ):
                raise LeaseLost(f"lease lost for {claim.task_id!r}")
            if row["lease_expires_at"] <= now:
                raise LeaseLost(f"lease expired for {claim.task_id!r}")
            if len(receipt_json.encode("utf-8")) > 10_000_000:
                raise ValueError("receipt exceeds the 10 MB persistence limit")
            # Keep lease validation, optional publication, and completion under
            # one write transaction. A competing worker cannot reclaim the
            # attempt between the fence check and its filesystem commit.
            if before_transition is not None:
                candidate = before_transition()
                if candidate is not None and not isinstance(candidate, CompletionEffect):
                    raise TypeError("completion callback returned an invalid effect")
                effect = candidate
            connection.execute(
                """
                INSERT INTO receipts(
                    run_id, task_id, attempt, receipt_hash, receipt_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    claim.run_id,
                    claim.task_id,
                    claim.attempt,
                    receipt_hash,
                    receipt_json,
                    now,
                ),
            )

            if succeeded:
                target = TaskState.SUCCEEDED
                next_at = None
            elif retryable and row["attempts"] < row["max_attempts"]:
                target = TaskState.RETRY_WAIT
                delay = min(
                    retry_cap_seconds,
                    retry_base_seconds * (2 ** max(0, claim.attempt - 1)),
                )
                next_at = now + delay
            else:
                target = TaskState.FAILED
                next_at = None
            self._transition_task(
                connection,
                claim.run_id,
                claim.task_id,
                row["state"],
                target,
                now,
                payload={
                    "attempt": claim.attempt,
                    "receipt_hash": receipt_hash,
                    "error": error,
                },
                event_key=f"task.completed:{claim.task_id}:{claim.attempt}",
                extra_sql=(
                    ", lease_owner=NULL, lease_expires_at=NULL, next_attempt_at=?, "
                    "result_json=?, error=?, finished_at=?"
                ),
                extra_values=(
                    next_at,
                    receipt_json,
                    error,
                    now if target in {TaskState.SUCCEEDED, TaskState.FAILED} else None,
                ),
            )
            self._refresh_tasks(connection, claim.run_id, now)
            connection.commit()
            committed = True
            return target
        except BaseException:
            try:
                connection.rollback()
            finally:
                if effect is not None and not committed:
                    try:
                        effect.rollback()
                    finally:
                        effect.finalize()
            raise
        finally:
            if effect is not None and committed:
                try:
                    effect.finalize()
                except Exception:
                    # The durable transition and its external effect committed.
                    # Failure to remove recovery material must not make callers
                    # retry an already-completed task.
                    pass
            connection.close()

    def activate_kill_switch(self, run_id: str, *, reason: str) -> None:
        if not reason.strip():
            raise ValueError("kill-switch reason must not be blank")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = self.clock()
            run = connection.execute(
                "SELECT state, kill_switch FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise UnknownRun(run_id)
            if run["kill_switch"]:
                connection.commit()
                return
            if run["state"] in {
                RunState.SUCCEEDED,
                RunState.FAILED,
                RunState.CANCELLED,
            }:
                connection.commit()
                return
            validate_run_transition(run["state"], RunState.CANCELLED)
            rows = connection.execute(
                """
                SELECT task_id, state, attempts FROM tasks
                WHERE run_id=? AND state IN (?, ?, ?, ?)
                ORDER BY sort_order
                """,
                (
                    run_id,
                    TaskState.PENDING,
                    TaskState.READY,
                    TaskState.RUNNING,
                    TaskState.RETRY_WAIT,
                ),
            ).fetchall()
            for row in rows:
                self._transition_task(
                    connection,
                    run_id,
                    row["task_id"],
                    row["state"],
                    TaskState.CANCELLED,
                    now,
                    payload={"reason": reason},
                    event_key=f"task.cancelled:{row['task_id']}:{row['attempts']}",
                    extra_sql=(
                        ", lease_owner=NULL, lease_expires_at=NULL, error=?, finished_at=?"
                    ),
                    extra_values=(reason, now),
                )
            connection.execute(
                """
                UPDATE runs
                SET state=?, kill_switch=1, failure_reason=?, finished_at=?
                WHERE run_id=?
                """,
                (RunState.CANCELLED, reason, now, run_id),
            )
            self._event(
                connection,
                run_id=run_id,
                task_id=None,
                event_type="run.kill_switch",
                payload={"reason": reason},
                created_at=now,
                event_key="run.kill_switch",
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def finalize_run(self, run_id: str) -> RunState:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = self.clock()
            run = connection.execute(
                "SELECT state FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise UnknownRun(run_id)
            current = RunState(run["state"])
            if current in {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}:
                connection.commit()
                return current
            self._refresh_tasks(connection, run_id, now)
            states = [
                TaskState(row["state"])
                for row in connection.execute(
                    "SELECT state FROM tasks WHERE run_id=?", (run_id,)
                ).fetchall()
            ]
            if states and all(state == TaskState.SUCCEEDED for state in states):
                target = RunState.SUCCEEDED
                reason = None
            elif any(state in {TaskState.RUNNING, TaskState.READY, TaskState.RETRY_WAIT} for state in states):
                connection.commit()
                return current
            elif any(state in {TaskState.FAILED, TaskState.BLOCKED} for state in states):
                target = RunState.FAILED
                reason = "one or more tasks did not succeed"
            else:
                connection.commit()
                return current
            validate_run_transition(current, target)
            connection.execute(
                "UPDATE runs SET state=?, failure_reason=?, finished_at=? WHERE run_id=?",
                (target, reason, now, run_id),
            )
            self._event(
                connection,
                run_id=run_id,
                task_id=None,
                event_type="run.finished",
                payload={"state": target, "reason": reason},
                created_at=now,
                event_key="run.finished",
            )
            connection.commit()
            return target
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def next_retry_at(self, run_id: str) -> float | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT MIN(next_attempt_at) AS value FROM tasks
                WHERE run_id=? AND state=?
                """,
                (run_id, TaskState.RETRY_WAIT),
            ).fetchone()
        return None if row is None or row["value"] is None else float(row["value"])

    def _snapshot_with_connection(
        self, connection: sqlite3.Connection, run_id: str
    ) -> dict[str, Any]:
        run = connection.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if run is None:
            raise UnknownRun(run_id)
        tasks = connection.execute(
            """
            SELECT task_id, state, attempts, max_attempts, next_attempt_at,
                   lease_owner, lease_expires_at, error, started_at, finished_at
            FROM tasks WHERE run_id=? ORDER BY sort_order, task_id
            """,
            (run_id,),
        ).fetchall()
        receipt_count = connection.execute(
            "SELECT COUNT(*) AS value FROM receipts WHERE run_id=?", (run_id,)
        ).fetchone()["value"]
        task_items = [dict(row) for row in tasks]
        counts: dict[str, int] = {}
        for task in task_items:
            counts[task["state"]] = counts.get(task["state"], 0) + 1
        return {
            "run_id": run_id,
            "state": run["state"],
            "spec_hash": run["spec_hash"],
            "kill_switch": bool(run["kill_switch"]),
            "failure_reason": run["failure_reason"],
            "created_at": run["created_at"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "counts": dict(sorted(counts.items())),
            "receipt_count": receipt_count,
            "event_count": run["event_count"],
            "event_head_hash": run["event_head_hash"],
            "tasks": task_items,
        }

    def snapshot(self, run_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            return self._snapshot_with_connection(connection, run_id)

    def _replay_with_connection(
        self, connection: sqlite3.Connection, run_id: str
    ) -> list[dict[str, Any]]:
        anchor = connection.execute(
            "SELECT event_count, event_head_hash FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if not anchor:
            raise UnknownRun(run_id)
        rows = connection.execute(
            """
            SELECT sequence, task_id, event_type, payload_json, created_at, event_key,
                   previous_hash, event_hash
            FROM events WHERE run_id=? ORDER BY sequence
            """,
            (run_id,),
        ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError as exc:
                raise StoreError(
                    f"event payload is invalid at sequence {row['sequence']}"
                ) from exc
            events.append(
                {
                    "sequence": row["sequence"],
                    "task_id": row["task_id"],
                    "event_type": row["event_type"],
                    "payload": payload,
                    "created_at": row["created_at"],
                    "event_key": row["event_key"],
                    "previous_hash": row["previous_hash"],
                    "event_hash": row["event_hash"],
                }
            )
        previous_hash = "0" * 64
        for event in events:
            try:
                material = {
                    "run_id": run_id,
                    "task_id": event["task_id"],
                    "event_type": event["event_type"],
                    "payload": event["payload"],
                    "created_at": event["created_at"],
                    "event_key": event["event_key"],
                    "previous_hash": previous_hash,
                }
                calculated = sha256(_canonical(material).encode("utf-8")).hexdigest()
            except (TypeError, ValueError) as exc:
                raise StoreError("event chain contains invalid JSON data") from exc
            if event["previous_hash"] != previous_hash or event["event_hash"] != calculated:
                raise StoreError(
                    f"event chain verification failed at sequence {event['sequence']}"
                )
            previous_hash = calculated
        if len(events) != anchor["event_count"] or previous_hash != anchor["event_head_hash"]:
            raise StoreError("event chain does not match the run anchor")
        return events

    def replay(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return self._replay_with_connection(connection, run_id)

    def export(self, run_id: str) -> dict[str, Any]:
        connection = self._connect()
        try:
            # Pin spec, status, event chain, and receipts to one WAL snapshot.
            # A concurrent completion can commit without producing a bundle
            # that mixes rows from before and after that transition.
            connection.execute("BEGIN")
            run = connection.execute(
                "SELECT spec_json FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise UnknownRun(run_id)
            receipts = connection.execute(
                """
                SELECT task_id, attempt, receipt_hash, receipt_json, created_at
                FROM receipts WHERE run_id=? ORDER BY task_id, attempt
                """,
                (run_id,),
            ).fetchall()
            events = self._replay_with_connection(connection, run_id)
            status = self._snapshot_with_connection(connection, run_id)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        receipt_items: list[dict[str, Any]] = []
        for row in receipts:
            try:
                parsed_receipt = json.loads(row["receipt_json"])
                calculated = sha256(_canonical(parsed_receipt).encode("utf-8")).hexdigest()
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise StoreError("stored receipt contains invalid JSON") from exc
            if calculated != row["receipt_hash"]:
                raise StoreError(
                    f"receipt verification failed for {row['task_id']} attempt {row['attempt']}"
                )
            receipt_items.append(
                {
                    "task_id": row["task_id"],
                    "attempt": row["attempt"],
                    "receipt_hash": row["receipt_hash"],
                    "receipt": parsed_receipt,
                    "created_at": row["created_at"],
                }
            )
        exported = {
            "format": "ai-software-factory/export-v1",
            "spec": json.loads(run["spec_json"]),
            "status": status,
            "events": events,
            "event_chain_root": events[-1]["event_hash"] if events else "0" * 64,
            "receipts": receipt_items,
        }
        exported["export_sha256"] = sha256(
            _canonical(exported).encode("utf-8")
        ).hexdigest()
        return exported
