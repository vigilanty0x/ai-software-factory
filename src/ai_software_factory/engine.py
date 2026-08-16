"""Local orchestration engine for validated software-factory DAGs."""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
import math
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
import time
from typing import Callable

from .evidence import (
    artifact_manifest,
    build_receipt,
    ownership_report,
    workspace_snapshot,
)
from .executors import (
    ExecutionRequest,
    ExecutionResult,
    Executor,
    Provider,
    SpecProvider,
    SubprocessExecutor,
)
from .models import FactorySpec
from .state import RunState, TaskState
from .store import ClaimedTask, CompletionEffect, FactoryStore, LeaseLost


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    state: RunState
    tasks_succeeded: int
    tasks_failed: int
    tasks_blocked: int
    tasks_cancelled: int


class PublishError(RuntimeError):
    """A fenced filesystem publication failed before task completion."""


class _LeaseHeartbeat:
    """Keep a live attempt fenced without delaying recovery after worker death."""

    def __init__(
        self,
        store: FactoryStore,
        claim: ClaimedTask,
        lease_seconds: float,
    ):
        self.store = store
        self.claim = claim
        self.lease_seconds = lease_seconds
        self.interval = max(0.02, min(1.0, lease_seconds / 3.0))
        self._stop = threading.Event()
        self._error: Exception | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"factory-lease-{claim.task_id}-{claim.attempt}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.claim = self.store.renew_lease(self.claim, self.lease_seconds)
            except Exception as exc:
                self._error = exc
                self._stop.set()
                return

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()

    def ensure_owned(self) -> None:
        if self._error is not None:
            raise LeaseLost(
                f"lease heartbeat failed for {self.claim.task_id!r}"
            ) from self._error


def resolve_workspace(spec: FactorySpec, base_directory: str | Path) -> Path:
    base = Path(base_directory).resolve()
    workspace = (base / spec.workspace).resolve()
    try:
        workspace.relative_to(base)
    except ValueError as exc:  # defensive: parser already rejects traversal
        raise ValueError("workspace escapes the specification directory") from exc
    return workspace


def _failure_result(
    exc: Exception,
    *,
    executor_name: str,
    started_at: float,
    clock: Callable[[], float],
    boundary: str,
) -> ExecutionResult:
    diagnostic = f"{type(exc).__name__}: {boundary} failed".encode()
    return ExecutionResult(
        exit_code=None,
        stdout=b"",
        stderr=diagnostic,
        stdout_bytes_seen=0,
        stderr_bytes_seen=len(diagnostic),
        stdout_sha256="sha256:" + sha256(b"").hexdigest(),
        stderr_sha256="sha256:" + sha256(diagnostic).hexdigest(),
        output_truncated=False,
        timed_out=False,
        duration_seconds=max(0.0, clock() - started_at),
        executor=executor_name,
    )


@dataclass(slots=True)
class _StagedChange:
    target: Path
    staged: Path | None
    backup: Path | None
    existed: bool
    applied: bool = False


def _lexists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _copy_regular_file(
    source: Path,
    destination: Path,
    deadline_exhausted: Callable[[], bool] | None = None,
) -> os.stat_result:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    source_descriptor = os.open(source, flags)
    try:
        with os.fdopen(source_descriptor, "rb") as source_stream, destination.open(
            "wb"
        ) as destination_stream:
            source_state = os.fstat(source_stream.fileno())
            if not stat.S_ISREG(source_state.st_mode):
                raise ValueError(f"publish source is not a regular file: {source}")
            while chunk := source_stream.read(1024 * 1024):
                if deadline_exhausted is not None and deadline_exhausted():
                    raise TimeoutError("run wall-clock budget exhausted during publish")
                destination_stream.write(chunk)
            destination_stream.flush()
            os.fsync(destination_stream.fileno())
        os.chmod(destination, stat.S_IMODE(source_state.st_mode))
        return source_state
    except BaseException:
        try:
            os.close(source_descriptor)
        except OSError:
            pass
        raise


def _temporary_path(parent: Path, prefix: str) -> Path:
    descriptor, value = tempfile.mkstemp(prefix=prefix, dir=parent)
    os.close(descriptor)
    return Path(value)


def _ensure_publish_parent(
    parent: Path, root: Path, created_directories: list[Path]
) -> None:
    missing: list[Path] = []
    cursor = parent
    while not _lexists(cursor):
        missing.append(cursor)
        cursor = cursor.parent
    try:
        cursor.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise ValueError("publish target parent escapes canonical workspace") from exc
    for directory in reversed(missing):
        directory.mkdir()
        created_directories.append(directory)
    try:
        parent.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise ValueError("publish target parent escapes canonical workspace") from exc


