from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_software_factory.engine import FactoryEngine, _publish_changes
from ai_software_factory.evidence import digest_json, workspace_snapshot
from ai_software_factory.executors import (
    DeterministicMockExecutor,
    ExecutionRequest,
)
from ai_software_factory.models import FactorySpec
from ai_software_factory.state import RunState
from ai_software_factory.store import FactoryStore, StoreError

from tests.support import ManualClock, result, spec, task


class RaisingExecutor:
    name = "raising-test"

    def execute(self, request):
        raise FileNotFoundError("synthetic missing executable")


class IsolationProbeExecutor:
    name = "isolation-probe"

    def __init__(self):
        self.calls = 0

    def execute(self, request):
        self.calls += 1
        marker = request.cwd / "owned.txt"
        if self.calls == 1:
            marker.write_text("failed attempt", encoding="utf-8")
            return result(1, executor=self.name)
        if marker.exists():
            return result(9, stderr=b"dirty retry workspace", executor=self.name)
        marker.write_text("clean retry", encoding="utf-8")
        return result(0, executor=self.name)


class CrashExecutor:
    name = "crash-probe"

    def execute(self, request):
        raise KeyboardInterrupt("synthetic worker death")


class AdvancingExecutor:
    name = "advancing-clock"

    def __init__(self, clock: ManualClock):
        self.clock = clock
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        if ":test:" in request.label:
            self.clock.advance(request.timeout_seconds)
            return result(-9, timed_out=True, executor=self.name)
        self.clock.advance(0.04)
        return result(0, executor=self.name)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = FactoryStore(self.root / "state.sqlite3")

    def run_with(self, raw, executor, key="run"):
        parsed = FactorySpec.from_dict(raw)
        engine = FactoryEngine(
            self.store,
            base_directory=self.root,
            executor=executor,
            sleeper=lambda _: None,
        )
        run_id = engine.plan(parsed, idempotency_key=key)
        return run_id, engine.run(run_id)

    def test_successful_dag_runs_in_dependency_order(self):
        raw = spec(task("first"), task("second", depends_on=["first"]))
        run_id, actual = self.run_with(raw, DeterministicMockExecutor())
        self.assertEqual(actual.state, RunState.SUCCEEDED)
        self.assertEqual(actual.tasks_succeeded, 2)
        claims = [
            event["task_id"]
            for event in self.store.replay(run_id)
            if event["event_type"] == "task.transition"
            and event["payload"]["to"] == "running"
        ]
        self.assertEqual(claims, ["first", "second"])

    def test_retry_then_success_produces_two_receipts(self):
        mock = DeterministicMockExecutor({"build": [result(1), result(0)]})
        run_id, actual = self.run_with(spec(task("build")), mock)
        self.assertEqual(actual.state, RunState.SUCCEEDED)
        snapshot = self.store.snapshot(run_id)
        self.assertEqual(snapshot["tasks"][0]["attempts"], 2)
        self.assertEqual(snapshot["receipt_count"], 2)

    def test_terminal_failure_blocks_dependents(self):
        raw = spec(
            task("build", max_attempts=1),
            task("publish", depends_on=["build"]),
        )
        run_id, actual = self.run_with(
            raw, DeterministicMockExecutor({"build": [result(1)]})
        )
        self.assertEqual(actual.state, RunState.FAILED)
        states = {item["task_id"]: item["state"] for item in self.store.snapshot(run_id)["tasks"]}
        self.assertEqual(states, {"build": "failed", "publish": "blocked"})

    def test_missing_artifact_fails_closed(self):
        raw = spec(task("build", artifacts=["missing.txt"], max_attempts=1))
        _, actual = self.run_with(raw, DeterministicMockExecutor())
        self.assertEqual(actual.state, RunState.FAILED)

    def test_failed_test_is_recorded(self):
        raw = spec(
            task(
                "build",
                tests=[{"name": "unit", "command": ["python", "-c", "pass"]}],
                max_attempts=1,
            )
        )
        mock = DeterministicMockExecutor({"build:test:unit": [result(2)]})
        run_id, actual = self.run_with(raw, mock)
        self.assertEqual(actual.state, RunState.FAILED)
        receipt = self.store.export(run_id)["receipts"][0]["receipt"]
        self.assertEqual(receipt["tests"][0]["name"], "unit")
        self.assertEqual(receipt["tests"][0]["exit_code"], 2)

    def test_main_failure_marks_expected_tests_not_run(self):
        raw = spec(
            task(
                "build",
                tests=[{"name": "unit", "command": ["python", "-c", "pass"]}],
                max_attempts=1,
            )
        )
        run_id, _ = self.run_with(
            raw, DeterministicMockExecutor({"build": [result(1)]})
        )
        receipt = self.store.export(run_id)["receipts"][0]["receipt"]
        self.assertEqual(receipt["tests"], [{"name": "unit", "status": "not_run"}])

    def test_executor_exception_becomes_terminal_evidence(self):
        run_id, actual = self.run_with(
            spec(task("build", max_attempts=1)), RaisingExecutor()
        )
        self.assertEqual(actual.state, RunState.FAILED)
        snapshot = self.store.snapshot(run_id)
        self.assertEqual(snapshot["tasks"][0]["state"], "failed")
        self.assertEqual(snapshot["receipt_count"], 1)

    def test_ownership_violation_is_non_retryable(self):
        raw = spec(
            task(
                "build",
                command=[
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('forbidden.txt').write_text('x')",
                ],
                owned_paths=["allowed.txt"],
                max_attempts=2,
            )
        )
        run_id, actual = self.run_with(raw, None, key="ownership")
        self.assertEqual(actual.state, RunState.FAILED)
        item = self.store.snapshot(run_id)["tasks"][0]
        self.assertEqual(item["attempts"], 1)
        receipt = self.store.export(run_id)["receipts"][0]["receipt"]
        self.assertEqual(receipt["ownership"]["violations"], ["forbidden.txt"])
        self.assertFalse((self.root / "workspace" / "forbidden.txt").exists())

    def test_retry_starts_from_clean_canonical_workspace(self):
        probe = IsolationProbeExecutor()
        run_id, actual = self.run_with(
            spec(task("build", owned_paths=["owned.txt"], max_attempts=2)),
            probe,
            key="isolated-retry",
        )
        self.assertEqual(actual.state, RunState.SUCCEEDED)
        self.assertEqual(self.store.snapshot(run_id)["tasks"][0]["attempts"], 2)
        self.assertEqual(
            (self.root / "workspace" / "owned.txt").read_text(encoding="utf-8"),
            "clean retry",
        )

    def test_resume_of_terminal_idempotent_run_is_noop(self):
        parsed = FactorySpec.from_dict(spec())
        engine = FactoryEngine(
            self.store,
            base_directory=self.root,
            executor=DeterministicMockExecutor(),
        )
        run_id = engine.plan(parsed, idempotency_key="same")
        first = engine.run(run_id)
        second = engine.run(engine.plan(parsed, idempotency_key="same"))
        self.assertEqual(first, second)
        self.assertEqual(self.store.snapshot(run_id)["receipt_count"], 1)

    def test_publish_revalidates_captured_file_digest(self):
        canonical = self.root / "canonical"
        attempt = self.root / "attempt"
        canonical.mkdir()
        attempt.mkdir()
        (canonical / "owned.txt").write_text("baseline", encoding="utf-8")
        source = attempt / "owned.txt"
        source.write_text("verified", encoding="utf-8")
        captured = workspace_snapshot(attempt)
        source.write_text("changed-after-evidence", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed after evidence capture"):
            _publish_changes(canonical, attempt, ["owned.txt"], captured)
        self.assertEqual(
            (canonical / "owned.txt").read_text(encoding="utf-8"),
            "baseline",
        )

    def test_worker_death_is_recoverable_after_configured_lease(self):
        clock = ManualClock()
        store = FactoryStore(self.root / "crash.sqlite3", clock=clock)
        parsed = FactorySpec.from_dict(
            spec(
                task("build", max_attempts=2),
                budget={
                    "max_wall_seconds": 20,
                    "lease_seconds": 1,
                    "retry_base_seconds": 0,
                    "retry_cap_seconds": 0,
                },
            )
        )
        engine = FactoryEngine(
            store,
            base_directory=self.root,
            executor=CrashExecutor(),
            clock=clock,
            sleeper=lambda _: None,
        )
        run_id = engine.plan(parsed, idempotency_key="crash-recovery")
        with self.assertRaises(KeyboardInterrupt):
            engine.run(run_id)
        clock.advance(1.1)
        recovered = store.claim_ready_task(run_id, "recovery-worker", 1)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.attempt, 2)  # type: ignore[union-attr]
        self.assertEqual(store.snapshot(run_id)["state"], "running")

    def test_run_wall_budget_caps_each_test_and_cancels_at_deadline(self):
        clock = ManualClock()
        store = FactoryStore(self.root / "wall.sqlite3", clock=clock)
        executor = AdvancingExecutor(clock)
        parsed = FactorySpec.from_dict(
            spec(
                task(
                    "build",
                    tests=[
                        {
                            "name": "slow",
                            "command": [sys.executable, "-c", "pass"],
                            "timeout_seconds": 1,
                        }
                    ],
                ),
                budget={"max_wall_seconds": 0.1, "lease_seconds": 1},
            )
        )
        engine = FactoryEngine(
            store,
            base_directory=self.root,
            executor=executor,
            clock=clock,
            sleeper=lambda _: None,
        )
        run_id = engine.plan(parsed, idempotency_key="wall-tests")
        actual = engine.run(run_id)
        self.assertEqual(actual.state, RunState.CANCELLED)
        self.assertEqual(len(executor.requests), 2)
        self.assertAlmostEqual(executor.requests[1].timeout_seconds, 0.06, places=6)

    def test_provider_cannot_execute_outside_attempt_workspace(self):
        outside = self.root / "outside.txt"

        class EscapingProvider:
            def task_request(inner_self, parsed, parsed_task, workspace):
                return ExecutionRequest(
                    parsed_task.id,
                    (
                        sys.executable,
                        "-c",
                        "from pathlib import Path; Path('outside.txt').write_text('bad')",
                    ),
                    self.root,
                    2,
                    4096,
                )

            def test_request(inner_self, parsed, parsed_task, test, workspace):
                raise AssertionError("no tests expected")

        parsed = FactorySpec.from_dict(spec(task("build", max_attempts=1)))
        engine = FactoryEngine(
            self.store,
            base_directory=self.root,
            provider=EscapingProvider(),
        )
        run_id = engine.plan(parsed, idempotency_key="provider-confinement")
        actual = engine.run(run_id)
        self.assertEqual(actual.state, RunState.FAILED)
        self.assertFalse(outside.exists())

    def test_multi_file_publish_rolls_back_prior_replacements(self):
        canonical = self.root / "canonical-rollback"
        attempt = self.root / "attempt-rollback"
        canonical.mkdir()
        attempt.mkdir()
        for name in ("a.txt", "b.txt"):
            (canonical / name).write_text(f"old-{name}", encoding="utf-8")
            (attempt / name).write_text(f"new-{name}", encoding="utf-8")
        after = workspace_snapshot(attempt)
        real_replace = os.replace
        calls = 0

        def fail_second(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic second replacement failure")
            return real_replace(source, target)

        with patch("ai_software_factory.engine.os.replace", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "second replacement"):
                _publish_changes(
                    canonical, attempt, ["a.txt", "b.txt"], after
                )
        self.assertEqual((canonical / "a.txt").read_text(), "old-a.txt")
        self.assertEqual((canonical / "b.txt").read_text(), "old-b.txt")

    def test_database_failure_compensates_published_files(self):
        parsed = FactorySpec.from_dict(spec(task("build")))
        run_id = self.store.create_run(parsed, "database-compensation")
        self.store.start_run(run_id)
        claim = self.store.claim_ready_task(run_id, "worker", 10)
        canonical = self.root / "canonical-database"
        attempt = self.root / "attempt-database"
        canonical.mkdir()
        attempt.mkdir()
        (canonical / "owned.txt").write_text("old", encoding="utf-8")
        (attempt / "owned.txt").write_text("new", encoding="utf-8")
        after = workspace_snapshot(attempt)
        receipt = {"attempt": 1, "marker": "synthetic"}
        with patch.object(
            self.store,
            "_transition_task",
            side_effect=StoreError("synthetic transition failure"),
        ):
            with self.assertRaisesRegex(StoreError, "transition failure"):
                self.store.complete_task(
                    claim,  # type: ignore[arg-type]
                    succeeded=True,
                    receipt=receipt,
                    receipt_hash=digest_json(receipt),
                    error=None,
                    retry_base_seconds=0,
                    retry_cap_seconds=0,
                    before_transition=lambda: _publish_changes(
                        canonical, attempt, ["owned.txt"], after
                    ),
                )
        self.assertEqual((canonical / "owned.txt").read_text(), "old")
        self.assertEqual(self.store.snapshot(run_id)["receipt_count"], 0)


if __name__ == "__main__":
    unittest.main()
