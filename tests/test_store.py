from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from ai_software_factory.evidence import digest_json, verify_export
from ai_software_factory.models import FactorySpec
from ai_software_factory.state import RunState, TaskState
from ai_software_factory.store import (
    FactoryStore,
    IdempotencyConflict,
    LeaseLost,
    StoreError,
)

from tests.support import ManualClock, spec, task


def receipt(attempt: int, marker: str = "ok") -> tuple[dict[str, object], str]:
    value: dict[str, object] = {"attempt": attempt, "marker": marker}
    return value, digest_json(value)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.clock = ManualClock()
        self.database = Path(self.temporary.name) / "factory.sqlite3"
        self.store = FactoryStore(self.database, clock=self.clock)

    def parsed(self, *tasks: dict[str, object], **changes: object) -> FactorySpec:
        return FactorySpec.from_dict(spec(*tasks, **changes))

    def started(self, parsed: FactorySpec | None = None, key: str = "run") -> str:
        run_id = self.store.create_run(parsed or self.parsed(), key)
        self.store.start_run(run_id)
        return run_id

    def complete(
        self,
        claim,
        *,
        succeeded: bool,
        retryable: bool = True,
        marker: str = "ok",
    ) -> TaskState:
        value, digest = receipt(claim.attempt, marker)
        return self.store.complete_task(
            claim,
            succeeded=succeeded,
            receipt=value,
            receipt_hash=digest,
            error=None if succeeded else "synthetic failure",
            retryable=retryable,
            retry_base_seconds=2,
            retry_cap_seconds=10,
        )

    def test_create_run_is_idempotent(self):
        parsed = self.parsed()
        first = self.store.create_run(parsed, "same")
        second = self.store.create_run(parsed, "same")
        self.assertEqual(first, second)
        self.assertEqual(self.store.snapshot(first)["event_count"], 1)

    def test_idempotency_key_rejects_different_spec(self):
        self.store.create_run(self.parsed(), "same")
        with self.assertRaises(IdempotencyConflict):
            self.store.create_run(self.parsed(task("different")), "same")

    def test_initial_root_is_ready_and_dependency_pending(self):
        run_id = self.store.create_run(
            self.parsed(task("root"), task("child", depends_on=["root"])), "dag"
        )
        tasks = {item["task_id"]: item["state"] for item in self.store.snapshot(run_id)["tasks"]}
        self.assertEqual(tasks, {"root": "ready", "child": "pending"})

    def test_atomic_claim_has_one_winner(self):
        run_id = self.started()
        barrier = threading.Barrier(3)
        claims = []

        def claim(worker: str) -> None:
            barrier.wait()
            claims.append(self.store.claim_ready_task(run_id, worker, 10))

        threads = [threading.Thread(target=claim, args=(f"w{index}",)) for index in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(item is not None for item in claims), 1)

    def test_expired_lease_is_reclaimed_with_new_attempt(self):
        run_id = self.started()
        first = self.store.claim_ready_task(run_id, "first", 10)
        self.assertIsNotNone(first)
        self.clock.advance(11)
        second = self.store.claim_ready_task(run_id, "second", 10)
        self.assertIsNotNone(second)
        self.assertEqual(second.attempt, 2)  # type: ignore[union-attr]

    def test_renew_rejects_non_positive_duration(self):
        run_id = self.started()
        claim = self.store.claim_ready_task(run_id, "worker", 10)
        with self.assertRaises(ValueError):
            self.store.renew_lease(claim, 0)  # type: ignore[arg-type]

    def test_completion_at_exact_expiry_loses_lease(self):
        run_id = self.started()
        claim = self.store.claim_ready_task(run_id, "worker", 10)
        self.clock.advance(10)
        with self.assertRaises(LeaseLost):
            self.complete(claim, succeeded=True)  # type: ignore[arg-type]

    def test_expired_lease_never_invokes_fenced_transition_callback(self):
        run_id = self.started(key="expired-callback")
        claim = self.store.claim_ready_task(run_id, "worker", 5)
        self.clock.advance(5)
        invoked: list[bool] = []
        value, digest = receipt(1)
        with self.assertRaises(LeaseLost):
            self.store.complete_task(
                claim,  # type: ignore[arg-type]
                succeeded=True,
                receipt=value,
                receipt_hash=digest,
                error=None,
                retry_base_seconds=0,
                retry_cap_seconds=0,
                before_transition=lambda: invoked.append(True),
            )
        self.assertEqual(invoked, [])

    def test_failure_uses_exponential_retry_time(self):
        run_id = self.started()
        claim = self.store.claim_ready_task(run_id, "worker", 10)
        state = self.complete(claim, succeeded=False)  # type: ignore[arg-type]
        self.assertEqual(state, TaskState.RETRY_WAIT)
        task_status = self.store.snapshot(run_id)["tasks"][0]
        self.assertEqual(task_status["next_attempt_at"], self.clock.value + 2)
        self.clock.advance(2)
        second = self.store.claim_ready_task(run_id, "worker", 10)
        self.assertEqual(second.attempt, 2)  # type: ignore[union-attr]

    def test_non_retryable_failure_is_terminal(self):
        run_id = self.started()
        claim = self.store.claim_ready_task(run_id, "worker", 10)
        state = self.complete(claim, succeeded=False, retryable=False)  # type: ignore[arg-type]
        self.assertEqual(state, TaskState.FAILED)

    def test_failed_dependency_blocks_entire_reversed_chain(self):
        parsed = self.parsed(
            task("d", depends_on=["c"]),
            task("c", depends_on=["b"]),
            task("b", depends_on=["a"]),
            task("a", max_attempts=1),
        )
        run_id = self.started(parsed, "chain")
        claim = self.store.claim_ready_task(run_id, "worker", 10)
        self.complete(claim, succeeded=False)  # type: ignore[arg-type]
        self.assertEqual(self.store.finalize_run(run_id), RunState.FAILED)
        states = {item["state"] for item in self.store.snapshot(run_id)["tasks"]}
        self.assertEqual(states, {"failed", "blocked"})

    def test_kill_switch_cancels_all_nonterminal_tasks(self):
        run_id = self.started(self.parsed(task("a"), task("b")), "kill")
        self.store.claim_ready_task(run_id, "worker", 10)
        self.store.activate_kill_switch(run_id, reason="operator request")
        snapshot = self.store.snapshot(run_id)
        self.assertEqual(snapshot["state"], "cancelled")
        self.assertTrue(snapshot["kill_switch"])
        self.assertEqual({item["state"] for item in snapshot["tasks"]}, {"cancelled"})

    def test_global_attempt_budget_activates_kill_switch(self):
        parsed = self.parsed(
            task("a"),
            task("b"),
            budget={"max_tasks": 2, "max_attempts": 1},
        )
        run_id = self.started(parsed, "attempt-budget")
        claim = self.store.claim_ready_task(run_id, "worker", 10)
        self.complete(claim, succeeded=True)  # type: ignore[arg-type]
        self.assertIsNone(self.store.claim_ready_task(run_id, "worker", 10))
        self.assertEqual(self.store.snapshot(run_id)["state"], "cancelled")

    def test_wall_budget_activates_kill_switch_before_claim(self):
        parsed = self.parsed(
            budget={"max_tasks": 1, "max_attempts": 2, "max_wall_seconds": 5}
        )
        run_id = self.started(parsed, "wall-budget")
        self.clock.advance(5)
        self.assertIsNone(self.store.claim_ready_task(run_id, "worker", 10))
        self.assertEqual(self.store.snapshot(run_id)["state"], "cancelled")

    def test_receipt_completion_is_idempotent_after_commit(self):
        run_id = self.started()
        claim = self.store.claim_ready_task(run_id, "worker", 10)
        self.assertEqual(self.complete(claim, succeeded=True), TaskState.SUCCEEDED)  # type: ignore[arg-type]
        self.assertEqual(self.complete(claim, succeeded=True), TaskState.SUCCEEDED)  # type: ignore[arg-type]
        self.assertEqual(self.store.snapshot(run_id)["receipt_count"], 1)

    def test_receipt_retry_with_changed_content_conflicts(self):
        run_id = self.started()
        claim = self.store.claim_ready_task(run_id, "worker", 10)
        self.complete(claim, succeeded=True)  # type: ignore[arg-type]
        with self.assertRaises(IdempotencyConflict):
            self.complete(claim, succeeded=True, marker="changed")  # type: ignore[arg-type]

    def test_event_payload_tampering_is_detected(self):
        run_id = self.store.create_run(self.parsed(), "tamper")
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE events SET payload_json='{}' WHERE run_id=?", (run_id,))
        with self.assertRaisesRegex(StoreError, "event chain"):
            self.store.replay(run_id)

    def test_event_deletion_is_detected_by_anchor(self):
        run_id = self.store.create_run(self.parsed(), "delete")
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("DELETE FROM events WHERE run_id=?", (run_id,))
        with self.assertRaisesRegex(StoreError, "anchor"):
            self.store.replay(run_id)

    def test_export_contains_verified_chain_and_receipts(self):
        run_id = self.started()
        claim = self.store.claim_ready_task(run_id, "worker", 10)
        self.complete(claim, succeeded=True)  # type: ignore[arg-type]
        self.store.finalize_run(run_id)
        exported = self.store.export(run_id)
        self.assertEqual(exported["event_chain_root"], exported["events"][-1]["event_hash"])
        self.assertEqual(len(exported["receipts"]), 1)

    def test_receipt_tampering_is_detected_on_export(self):
        run_id = self.started()
        claim = self.store.claim_ready_task(run_id, "worker", 10)
        self.complete(claim, succeeded=True)  # type: ignore[arg-type]
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                "UPDATE receipts SET receipt_json='{}' WHERE run_id=?", (run_id,)
            )
        with self.assertRaisesRegex(StoreError, "receipt verification"):
            self.store.export(run_id)

    def test_export_uses_one_snapshot_during_concurrent_completion(self):
        run_id = self.started(key="export-snapshot")
        claim = self.store.claim_ready_task(run_id, "worker", 10)
        failures: list[BaseException] = []

        class RacingStore(FactoryStore):
            pending_claim = claim

            def _replay_with_connection(inner, connection, selected_run_id):
                if inner.pending_claim is not None:
                    selected_claim = inner.pending_claim
                    inner.pending_claim = None

                    def complete_in_writer() -> None:
                        try:
                            value, digest = receipt(selected_claim.attempt)
                            inner.complete_task(
                                selected_claim,
                                succeeded=True,
                                receipt=value,
                                receipt_hash=digest,
                                error=None,
                                retry_base_seconds=0,
                                retry_cap_seconds=0,
                            )
                        except BaseException as exc:
                            failures.append(exc)

                    writer = threading.Thread(target=complete_in_writer)
                    writer.start()
                    writer.join(timeout=5)
                    if writer.is_alive():
                        raise AssertionError("concurrent writer did not finish")
                    if failures:
                        raise failures[0]
                return super()._replay_with_connection(
                    connection, selected_run_id
                )

        racing = RacingStore(self.database, clock=self.clock)
        exported = racing.export(run_id)
        self.assertEqual(exported["status"]["receipt_count"], 0)
        self.assertEqual(exported["receipts"], [])
        self.assertEqual(racing.snapshot(run_id)["receipt_count"], 1)
        valid, issues = verify_export(exported)
        self.assertTrue(valid, issues)

        exported["status"]["receipt_count"] = 1
        material = {
            key: value for key, value in exported.items() if key != "export_sha256"
        }
        exported["export_sha256"] = digest_json(material)
        valid, issues = verify_export(exported)
        self.assertFalse(valid)
        self.assertIn("receipt count mismatch", issues)

    def test_open_existing_does_not_create_missing_database(self):
        missing = Path(self.temporary.name) / "missing.sqlite3"
        with self.assertRaises(FileNotFoundError):
            FactoryStore(missing, create=False)
        self.assertFalse(missing.exists())

    def test_unversioned_nonempty_database_is_rejected(self):
        legacy = Path(self.temporary.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy)) as connection, connection:
            connection.execute("CREATE TABLE old(value TEXT)")
        with self.assertRaisesRegex(StoreError, "unversioned"):
            FactoryStore(legacy)


if __name__ == "__main__":
    unittest.main()