def _backup_target(
    target: Path, deadline_exhausted: Callable[[], bool] | None = None
) -> tuple[Path | None, bool]:
    if target.is_symlink():
        backup = _temporary_path(target.parent, f".{target.name}.factory-backup-")
        backup.unlink()
        os.symlink(
            os.readlink(target),
            backup,
            target_is_directory=target.is_dir(),
        )
        return backup, True
    if not target.exists():
        return None, False
    state = os.lstat(target)
    if not stat.S_ISREG(state.st_mode):
        raise ValueError(f"publish target is not a regular file: {target}")
    backup = _temporary_path(target.parent, f".{target.name}.factory-backup-")
    try:
        _copy_regular_file(target, backup, deadline_exhausted)
    except BaseException:
        backup.unlink(missing_ok=True)
        raise
    return backup, True


def _publish_changes(
    canonical_workspace: Path,
    attempt_workspace: Path,
    changed_paths: list[str],
    after: dict[str, dict[str, object]],
    deadline_exhausted: Callable[[], bool] | None = None,
) -> CompletionEffect:
    """Publish regular-file changes from an isolated attempt.

    All new contents and backups are prepared before the first replacement.
    Handled I/O or database failures restore already-replaced targets through
    the returned compensating effect.
    """

    resolved_canonical = canonical_workspace.resolve()
    resolved_attempt = attempt_workspace.resolve()
    changes: list[_StagedChange] = []
    created_directories: list[Path] = []

    def cleanup() -> None:
        for change in changes:
            for path in (change.staged, change.backup):
                if path is not None:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass

    def remove_created_directories() -> None:
        for directory in reversed(created_directories):
            try:
                directory.rmdir()
            except OSError:
                pass

    def rollback() -> None:
        failures: list[str] = []
        for change in reversed(changes):
            if not change.applied:
                continue
            try:
                if change.existed:
                    if change.backup is None:
                        raise OSError("missing publication backup")
                    os.replace(change.backup, change.target)
                    change.backup = None
                elif _lexists(change.target):
                    change.target.unlink()
                change.applied = False
            except OSError:
                failures.append(str(change.target))
        remove_created_directories()
        if failures:
            raise PublishError(
                "could not roll back published path(s): " + ", ".join(failures)
            )

    try:
        for relative in changed_paths:
            if deadline_exhausted is not None and deadline_exhausted():
                raise TimeoutError("run wall-clock budget exhausted during publish")
            target = canonical_workspace / relative
            source = attempt_workspace / relative
            state = after.get(relative)
            try:
                target.parent.resolve().relative_to(resolved_canonical)
            except ValueError as exc:
                raise ValueError(
                    f"publish target escapes canonical workspace: {relative}"
                ) from exc
            _ensure_publish_parent(
                target.parent, resolved_canonical, created_directories
            )
            backup, existed = _backup_target(target, deadline_exhausted)
            change = _StagedChange(target, None, backup, existed)
            changes.append(change)
            if state is not None:
                if state.get("kind") != "file":
                    raise ValueError(
                        f"cannot publish unsafe workspace entry: {relative}"
                    )
                expected_size = state.get("size")
                expected_digest = state.get("sha256")
                if (
                    isinstance(expected_size, bool)
                    or not isinstance(expected_size, int)
                    or expected_size < 0
                    or not isinstance(expected_digest, str)
                    or len(expected_digest) != 71
                    or not expected_digest.startswith("sha256:")
                    or any(
                        character not in "0123456789abcdef"
                        for character in expected_digest[7:]
                    )
                ):
                    raise ValueError(
                        f"cannot publish invalid workspace evidence: {relative}"
                    )
                try:
                    source.resolve(strict=True).relative_to(resolved_attempt)
                except (OSError, ValueError) as exc:
                    raise ValueError(
                        f"publish source escapes attempt workspace: {relative}"
                    ) from exc
                staged = _temporary_path(
                    target.parent, f".{target.name}.factory-stage-"
                )
                change.staged = staged
                digest = sha256()
                copied = 0
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                source_descriptor = os.open(source, flags)
                try:
                    with os.fdopen(source_descriptor, "rb") as source_stream, staged.open(
                        "wb"
                    ) as target_stream:
                        source_state = os.fstat(source_stream.fileno())
                        if not stat.S_ISREG(source_state.st_mode):
                            raise ValueError(
                                f"publish source is not a regular file: {relative}"
                            )
                        while chunk := source_stream.read(1024 * 1024):
                            if (
                                deadline_exhausted is not None
                                and deadline_exhausted()
                            ):
                                raise TimeoutError(
                                    "run wall-clock budget exhausted during publish"
                                )
                            digest.update(chunk)
                            copied += len(chunk)
                            target_stream.write(chunk)
                        target_stream.flush()
                        os.fsync(target_stream.fileno())
                except BaseException:
                    try:
                        os.close(source_descriptor)
                    except OSError:
                        pass
                    raise
                actual_digest = "sha256:" + digest.hexdigest()
                if copied != expected_size or actual_digest != expected_digest:
                    raise ValueError(
                        f"publish source changed after evidence capture: {relative}"
                    )
                os.chmod(staged, stat.S_IMODE(source_state.st_mode))

        for change in changes:
            if deadline_exhausted is not None and deadline_exhausted():
                raise TimeoutError("run wall-clock budget exhausted during publish")
            if change.staged is None:
                if _lexists(change.target):
                    change.target.unlink()
            else:
                os.replace(change.staged, change.target)
                change.staged = None
            change.applied = True
    except BaseException:
        try:
            rollback()
        finally:
            cleanup()
            remove_created_directories()
        raise

    return CompletionEffect(rollback=rollback, finalize=cleanup)


def _remaining_wall_seconds(
    spec: FactorySpec, run_started_at: float, clock: Callable[[], float]
) -> float:
    elapsed = max(0.0, clock() - run_started_at)
    return spec.budget.max_wall_seconds - elapsed


def _confine_request(
    request: ExecutionRequest,
    *,
    workspace: Path,
    timeout_cap: float,
    remaining_wall: float,
    output_cap: int,
) -> ExecutionRequest:
    """Validate a provider request against engine-owned policy boundaries."""

    if not isinstance(request, ExecutionRequest):
        raise TypeError("provider must return an ExecutionRequest")
    cwd = Path(request.cwd).resolve()
    root = workspace.resolve()
    try:
        cwd.relative_to(root)
    except ValueError as exc:
        raise ValueError("provider request cwd escapes the attempt workspace") from exc
    if (
        isinstance(request.timeout_seconds, bool)
        or not isinstance(request.timeout_seconds, (int, float))
        or not math.isfinite(float(request.timeout_seconds))
        or request.timeout_seconds <= 0
    ):
        raise ValueError("provider request timeout must be a positive finite number")
    if (
        isinstance(request.max_output_bytes, bool)
        or not isinstance(request.max_output_bytes, int)
        or not 0 < request.max_output_bytes <= output_cap
    ):
        raise ValueError("provider request output cap exceeds the factory policy")
    effective_timeout = min(
        float(request.timeout_seconds), float(timeout_cap), remaining_wall
    )
    if effective_timeout <= 0:
        raise ValueError("run wall-clock budget is exhausted")
    return replace(request, cwd=cwd, timeout_seconds=effective_timeout)


class FactoryEngine:
    def __init__(
        self,
        store: FactoryStore,
        *,
        base_directory: str | Path,
        provider: Provider | None = None,
        executor: Executor | None = None,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.store = store
        self.base_directory = Path(base_directory).resolve()
        self.provider = provider or SpecProvider()
        self.clock = clock
        self.sleeper = sleeper
        # A default subprocess executor is rooted at the broad base directory;
        # each request still uses the narrower validated spec workspace.
        self.executor = executor or SubprocessExecutor(self.base_directory)

    def plan(self, spec: FactorySpec, *, idempotency_key: str | None = None) -> str:
        resolve_workspace(spec, self.base_directory).mkdir(parents=True, exist_ok=True)
        return self.store.create_run(spec, idempotency_key)

    def execute_one(self, run_id: str, worker_id: str) -> bool:
        spec = self.store.load_spec(run_id)
        canonical_workspace = resolve_workspace(spec, self.base_directory)
        claim = self.store.claim_ready_task(
            run_id, worker_id, spec.budget.lease_seconds
        )
        if claim is None:
            return False
        task = spec.task(claim.task_id)
        heartbeat = _LeaseHeartbeat(
            self.store, claim, spec.budget.lease_seconds
        )
        heartbeat.start()
        try:
            attempts_root = self.base_directory / ".factory-attempts"
            attempts_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix=f"{task.id}-{claim.attempt}-", dir=attempts_root
            ) as temporary:
                workspace = Path(temporary) / "workspace"
                shutil.copytree(canonical_workspace, workspace, symlinks=True)
                before = workspace_snapshot(workspace)
                status = self.store.snapshot(run_id)
                run_started_at = (
                    self.clock()
                    if status["started_at"] is None
                    else float(status["started_at"])
                )

                def remaining_wall() -> float:
                    return _remaining_wall_seconds(
                        spec, run_started_at, self.clock
                    )

                def cancel_if_wall_exhausted() -> bool:
                    if remaining_wall() > 0:
                        return False
                    self.store.activate_kill_switch(
                        run_id, reason="wall-clock budget exhausted"
                    )
                    return True

                if cancel_if_wall_exhausted():
                    return False
                task_timeout = (
                    task.timeout_seconds
                    or spec.budget.default_task_timeout_seconds
                )
                try:
                    request = _confine_request(
                        self.provider.task_request(spec, task, workspace),
                        workspace=workspace,
                        timeout_cap=task_timeout,
                        remaining_wall=remaining_wall(),
                        output_cap=spec.budget.max_output_bytes,
                    )
                except Exception as exc:
                    if cancel_if_wall_exhausted():
                        return False
                    request = _confine_request(
                        ExecutionRequest(
                            label=task.id,
                            argv=task.command,
                            cwd=workspace,
                            timeout_seconds=task_timeout,
                            max_output_bytes=spec.budget.max_output_bytes,
                            environment=task.environment,
                        ),
                        workspace=workspace,
                        timeout_cap=task_timeout,
                        remaining_wall=remaining_wall(),
                        output_cap=spec.budget.max_output_bytes,
                    )
                    provider_error: Exception | None = exc
                else:
                    provider_error = None
                started_at = self.clock()
                tests: list[tuple[str, ExecutionRequest, ExecutionResult]] = []
                try:
                    if provider_error is not None:
                        raise provider_error
                    result = self.executor.execute(request)
                    if result.succeeded:
                        for test in task.tests:
                            if cancel_if_wall_exhausted():
                                return False
                            test_timeout = (
                                test.timeout_seconds
                                or task.timeout_seconds
                                or spec.budget.default_task_timeout_seconds
                            )
                            test_request = _confine_request(
                                self.provider.test_request(
                                    spec, task, test, workspace
                                ),
                                workspace=workspace,
                                timeout_cap=test_timeout,
                                remaining_wall=remaining_wall(),
                                output_cap=spec.budget.max_output_bytes,
                            )
                            test_result = self.executor.execute(test_request)
                            tests.append((test.name, test_request, test_result))
                            if not test_result.succeeded:
                                break
                except Exception as exc:
                    result = _failure_result(
                        exc,
                        executor_name=getattr(
                            self.executor, "name", type(self.executor).__name__
                        ),
                        started_at=started_at,
                        clock=self.clock,
                        boundary="execution boundary",
                    )
                    tests = []
                if cancel_if_wall_exhausted():
                    return False
                try:
                    artifacts = artifact_manifest(workspace, task.artifacts)
                    after = workspace_snapshot(workspace)
                    ownership = ownership_report(
                        before,
                        after,
                        tuple(dict.fromkeys(task.owned_paths + task.artifacts)),
                    )
                except Exception as exc:
                    result = _failure_result(
                        exc,
                        executor_name=getattr(
                            self.executor, "name", type(self.executor).__name__
                        ),
                        started_at=started_at,
                        clock=self.clock,
                        boundary="evidence collection",
                    )
                    artifacts = [
                        {"path": path, "exists": False}
                        for path in task.artifacts
                    ]
                    after = before
                    ownership = {
                        "changed_paths": [],
                        "violations": ["<evidence-collection>"],
                        "unsafe_paths": [],
                    }
                if cancel_if_wall_exhausted():
                    return False

                can_publish = (
                    result.succeeded
                    and len(tests) == len(task.tests)
                    and all(item[2].succeeded for item in tests)
                    and all(
                        item.get("exists") and item.get("kind") == "file"
                        for item in artifacts
                    )
                    and not ownership["violations"]
                )
                finished_at = self.clock()
                spec_hash = sha256(
                    spec.canonical_json().encode("utf-8")
                ).hexdigest()
                receipt, receipt_hash = build_receipt(
                    run_id=run_id,
                    task_id=task.id,
                    attempt=claim.attempt,
                    started_at=started_at,
                    finished_at=finished_at,
                    request=request,
                    result=result,
                    tests=tests,
                    artifacts=artifacts,
                    owner=task.owner,
                    spec_hash=spec_hash,
                    expected_tests=tuple(test.name for test in task.tests),
                    ownership=ownership,
                )
                succeeded = receipt["outcome"] == "succeeded"
                if not result.succeeded:
                    error = (
                        "task timed out"
                        if result.timed_out
                        else f"task exited with code {result.exit_code}"
                    )
                elif any(not item[2].succeeded for item in tests):
                    failed_test = next(
                        item[0] for item in tests if not item[2].succeeded
                    )
                    error = f"test failed: {failed_test}"
                elif any(not item.get("exists") for item in artifacts):
                    error = "one or more declared artifacts are missing"
                elif any(item.get("kind") != "file" for item in artifacts):
                    error = "one or more declared artifacts are not regular files"
                elif ownership["violations"]:
                    error = "task modified paths outside its declared ownership"
                else:
                    error = None

                if cancel_if_wall_exhausted():
                    return False

                def publish() -> CompletionEffect:
                    try:
                        return _publish_changes(
                            canonical_workspace,
                            workspace,
                            ownership["changed_paths"],
                            after,
                            deadline_exhausted=lambda: remaining_wall() <= 0,
                        )
                    except Exception as exc:
                        raise PublishError(
                            "verified workspace publication failed"
                        ) from exc

                heartbeat.stop()
                try:
                    heartbeat.ensure_owned()
                    claim = self.store.renew_lease(
                        claim, spec.budget.lease_seconds
                    )
                except LeaseLost:
                    return False
                try:
                    self.store.complete_task(
                        claim,
                        succeeded=succeeded,
                        receipt=receipt,
                        receipt_hash=receipt_hash,
                        error=error,
                        retryable=not bool(ownership["violations"]),
                        retry_base_seconds=spec.budget.retry_base_seconds,
                        retry_cap_seconds=spec.budget.retry_cap_seconds,
                        before_transition=publish if can_publish else None,
                    )
                except LeaseLost:
                    # Another worker recovered the expired lease or the kill
                    # switch cancelled it.  Stale work never publishes.
                    return False
                except PublishError as exc:
                    if cancel_if_wall_exhausted():
                        return False
                    result = _failure_result(
                        exc,
                        executor_name=getattr(
                            self.executor, "name", type(self.executor).__name__
                        ),
                        started_at=started_at,
                        clock=self.clock,
                        boundary="atomic publish",
                    )
                    ownership["violations"] = ["<publish>"]
                    finished_at = self.clock()
                    receipt, receipt_hash = build_receipt(
                        run_id=run_id,
                        task_id=task.id,
                        attempt=claim.attempt,
                        started_at=started_at,
                        finished_at=finished_at,
                        request=request,
                        result=result,
                        tests=tests,
                        artifacts=artifacts,
                        owner=task.owner,
                        spec_hash=spec_hash,
                        expected_tests=tuple(test.name for test in task.tests),
                        ownership=ownership,
                    )
                    try:
                        claim = self.store.renew_lease(
                            claim, spec.budget.lease_seconds
                        )
                        self.store.complete_task(
                            claim,
                            succeeded=False,
                            receipt=receipt,
                            receipt_hash=receipt_hash,
                            error="publishing verified changes failed",
                            retryable=False,
                            retry_base_seconds=spec.budget.retry_base_seconds,
                            retry_cap_seconds=spec.budget.retry_cap_seconds,
                        )
                    except LeaseLost:
                        return False
        finally:
            heartbeat.stop()
        return True

    def run(self, run_id: str, *, worker_id: str = "local-worker") -> RunResult:
        initial = self.store.snapshot(run_id)
        if initial["state"] not in {
            RunState.SUCCEEDED,
            RunState.FAILED,
            RunState.CANCELLED,
        }:
            self.store.start_run(run_id)
        while True:
            worked = self.execute_one(run_id, worker_id)
            state = self.store.finalize_run(run_id)
            if state in {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}:
                break
            if not worked:
                retry_at = self.store.next_retry_at(run_id)
                if retry_at is None:
                    # This can happen while another worker owns all executable
                    # work.  Yield without spinning.
                    self.sleeper(0.05)
                else:
                    self.sleeper(min(1.0, max(0.0, retry_at - self.clock())))
        snapshot = self.store.snapshot(run_id)
        counts = snapshot["counts"]
        return RunResult(
            run_id=run_id,
            state=RunState(snapshot["state"]),
            tasks_succeeded=counts.get(TaskState.SUCCEEDED, 0),
            tasks_failed=counts.get(TaskState.FAILED, 0),
            tasks_blocked=counts.get(TaskState.BLOCKED, 0),
            tasks_cancelled=counts.get(TaskState.CANCELLED, 0),
        )
